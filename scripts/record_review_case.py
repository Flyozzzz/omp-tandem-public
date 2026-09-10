"""Record one explicitly authorized Claude + OMP review episode, not a benchmark."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omp_tandem.execution import ExecutionOptions, conversation_usage, task_usage
from omp_tandem.reviews import ReviewRequest
from omp_tandem.workspace import resolve_scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help="ReviewRequest JSON, including any author rationale separately",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New private evidence directory; must not already exist",
    )
    parser.add_argument(
        "--allow-paid",
        action="store_true",
        help="Explicitly authorize this coordinator and peer provider episode",
    )
    parser.add_argument("--coordinator-model", default="sonnet")
    parser.add_argument(
        "--peer-profile", choices=("quick", "balanced", "deep"), default="quick"
    )
    parser.add_argument("--budget-seconds", type=int, default=300)
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error(
            "This records real provider calls; --allow-paid requires user authorization"
        )
    if not 10 <= args.budget_seconds <= 7200:
        parser.error("--budget-seconds must be 10..7200")
    project = args.project_root.resolve(strict=True)
    request = ReviewRequest.model_validate_json(args.request.read_text())
    claude = shutil.which("claude")
    omp = shutil.which("omp")
    if claude is None or omp is None:
        parser.error("Configured Claude and OMP executables are required")
    os.umask(0o077)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, mode=0o700)
    clock = time.monotonic()
    started = datetime.now(UTC).isoformat()
    with Path(__file__).open("rb") as source:
        collector_sha = hashlib.file_digest(source, "sha256").hexdigest()
    versions = {
        "collector_sha256": collector_sha,
        "claude": subprocess.run(
            [claude, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip(),
        "omp": subprocess.run(
            [omp, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "tandem": importlib.metadata.version("omp-tandem"),
        "omp_rpc": importlib.metadata.version("omp-rpc"),
    }
    sdk_url = json.loads(
        importlib.metadata.distribution("omp-rpc").read_text("direct_url.json") or "{}"
    ).get("url", "")
    sdk_revision = re.search(r"/([0-9a-f]{40})(?:[./]|$)", sdk_url)
    versions["omp_rpc_revision"] = sdk_revision.group(1) if sdk_revision else None
    for name, executable in (("claude", claude), ("omp", omp)):
        with open(executable, "rb") as source:
            versions[name + "_executable_sha256"] = hashlib.file_digest(
                source, "sha256"
            ).hexdigest()
    state = output / "state"
    config = output / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "omp-tandem": {
                        "command": sys.executable,
                        "args": [
                            "-I",
                            "-m",
                            "omp_tandem",
                            "--project-root",
                            str(project),
                            "--state-dir",
                            str(state),
                            "--omp",
                            omp,
                            "--disable-channel",
                            "--no-legacy-import",
                        ],
                    }
                }
            }
        )
    )
    request_key = str(uuid4())
    creation = {
        "action": "start",
        "request_key": request_key,
        "request": request.model_dump(),
        "execution": {"profile": args.peer_profile},
        "budget_seconds": args.budget_seconds,
    }
    prompt = (
        "Perform this real read-only review of the explicitly bound project. Use tandem_scope, then "
        "tandem_review_run with these exact creation arguments: "
        + json.dumps(creation, ensure_ascii=False)
        + ". Continue using action=status with the returned run_id until a terminal scenario result; "
        "do not start another run, manually switch stages, edit files, or call unrelated tools. "
        "If clarification cannot be answered from the supplied facts, report the blocker and cancel "
        "the run instead of inventing context. Read every available complete stage answer. Briefly explain the "
        "independent observations, comparison/disagreements, current applicability and any remaining "
        "limits. Do not portray peer-only cost as whole-process cost or a successful review as proof "
        "that the code has no bugs. This recording is one episode, not a comparative benchmark."
    )
    command = [
        claude,
        "-p",
        prompt,
        "--model",
        args.coordinator_model,
        "--effort",
        "low",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--tools",
        "",
        "--allowedTools",
        "mcp__omp-tandem__tandem_scope",
        "mcp__omp-tandem__tandem_review_run",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    error = None
    with (
        (output / "coordinator.jsonl").open("w") as stdout,
        (output / "coordinator.stderr").open("w") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=output,
            env={**os.environ, "ENABLE_TOOL_SEARCH": "false"},
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=args.budget_seconds + 180)
        except subprocess.TimeoutExpired:
            error = "Coordinator episode exceeded its bounded deadline; private partial evidence retained."
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    wall_seconds = time.monotonic() - clock
    finals = []
    for line in (output / "coordinator.jsonl").read_text().splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            error = error or "Coordinator output contained an invalid JSON frame."
            continue
        if frame.get("type") == "result":
            finals.append(frame)
    if not finals:
        error = (
            error
            or "Coordinator produced no final measurement record; coordinator usage is unknown."
        )
    final = finals[-1] if finals else {}
    scope = resolve_scope(state, project_root=project)
    runs, tasks, questions, run, manifest = [], [], [], None, None
    database = scope.directory / "tasks.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            runs = [
                dict(row)
                for row in db.execute("SELECT * FROM review_runs ORDER BY created")
            ]
            tasks = [
                dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY created")
            ]
            questions = [
                dict(row)
                for row in db.execute(
                    "SELECT question_id,task_id,question,context,state,answer FROM questions ORDER BY created"
                )
            ]
            run = next((row for row in runs if row["request_key"] == request_key), None)
            manifest = (
                json.loads(
                    db.execute(
                        "SELECT manifest FROM reviews WHERE review_id=?",
                        (run["review_id"],),
                    ).fetchone()[0]
                )
                if run and run["review_id"]
                else None
            )
    peer = conversation_usage(tasks)
    coordinator_cost = final.get("total_cost_usd")
    if (
        type(coordinator_cost) not in (int, float)
        or not math.isfinite(coordinator_cost)
        or coordinator_cost < 0
    ):
        coordinator_cost = None
    peer_cost = peer["cost"]["value"]
    total_cost = (
        coordinator_cost + peer_cost
        if coordinator_cost is not None and peer_cost is not None
        else None
    )
    stages = {}
    task_rows = {row["task_id"]: row for row in tasks}
    for phase in ("independent", "comparison"):
        saved = (
            json.loads(run[phase + "_json"]) if run and run[phase + "_json"] else None
        )
        native = task_rows.get(run[phase + "_task_id"]) if run else None
        submitted = (
            json.loads(native["report_json"])
            if native and native["report_json"]
            else None
        )
        if saved is None and native is not None:
            saved = {
                "task_id": native["task_id"],
                "status": native["status"],
                "outcome": submitted.get("outcome")
                if submitted and native["status"] == "completed"
                else None,
                "answer": submitted.get("answer") if submitted else native["answer"],
                "execution": {
                    "actual": {
                        "model": native["actual_model"],
                        "thinking": native["actual_thinking"],
                    }
                },
                "usage": {"task": task_usage(native)},
            }
        stages[phase] = (
            {
                key: saved.get(key)
                for key in (
                    "task_id",
                    "status",
                    "outcome",
                    "answer",
                    "execution",
                    "usage",
                )
            }
            if saved
            else None
        )
        if stages[phase] is not None:
            stages[phase]["reported_outcome"] = (
                submitted.get("outcome") if submitted else None
            )
            stages[phase]["structured_report"] = submitted is not None
    expected_request = request.model_dump()
    for key in ("paths", "context_paths"):
        if expected_request[key] is not None:
            expected_request[key] = sorted(set(expected_request[key]))
    expected_payload = {
        "request": expected_request,
        "execution": ExecutionOptions(profile=args.peer_profile).model_dump(
            exclude_none=True
        ),
        "budget_seconds": args.budget_seconds,
        "compare": True,
    }
    request_matches = (
        run is not None and json.loads(run["payload_json"]) == expected_payload
    )
    record = {
        "kind": "single_live_review_episode",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "versions": versions,
        "request": request.model_dump(),
        "run_id": run["run_id"] if run else None,
        "status": run["status"] if run else "not_started",
        "outcome": run["outcome"] if run else None,
        "run_error": run["error"] if run else None,
        "run_elapsed_seconds": max(0, run["updated"] - run["created"]) if run else None,
        "coordinator_exit_code": process.returncode,
        "error": error,
        "protocol_followed": len(runs) == 1 and request_matches,
        "wall_seconds": round(wall_seconds, 3),
        "human_seconds": None,
        "coordinator": {
            "requested_model": args.coordinator_model,
            "model_usage": final.get("modelUsage"),
            "usage": final.get("usage"),
            "cost_usd": coordinator_cost,
            "answer": final.get("result"),
        },
        "peer": peer,
        "stages": stages,
        "questions": questions,
        "input_manifest": manifest,
        "total_reported_cost_usd": total_cost,
        "cost_note": "Sum of reported coordinator and OMP estimates when both are complete; not a provider invoice. Unknown is not zero.",
        "timing_note": "Episode includes version inspection, client/MCP startup, capture, provider calls, waiting and client shutdown. Prior implementation/setup and human preparation were not timed. No interactive human input was supplied during this CLI episode.",
        "limits": [
            "One real episode, not a controlled benchmark or proof of superiority.",
            "Working snapshot and model aliases may differ on replay.",
            "Private raw evidence may contain project material; review before publishing.",
        ],
    }
    (output / "case.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "evidence": str(output / "case.json"),
                "status": record["status"],
                "outcome": record["outcome"],
                "protocol_followed": record["protocol_followed"],
                "wall_seconds": record["wall_seconds"],
                "coordinator_cost_usd": coordinator_cost,
                "peer_cost_usd": peer_cost,
                "total_reported_cost_usd": total_cost,
            },
            ensure_ascii=False,
        )
    )
    return (
        0
        if process.returncode == 0
        and error is None
        and record["protocol_followed"]
        and record["status"] in ("completed", "no_changes")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
