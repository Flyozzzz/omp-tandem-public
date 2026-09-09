"""Consumer-facing task summaries, detailed results, and wait projections."""

import json
from contextlib import closing

from .artifacts import ArtifactStore
from .project_context import ProjectContextStore
from .runtime_models import ACTIVE, TaskSummary
from .task_contracts import current_task, work_policy
from .task_store import TaskStore


class TaskResults:
    def __init__(
        self, tasks: TaskStore, artifacts: ArtifactStore, projects: ProjectContextStore
    ):
        self.tasks = tasks
        self.artifacts = artifacts
        self.projects = projects

    def view(self, task_id, details=False):
        task = self.tasks.get(task_id)
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
        if task["activity"] and status in ACTIVE:
            result["activity"] = task["activity"]
        if task["error"]:
            result["error"] = task["error"]
        if status == "waiting_input":
            with closing(self.tasks.connect()) as db:
                question = db.execute(
                    "SELECT * FROM questions WHERE task_id=? AND state='pending'",
                    (task_id,),
                ).fetchone()
            if question:
                result["question"] = {
                    key: question[key]
                    for key in ("question_id", "question", "context", "deadline")
                }
                result["question"]["options"] = json.loads(question["options_json"])
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
