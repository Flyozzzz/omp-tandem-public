"""Bounded clarification callbacks and pinned-context final report coordination."""

import json
import time
from contextlib import closing
from uuid import uuid4

from .artifacts import ArtifactStore
from .findings import FindingStore
from .models import TaskOutcome
from .project_context import ProjectContextStore
from .runtime_models import ACTIVE, Cancelled, QuestionRequest
from .task_store import TaskStore


class TaskInteraction:
    def __init__(
        self,
        tasks: TaskStore,
        artifacts: ArtifactStore,
        projects: ProjectContextStore,
        findings: FindingStore,
    ):
        self.tasks = tasks
        self.artifacts = artifacts
        self.projects = projects
        self.findings = findings

    def submit_report(self, task_id, report):
        report = TaskOutcome.model_validate(report)
        for identifier in report.artifact_ids:
            self.artifacts.info(identifier)
        payload = report.model_dump_json()
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT status, cancel_requested, report_json, project_context_id FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None or task["cancel_requested"] or task["status"] != "running":
                raise ValueError(
                    "Task is not running or still has an unanswered question"
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
            db.execute(
                "UPDATE tasks SET report_json=?, updated=? WHERE task_id=?",
                (payload, time.time(), task_id),
            )
            db.commit()
        return "Final report recorded. End this turn now; do not perform more work."

    def ask(self, task_id, request, context):
        request = QuestionRequest.model_validate(request)
        question_id, now = str(uuid4()), time.time()
        with closing(self.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT status, deadline, cancel_requested, report_json, question_timeout_seconds FROM tasks WHERE task_id=?",
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
            db.execute(
                "UPDATE tasks SET status='waiting_input', activity='Waiting for clarification', updated=? WHERE task_id=?",
                (now, task_id),
            )
            self.tasks.channel.emit(
                "question_waiting",
                {"task_id": task_id, "question_id": question_id},
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
                    "SELECT status, cancel_requested FROM tasks WHERE task_id=?",
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
            if row["state"] == "answered" and row["answer"] == answer:
                return {
                    "task_id": task_id,
                    "question_id": question_id,
                    "accepted": True,
                    "next_action": "wait",
                }
            task = db.execute(
                "SELECT status, cancel_requested FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
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
