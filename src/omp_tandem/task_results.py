"""Consumer-facing task summaries, detailed results, and wait projections."""

import json
import sqlite3
import time
from contextlib import closing

from .artifacts import ArtifactStore
from .execution import conversation_usage, task_usage
from .models import CheckRun, assess_checks
from .project_context import ProjectContextStore
from .runtime_models import ACTIVE, TaskSummary
from .task_contracts import current_task, work_policy
from .task_interaction import CHECK_RUN_ARTIFACT
from .task_store import TaskStore


def read_artifact_text(artifacts, artifact_id):
    """Read a complete artifact by following the bounded reader's pages."""
    pages, offset = [], 0
    while True:
        page = artifacts.read(artifact_id, offset, 50000)
        pages.append(page["content"])
        if page["next_offset"] is None:
            return "".join(pages)
        offset = page["next_offset"]


class TaskResults:
    def __init__(
        self, tasks: TaskStore, artifacts: ArtifactStore, projects: ProjectContextStore
    ):
        self.tasks = tasks
        self.artifacts = artifacts
        self.projects = projects

    def view(self, task_id, details=False, *, refresh=True):
        task = self.tasks.get(task_id, refresh=refresh)
        report = json.loads(task["report_json"]) if task["report_json"] else None
        status = task["status"]
        action = (
            "reply"
            if status == "waiting_input"
            else "wait"
            if status in ACTIVE
            else "review_result"
            if status == "completed"
            else "inspect_error"
        )
        result = {
            "task_id": task_id,
            "conversation_id": task["conversation_id"],
            "status": status,
            "outcome": report["outcome"] if report and status == "completed" else None,
            "summary": report["summary"][:600]
            if report and status == "completed"
            else "",
            "next_action": action,
        }
        if task.get("review_run_id"):
            result["review_run_id"] = task["review_run_id"]
        if task.get("review_id"):
            result["review"] = {
                "review_id": task["review_id"],
                "stage": task["review_stage"],
            }
        result["execution"] = {
            **(
                json.loads(task["execution_json"])
                if task.get("execution_json")
                else {
                    "requested": None,
                    "effective": None,
                }
            ),
            "actual": {
                "model": task.get("actual_model"),
                "thinking": task.get("actual_thinking"),
            },
        }
        with closing(self.tasks.connect()) as db:
            turns = [
                dict(row)
                for row in db.execute(
                    "SELECT accounting_json, started_at, ended_at, duration_seconds, status "
                    "FROM tasks WHERE conversation_id=? ORDER BY created, task_id",
                    (task["conversation_id"],),
                )
            ]
        result["usage"] = {
            "task": task_usage(task),
            "conversation": conversation_usage(turns),
        }
        if task["activity"] and status in ACTIVE:
            result["activity"] = task["activity"]
        if task["error"]:
            result["error"] = task["error"]
        if status == "waiting_input":
            with closing(self.tasks.connect()) as db:
                question = db.execute(
                    "SELECT * FROM questions WHERE task_id=? AND state='pending' AND deadline>?",
                    (task_id, time.time()),
                ).fetchone()
            if question:
                result["question"] = {
                    key: question[key]
                    for key in ("question_id", "question", "context", "deadline")
                }
                result["question"]["options"] = json.loads(question["options_json"])
            else:
                result["next_action"] = "wait"
        artifact_ids = json.loads(task["result_artifacts"])
        if artifact_ids:
            result["artifacts"] = [
                self.artifacts.info(identifier) for identifier in artifact_ids
            ]
        preliminary = [
            item
            for item in self.artifacts.for_task(task_id)
            if item["artifact_id"] not in artifact_ids
        ]
        if preliminary:
            result["provisional_artifacts"] = (
                preliminary if details else preliminary[:10]
            )
            result["provisional_artifacts_count"] = len(preliminary)
            result["provisional_artifacts_truncated"] = (
                not details and len(preliminary) > 10
            )
        runs, unreadable = self._check_runs(task_id)
        result["check_runs"] = assess_checks(runs)
        result["check_runs"]["unreadable_records"] = unreadable
        result["facts"] = self._facts(
            task_id, task, status, report, result["check_runs"]
        )
        if task["project_context_id"]:
            result["project_context"] = self.projects.info(task["project_context_id"])
            if task["previous_project_context_id"]:
                result["project_context_changed_from"] = task[
                    "previous_project_context_id"
                ]
        if status not in ACTIVE:
            reported_answer = report.get("answer") if report else None
            answer = reported_answer or task["answer"]
            limit = len(answer) if details else 16000
            result["answer"] = answer[:limit]
            result["answer_length"] = len(answer)
            result["answer_truncated"] = len(answer) > limit
            result["answer_source"] = (
                "report" if reported_answer else "unstructured" if answer else None
            )
            reply = next(
                (
                    item
                    for item in result.get("artifacts", [])
                    if item["task_id"] == task_id and item["name"] == "reply"
                ),
                None,
            )
            if reply is not None:
                result["answer_artifact_id"] = reply["artifact_id"]
        if status == "completed" and report is None:
            result["summary"] = (
                "Historical unstructured response; outcome was not assessed."
            )
        if details:
            result["report"] = report
            result["diagnostics"] = {
                key: task[key]
                for key in (
                    "cwd",
                    "model",
                    "mode",
                    "session_file",
                    "created",
                    "updated",
                    "deadline",
                    "question_timeout_seconds",
                    "event_history_limit",
                )
            }
            result["contract"] = (
                json.loads(task["contract_json"]) if task["contract_json"] else None
            )
            result["work_policy"] = {
                "mode": task["mode"],
                "cwd": task["cwd"],
                **work_policy(task).model_dump(),
            }
            result["current_task"] = current_task(task)
            if task["project_context_id"]:
                result["project_context"]["context"] = self.projects.get(
                    task["project_context_id"]
                )["context"]
        return result

    def _check_runs(self, task_id):
        """Server-recorded check runs for this task, oldest first.

        Only artifacts under the reserved server name count; a participant
        publication with a similar name is an ordinary artifact and never a run.
        A record that cannot be decoded is reported as unreadable rather than
        breaking the whole view.
        """
        runs, unreadable = [], []
        for item in reversed(self.artifacts.for_task(task_id)):
            if item["name"] != CHECK_RUN_ARTIFACT:
                continue
            try:
                record = json.loads(
                    read_artifact_text(self.artifacts, item["artifact_id"])
                )
                if not isinstance(record, dict):
                    raise TypeError("check run record is not an object")
                for key in ("task_id", "recorded_at", "recorded_by"):
                    record.pop(key, None)
                # Validate each record on its own so one malformed historical
                # record never erases the assessment of the valid ones.
                runs.append(CheckRun.model_validate(record))
            except (ValueError, TypeError) as exc:
                unreadable.append(
                    {"artifact_id": item["artifact_id"], "error": str(exc)[:200]}
                )
        return runs, unreadable

    def _facts(self, task_id, task, status, report, checks):
        """Four separately observed facts; none is inferred from another.

        execution: how the worker turn ended; delivery: what the final report
        declared, or that none was delivered; verdict: the linked shared-work
        reviewer decision, if any; checks: current applicability of the recorded
        check runs. A delivered rejection is a successful delivery, and an
        interrupted turn with a saved report is still interrupted.
        """
        if status in ACTIVE:
            execution = "running"
        elif status == "completed":
            execution = "completed"
        elif status == "cancelled":
            execution = "cancelled"
        elif status == "interrupted":
            execution = "interrupted"
        else:
            execution = "failed"
        if report is not None:
            delivery = report["outcome"]
        elif status in ACTIVE:
            delivery = "pending"
        else:
            delivery = "missing"
        verdict = {"status": "none", "attempt_id": None, "kind": None}
        with closing(self.tasks.connect()) as db:
            try:
                row = db.execute(
                    "SELECT attempt FROM work_attempts WHERE native_task_id=?",
                    (task_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
        if row is not None:
            attempt = json.loads(row["attempt"])
            verdict = {
                "status": attempt.get("verdict") or "none",
                "attempt_id": attempt.get("attempt_id"),
                "kind": attempt.get("kind"),
            }
        return {
            "execution": execution,
            "delivery": delivery,
            "verdict": verdict,
            "checks": {
                "status": checks["status"],
                "run_count": checks["run_count"],
                "known_issues": len(checks["known_issues"]),
                "invalid_supersedes": len(checks["invalid_supersedes"]),
            },
        }

    def recent(self, limit=20):
        ids = self.tasks.recent_ids(limit)
        keys = TaskSummary.__annotations__
        return [
            {key: value for key, value in self.view(identifier).items() if key in keys}
            for identifier in ids
        ]

    def wait_snapshot(self, task_ids):
        identifiers = list(dict.fromkeys(task_ids))
        found = self.tasks.wait_states(identifiers)
        if len(found) != len(identifiers):
            raise ValueError("Unknown task_id in wait set")
        ready, pending = [], []
        for identifier in identifiers:
            state = found[identifier]
            if state["status"] == "waiting_input":
                current = self.view(identifier)
                state["status"] = current["status"]
                if current.get("question"):
                    state["question"] = current["question"]
            if state["status"] == "waiting_input" or state["status"] not in ACTIVE:
                state["next_action"] = (
                    "reply" if state.get("question") else "read_result"
                )
                ready.append(state)
            else:
                pending.append(identifier)
        return self.tasks.channel.decorate({"ready": ready, "pending": pending})
