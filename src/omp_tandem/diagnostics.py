"""Explicit, bounded live checks in the MCP session being diagnosed."""

import asyncio
import secrets
import shutil
import time
from contextlib import closing
from pathlib import Path

from .runtime_models import ACTIVE


class Diagnostics:
    def __init__(self, bridge):
        self.bridge = bridge
        with closing(bridge.tasks.connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS diagnostic_probes "
                "(task_id TEXT PRIMARY KEY, expected_answer TEXT NOT NULL)"
            )

    def inspect(self, expected_project=None):
        bridge = self.bridge
        executable = bridge.runtime.worker.executable
        available = shutil.which(executable)
        expected = None
        if expected_project is not None:
            expected_path = Path(expected_project).expanduser()
            if not expected_path.is_absolute():
                raise ValueError("expected_project must be an absolute path")
            expected = str(expected_path.resolve())
        scope = bridge.scope.info()
        matches = str(bridge.scope.root) == expected if expected is not None else None
        channel = bridge.channel.status()
        issues = []
        if matches is False:
            issues.append(
                {
                    "code": "project_mismatch",
                    "reason": "This client is bound to a different project than expected.",
                    "next_step": "Restart the client from the intended project or correct its launch-project configuration; task cwd cannot switch data namespaces.",
                }
            )
        if available is None:
            issues.append(
                {
                    "code": "omp_unavailable",
                    "reason": "The configured OMP executable is unavailable.",
                    "next_step": "Install OMP or correct the configured executable path, then repeat the check.",
                }
            )
        return {
            "status": "blocked" if issues else "local_ready",
            "project": {
                **scope,
                "expected_project": expected,
                "matches_expected": matches,
            },
            "runtime": {
                "executable": executable,
                "available": available is not None,
                "requested_model": bridge.runtime.model or None,
                "actual_model": None,
                "authentication": "not_checked",
                "short_task": "not_run",
            },
            "channel": channel,
            "issues": issues,
            "next_step": issues[0]["next_step"]
            if issues
            else "Run tandem_diagnose(live=true) only when the user requests a live check; it makes a provider request and may incur a charge.",
        }

    async def run(
        self,
        *,
        live=False,
        task_id=None,
        expected_project=None,
        wait_seconds=25,
        timeout_seconds=90,
    ):
        if live and task_id is not None:
            raise ValueError(
                "Use live=true to start a check OR task_id to inspect the existing check"
            )
        report = self.inspect(expected_project)
        if report["status"] == "blocked" or (not live and task_id is None):
            return self.bridge.channel.decorate(report)
        if task_id is None:
            expected_answer = "OMP_TANDEM_DIAGNOSTIC_" + secrets.token_hex(12)
            job = await asyncio.to_thread(
                self.bridge.start,
                prompt=(
                    "The user requested a live connectivity check. Do not read files, ask questions, "
                    "or change external state. Call tandem_finish with outcome success, a short "
                    "summary, and answer exactly "
                    + expected_answer
                    + ". Then end the turn."
                ),
                mode="think",
                timeout_seconds=timeout_seconds,
                execution={"profile": "quick"},
            )
            task_id = job["task_id"]
            try:
                with closing(self.bridge.tasks.connect()) as db:
                    db.execute(
                        "INSERT INTO diagnostic_probes VALUES (?,?)",
                        (task_id, expected_answer),
                    )
            except Exception:
                await asyncio.to_thread(self.bridge.cancel, task_id)
                raise
        else:
            with closing(self.bridge.tasks.connect()) as db:
                probe = db.execute(
                    "SELECT expected_answer FROM diagnostic_probes WHERE task_id=?",
                    (task_id,),
                ).fetchone()
            if probe is None:
                raise ValueError(
                    "Unknown diagnostic task; do not restart a running check"
                )
            expected_answer = probe["expected_answer"]
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await asyncio.to_thread(self.bridge.view, task_id, True)
            if (
                result["status"] not in ACTIVE
                or result["status"] == "waiting_input"
                or time.monotonic() >= deadline
            ):
                break
            await asyncio.sleep(0.15)
        report["task_id"] = task_id
        report["conversation_id"] = result["conversation_id"]
        report["task_status"] = result["status"]
        report["channel"] = self.bridge.channel.status()
        report["runtime"]["short_task"] = result["status"]
        report["execution"] = result.get("execution")
        report["usage"] = result.get("usage")
        if result["status"] in ACTIVE:
            report["status"] = "running"
            report["next_step"] = (
                "Inspect this same task with tandem_diagnose(task_id=...); do not start another paid check."
            )
            report["next_action"] = "wait"
            if result["status"] == "waiting_input":
                report["status"] = "blocked"
                report["question"] = result.get("question")
                report["next_action"] = "reply"
                report["next_step"] = (
                    "The diagnostic unexpectedly needs input. Inspect its question; do not fabricate credentials or approval."
                )
            return self.bridge.channel.decorate(report)
        succeeded = (
            result["status"] == "completed"
            and result.get("outcome") == "success"
            and result.get("answer") == expected_answer
        )
        if succeeded:
            report["runtime"].update(
                authentication="verified_by_task",
                actual_model=result.get("execution", {}).get("actual", {}).get("model"),
                short_task="passed",
            )
            report["status"] = "ready"
            if not report["channel"]["confirmed"]:
                report["next_step"] = (
                    "OMP completed the check; push receipt is not confirmed, so use bounded polling. Acknowledge a real channel probe if this client supports Channels; no second paid check is needed."
                )
            else:
                report["next_step"] = (
                    "OMP completed the check and channel receipt is confirmed. Follow current delivery instructions; channel receipt alone does not prove an independent watchdog is armed."
                )
        else:
            error = (
                result.get("error")
                or "The diagnostic did not produce its expected structured answer."
            )
            report["status"] = "blocked"
            report["runtime"]["short_task"] = "failed"
            report["issues"].append(
                {
                    "code": "live_task_failed",
                    "reason": error,
                    "next_step": "Inspect the task error and OMP provider/model configuration. Authentication remains unverified; this check does not modify credentials.",
                }
            )
            report["next_step"] = report["issues"][-1]["next_step"]
        report["next_action"] = "review_result"
        return self.bridge.channel.decorate(report)
