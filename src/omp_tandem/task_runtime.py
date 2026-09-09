"""Task admission and owner-scoped worker threads and capacity leases."""

import json
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

from .artifacts import ArtifactStore
from .execution import resolve_execution
from .models import TaskContract, TurnContract, WorkPolicy
from .native_worker import NativeWorker
from .project_context import ProjectContextStore
from .runtime_models import MAX_EVENT_HISTORY
from .task_contracts import TaskMessages, owned_paths, work_policy
from .task_store import TaskStore
from .workspace import ProjectScope, WorkerSlots


class TaskRuntime:
    def __init__(
        self,
        tasks: TaskStore,
        worker: NativeWorker,
        messages: TaskMessages,
        artifacts: ArtifactStore,
        projects: ProjectContextStore,
        scope: ProjectScope,
        slots: WorkerSlots,
        model: str | None,
    ):
        self.tasks = tasks
        self.worker = worker
        self.messages = messages
        self.artifacts = artifacts
        self.projects = projects
        self.scope = scope
        self.slots = slots
        self.model = model or ""
        self.threads: dict[str, threading.Thread] = {}
        self.guard = threading.Lock()
        self.closing = False

    def start(
        self,
        prompt=None,
        cwd=None,
        mode="analyze",
        conversation_id=None,
        timeout_seconds=None,
        contract=None,
        project_context_id=None,
        question_timeout_seconds=None,
        granted_roots=(),
        *,
        execution=None,
        review_id=None,
        review_stage=None,
    ):
        resuming = conversation_id is not None
        if (prompt is None) == (contract is None):
            raise ValueError("Supply exactly one of prompt or contract")
        if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
            raise ValueError("prompt cannot be empty")
        if mode not in ("think", "analyze", "work"):
            raise ValueError("Invalid mode")
        if question_timeout_seconds is not None and (
            type(question_timeout_seconds) is not int
            or not 1 <= question_timeout_seconds <= 1800
        ):
            raise ValueError("question_timeout_seconds must be 1..1800")
        if contract is not None:
            contract = (TurnContract if resuming else TaskContract).model_validate(
                contract
            )
        self.tasks.recover()
        conversation_id = str(UUID(conversation_id)) if resuming else str(uuid4())
        handle = self.tasks.lock(conversation_id)
        task_id = str(uuid4())
        slot = None
        try:
            session_file, model = None, self.model
            previous_context_id = None
            previous_execution = None
            policy = WorkPolicy()
            if resuming:
                previous = self.tasks.latest(conversation_id)
                if previous is None:
                    raise ValueError("Unknown conversation_id; use tandem_list")
                cwd, mode, model, session_file = (
                    previous[key] for key in ("cwd", "mode", "model", "session_file")
                )
                policy = work_policy(previous)
                if previous.get("execution_json"):
                    previous_execution = json.loads(previous["execution_json"])[
                        "effective"
                    ]
                    # Continue the native selection, not an earlier fuzzy model alias.
                    if previous.get("actual_model"):
                        previous_execution["model"] = previous["actual_model"]
                    if previous.get("actual_thinking"):
                        previous_execution["thinking"] = previous["actual_thinking"]
                if review_id is None:
                    review_id = previous.get("review_id")
                if review_stage is None:
                    review_stage = previous.get("review_stage")
                previous_context_id = previous["project_context_id"]
                if project_context_id is None:
                    project_context_id = previous_context_id
                if question_timeout_seconds is None:
                    question_timeout_seconds = (
                        previous["question_timeout_seconds"] or 300
                    )
                if session_file:
                    session_file = str(self.tasks.session_path(session_file))
                if not session_file or not Path(session_file).is_file():
                    raise ValueError(
                        "Conversation has no saved OMP history; start a new task with its context"
                    )
            elif contract is not None:
                policy = WorkPolicy(
                    scope=contract.scope, constraints=contract.constraints
                )
            settings = resolve_execution(
                execution,
                previous=previous_execution,
                model=model,
                timeout_seconds=timeout_seconds,
            )
            if question_timeout_seconds is None:
                question_timeout_seconds = 300
            cwd = self.scope.validate_cwd(cwd, allowed_roots=granted_roots)
            snapshot = (
                self.projects.get(project_context_id)
                if project_context_id is not None
                else None
            )
            if snapshot is not None:
                project_context_id = snapshot["context_id"]
                if (
                    previous_context_id
                    and self.projects.info(previous_context_id)["project_id"]
                    != snapshot["project_id"]
                ):
                    raise ValueError(
                        "A follow-up cannot switch products; start a new conversation"
                    )
            changed_from = (
                previous_context_id
                if previous_context_id != project_context_id
                else None
            )
            now = time.time()
            record = {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "created": now,
                "updated": now,
                "cwd": str(Path(cwd).resolve()),
                "mode": mode,
                "model": settings["effective"]["model"] or "",
                "prompt": prompt or "",
                "session_file": session_file,
                "status": "starting",
                "contract_json": contract.model_dump_json() if contract else None,
                "policy_json": policy.model_dump_json(),
                "deadline": now + settings["effective"]["timeout_seconds"],
                "execution_json": json.dumps(settings),
                "review_id": review_id,
                "review_stage": review_stage,
                "project_context_id": project_context_id,
                "previous_project_context_id": changed_from,
                "question_timeout_seconds": question_timeout_seconds,
                "event_history_limit": MAX_EVENT_HISTORY,
                "workspace_roots": json.dumps(
                    [
                        str(root)
                        for root in dict.fromkeys((self.scope.root, *granted_roots))
                    ]
                ),
            }
            if len(self.messages.build(record, snapshot).encode("utf-8")) > 200_000:
                raise ValueError(
                    "Resolved task and product context exceed 200000 UTF8 bytes; move details into artifacts"
                )
            for identifier in contract.artifact_ids if contract else ():
                self.artifacts.info(identifier)
            owned = owned_paths(cwd, policy)
            slot = self.slots.acquire()
            with self.guard:
                if self.closing:
                    raise ValueError("MCP server is shutting down")
                self.tasks.insert(record, owned)
                thread = threading.Thread(
                    target=self._run, args=(task_id, handle, slot), daemon=True
                )
                self.threads[task_id] = thread
                try:
                    thread.start()
                except RuntimeError:
                    self.threads.pop(task_id, None)
                    self.tasks.update(
                        task_id, status="failed", error="Could not start worker thread"
                    )
                    raise
        except BaseException:
            handle.close()
            if slot is not None:
                slot.close()
            raise
        result = {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "status": "starting",
            "next_action": "wait",
            "execution": {**settings, "actual": {"model": None, "thinking": None}},
        }
        if snapshot is not None:
            result["project_context"] = {
                key: value for key, value in snapshot.items() if key != "context"
            }
        return result

    def _run(self, task_id, handle, slot):
        try:
            self.worker.execute(task_id)
        finally:
            handle.close()
            slot.close()
            with self.guard:
                self.threads.pop(task_id, None)

    def shutdown(self):
        with self.guard:
            self.closing = True
            threads = list(self.threads.items())
        for task_id, _ in threads:
            self.tasks.cancel(task_id)
        for _, thread in threads:
            thread.join(timeout=55)
