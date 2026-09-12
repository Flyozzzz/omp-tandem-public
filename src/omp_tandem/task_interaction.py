"""Bounded clarification callbacks and pinned-context final report coordination."""

import json
import time
from contextlib import closing
from uuid import uuid4

from .artifacts import ArtifactStore
from .findings import FindingStore
from .models import decode_outcome
from .project_context import ProjectContextStore
from .runtime_models import (
    ACTIVE,
    RESERVED_ARTIFACT_PREFIX,
    Cancelled,
    QuestionRequest,
)
from .task_store import TaskStore
from .work_items import independent_stage

# Server-recorded records live under the reserved prefix that PublishRequest
# refuses, so a participant cannot publish a look-alike record.
CHECK_RUN_ARTIFACT = RESERVED_ARTIFACT_PREFIX + "check-run"


class TaskInteraction:
    def __init__(
        self,
        tasks: TaskStore,
        artifacts: ArtifactStore,
        projects: ProjectContextStore,
        findings: FindingStore,
        work_items,
    ):
        self.tasks = tasks
        self.artifacts = artifacts
        self.projects = projects
        self.findings = findings
        self.work_items = work_items

    def _independent(self, task_id, task):
        attempt = self.work_items.stage_attempt(task_id)
        if attempt is not None:
            return independent_stage(attempt)
        # Low-level snapshot tasks and review runs have the same clarification
        # boundary; legacy managed attempts retain their recorded disclosure mode.
        return bool(task["review_id"] and task["review_stage"] == "independent")

    def _require_snapshot(self, db, task_id, question):
        try:
            context = json.loads(question["context"])
        except (ValueError, TypeError):
            context = None
        paths = context.get("requested_paths", []) if isinstance(context, dict) else []
        if (
            not isinstance(paths, list)
            or len(paths) > 256
            or any(not isinstance(path, str) for path in paths)
        ):
            paths = []
        result = {
            "question_id": question["question_id"],
            "clarification_requires_new_snapshot": True,
            "reason": question["question"],
            "context": question["context"],
            "requested_paths": list(dict.fromkeys(paths)),
            "next_step": (
                "End this stage blocked or partial. Declare the required context and "
                "create a new immutable snapshot/attempt; do not reply with free text."
            ),
        }
        db.execute(
            "UPDATE questions SET state='context_required' WHERE question_id=?",
            (question["question_id"],),
        )
        db.execute(
            "UPDATE tasks SET status='running', activity='Clarification requires new snapshot', updated=? WHERE task_id=?",
            (time.time(), task_id),
        )
        self.work_items.block_review_context(db, task_id, result)
        return json.dumps(result, ensure_ascii=False)

    def submit_report(self, task_id, report):
        # Refusals name the offending fields and the permitted correction; the
        # declared outcome is never rewritten on the author's behalf.
        report = decode_outcome(report)
        for identifier in report.artifact_ids:
            self.artifacts.info(identifier)
        for run in report.check_runs:
            if run.output_artifact_id:
                self.artifacts.info(run.output_artifact_id)
        payload = report.model_dump_json()
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT status, cancel_requested, report_json, project_context_id, conversation_id FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None or task["cancel_requested"] or task["status"] != "running":
                raise ValueError(
                    "Task is not running or still has an unanswered question"
                )
            if (
                report.outcome not in ("blocked", "partial")
                and db.execute(
                    "SELECT 1 FROM questions WHERE task_id=? AND state='context_required'",
                    (task_id,),
                ).fetchone()
            ):
                raise ValueError(
                    "Clarification requires a new snapshot; this stage must remain blocked or partial"
                )
            snapshot = (
                self.projects.get(task["project_context_id"])
                if task["project_context_id"]
                else None
            )
            rules = (
                {rule["id"] for rule in snapshot["context"]["rules"]}
                if snapshot
                else set()
            )
            decisions = (
                {decision["id"] for decision in snapshot["context"]["decisions"]}
                if snapshot
                else set()
            )
            unknown_rules = {
                reference.rule_id for reference in report.rule_references
            } - rules
            unknown_decisions = set(report.decision_references) - decisions
            if unknown_rules or unknown_decisions:
                raise ValueError(
                    f"Report references are not in the pinned product context: rules={sorted(unknown_rules)}, decisions={sorted(unknown_decisions)}"
                )
            if task["report_json"] is not None and task["report_json"] != payload:
                raise ValueError("A final report has already been submitted")
            if report.findings or report.finding_updates:
                self.findings.ingest_report(
                    db,
                    task_id,
                    findings=report.findings,
                    finding_updates=report.finding_updates,
                )
            if task["report_json"] is None:
                # Append-only history in the existing immutable artifact layer: one
                # record per run, never rewritten by a later report or run.
                recorded = time.time()
                for run in report.check_runs:
                    self.artifacts.publish_in_transaction(
                        db,
                        CHECK_RUN_ARTIFACT,
                        json.dumps(
                            {
                                **run.model_dump(),
                                "task_id": task_id,
                                "recorded_at": recorded,
                                "recorded_by": "report",
                            },
                            ensure_ascii=False,
                        ),
                        "application/json",
                        conversation_id=task["conversation_id"],
                        task_id=task_id,
                    )
            db.execute(
                "UPDATE tasks SET report_json=?, updated=? WHERE task_id=?",
                (payload, time.time(), task_id),
            )
            db.commit()
        return "Final report recorded. End this turn now; do not perform more work."

    def ask(self, task_id, request, context):
        if context.cancelled:
            raise Cancelled()
        request = QuestionRequest.model_validate(request)
        question_id, now = str(uuid4()), time.time()
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if (
                task is None
                or task["status"] != "running"
                or task["cancel_requested"]
                or task["report_json"]
            ):
                raise ValueError(
                    "Cannot ask: task is not running, already waiting, or has submitted its report"
                )
            deadline = min(
                now + (task["question_timeout_seconds"] or 300), task["deadline"]
            )
            if deadline <= now:
                raise TimeoutError("Task deadline reached")
            db.execute(
                "INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    question_id,
                    task_id,
                    request.question,
                    request.context,
                    json.dumps(request.options, ensure_ascii=False),
                    now,
                    deadline,
                    "pending",
                    None,
                    None,
                ),
            )
            if self._independent(task_id, task):
                result = self._require_snapshot(
                    db,
                    task_id,
                    {
                        "question_id": question_id,
                        "question": request.question,
                        "context": request.context,
                    },
                )
                db.commit()
                return result
            db.execute(
                "UPDATE tasks SET status='waiting_input', activity='Waiting for clarification', updated=? WHERE task_id=?",
                (now, task_id),
            )
            self.tasks.channel.emit(
                "question_waiting",
                {
                    "task_id": task_id,
                    "question_id": question_id,
                    **(
                        {"review_run_id": task["review_run_id"]}
                        if task["review_run_id"]
                        else {}
                    ),
                },
                task_id=task_id,
                dedupe_key=f"question:{question_id}",
                connection=db,
            )
            db.commit()
        self.tasks.channel.signal()
        while True:
            if context.cancelled:
                raise Cancelled()
            with closing(self.tasks.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                task = db.execute(
                    "SELECT * FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                question = db.execute(
                    "SELECT * FROM questions WHERE question_id=?", (question_id,)
                ).fetchone()
                if (
                    task is None
                    or task["cancel_requested"]
                    or task["status"] not in ACTIVE
                ):
                    raise Cancelled()
                if question["state"] == "context_required" or self._independent(
                    task_id, task
                ):
                    result = self._require_snapshot(db, task_id, question)
                    db.commit()
                    return result
                if question["state"] == "answered":
                    db.commit()
                    return json.dumps(
                        {"question_id": question_id, "answer": question["answer"]},
                        ensure_ascii=False,
                    )
                if question["state"] not in ("pending", "expired"):
                    raise ValueError(
                        f"Question is {question['state']}; do not assume an answer"
                    )
                if (
                    question["state"] == "expired"
                    or time.time() >= question["deadline"]
                ):
                    db.execute(
                        "UPDATE questions SET state='expired' WHERE question_id=?",
                        (question_id,),
                    )
                    db.execute(
                        "UPDATE tasks SET status='running', activity='Clarification expired', updated=? WHERE task_id=? AND status='waiting_input'",
                        (time.time(), task_id),
                    )
                    db.commit()
                    return json.dumps(
                        {
                            "question_id": question_id,
                            "expired": True,
                            "instruction": "No answer was received. Report blocked or partial; do not invent the missing decision.",
                        }
                    )
                db.commit()
            time.sleep(0.15)

    def reply(self, task_id, question_id, answer):
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or len(answer.encode("utf-8")) > 60000
        ):
            raise ValueError("Answer must be nonempty and at most 60000 UTF8 bytes")
        self.tasks.get(task_id)
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM questions WHERE question_id=? AND task_id=?",
                (question_id, task_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown question_id for this task")
            task = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row["state"] == "context_required" or self._independent(task_id, task):
                raise ValueError(
                    "Clarification requires a new immutable snapshot/attempt; free-text replies cannot enter this independent stage"
                )
            if row["state"] == "answered" and row["answer"] == answer:
                return {
                    "task_id": task_id,
                    "question_id": question_id,
                    "accepted": True,
                    "next_action": "wait",
                }
            if row["state"] != "pending" or time.time() >= row["deadline"]:
                raise ValueError("Question is no longer pending or has expired")
            if task["status"] != "waiting_input" or task["cancel_requested"]:
                raise ValueError("Task no longer accepts clarification")
            now = time.time()
            db.execute(
                "UPDATE questions SET state='answered', answer=?, answered=? WHERE question_id=?",
                (answer, now, question_id),
            )
            db.execute(
                "UPDATE tasks SET status='running', activity='Continuing after clarification', updated=? WHERE task_id=?",
                (now, task_id),
            )
            db.commit()
        return {
            "task_id": task_id,
            "question_id": question_id,
            "accepted": True,
            "next_action": "wait",
        }
