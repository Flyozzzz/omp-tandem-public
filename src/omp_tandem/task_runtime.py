"""Task admission and owner-scoped worker threads and capacity leases."""

import json
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID, uuid4

from .artifacts import ArtifactStore
from .execution import resolve_execution
from .models import (
    ConversationHandoff,
    TaskContract,
    TaskRequirements,
    TurnContract,
    WorkPolicy,
)
from .native_worker import NativeWorker
from .project_context import ProjectContextStore
from .review_check_state import has_review_runner
from .runtime_models import MAX_EVENT_HISTORY
from .task_contracts import (
    TaskMessages,
    owned_paths,
    require_review_context_capture,
    work_policy,
)
from .task_store import TaskStore
from .verification import verification_requirements
from .work_items import shell_permission
from .work_workspace import observe_declared_paths
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

    def preflight(
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
        reserved_task_id=None,
        review_run_id=None,
        work_attempt_id=None,
        continuation="resume",
        handoff=None,
    ):
        """Read-only admission observations, not a reservation or execution proof."""
        arguments = {key: value for key, value in locals().items() if key != "self"}
        prepared = self._prepare(**arguments)
        return prepared["preflight"]

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
        reserved_task_id=None,
        review_run_id=None,
        work_attempt_id=None,
        continuation="resume",
        handoff=None,
    ):
        arguments = {key: value for key, value in locals().items() if key != "self"}
        # No recovery, reservation, or capacity mutation before basic admission succeeds.
        initial = self._prepare(**arguments)
        task_id = initial["record"]["task_id"]
        destination = initial["record"]["conversation_id"]
        handles = ExitStack()
        slot = None
        try:
            # Fresh must serialize with the source too, rather than escape its lease.
            for identifier in sorted(
                {
                    destination,
                    *([str(UUID(conversation_id))] if conversation_id else []),
                }
            ):
                handles.enter_context(self.tasks.lock(identifier))
            with self.guard:
                if self.closing:
                    raise ValueError("MCP server is shutting down")
                prepared = self._prepare(**arguments)
                record = prepared["record"]
                record.update(task_id=task_id, conversation_id=destination)
                slot = self.slots.acquire()
                self.tasks.insert(record, prepared["owned"])
                if work_attempt_id is not None:
                    try:
                        self.worker.work_items.started(
                            work_attempt_id,
                            workspace=record["cwd"],
                            native_task_id=task_id,
                        )
                    except Exception:
                        self.tasks.update(
                            task_id,
                            status="failed",
                            error="Work attempt admission was rejected",
                        )
                        raise
                thread = threading.Thread(
                    target=self._run, args=(task_id, handles, slot), daemon=True
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
            handles.close()
            if slot is not None:
                slot.close()
            raise
        result = {
            "task_id": task_id,
            "conversation_id": destination,
            "status": "starting",
            "next_action": "wait",
            "execution": {
                **prepared["settings"],
                "actual": {"model": None, "thinking": None},
            },
            "continuation": continuation,
            "previous_task_id": record["previous_task_id"],
            "previous_conversation_id": record["previous_conversation_id"],
            "diagnostics": prepared["preflight"]["diagnostics"],
        }
        if prepared["snapshot"] is not None:
            result["project_context"] = prepared["preflight"]["project_context"]
        return result

    def _prepare(
        self,
        prompt,
        cwd,
        mode,
        conversation_id,
        timeout_seconds,
        contract,
        project_context_id,
        question_timeout_seconds,
        granted_roots,
        *,
        execution,
        review_id,
        review_stage,
        reserved_task_id,
        review_run_id,
        work_attempt_id,
        continuation,
        handoff,
    ):
        following = conversation_id is not None
        fresh = continuation == "fresh"
        if continuation not in {"resume", "fresh"}:
            raise ValueError("continuation must be resume or fresh")
        if fresh and (not following or handoff is None):
            raise ValueError(
                "Fresh continuation requires conversation_id and an explicit handoff"
            )
        if not fresh and handoff is not None:
            raise ValueError("handoff is only valid for fresh continuation")
        handoff = (
            ConversationHandoff.model_validate(handoff) if handoff is not None else None
        )
        if (reserved_task_id is None) != (review_run_id is None):
            raise ValueError(
                "Review run dispatch requires both private reservation arguments"
            )
        if fresh and (
            review_id is not None
            or review_run_id is not None
            or work_attempt_id is not None
        ):
            raise ValueError(
                "Fresh continuation cannot transfer review or managed work capabilities"
            )
        if review_run_id is not None:
            review_run_id = str(UUID(review_run_id))
            if (review_stage == "independent" and following) or (
                review_stage == "comparison" and not following
            ):
                raise ValueError(
                    "Review run stages must start independently then resume for comparison"
                )
        attempt = None
        if work_attempt_id is not None:
            if following or review_run_id is not None:
                raise ValueError(
                    "Managed work attempts require a dedicated native turn"
                )
            attempt = self.worker.work_items.attempt(work_attempt_id)
            self.worker.work_items.authenticate(attempt["token"])
            if attempt["actor"] != "omp":
                raise ValueError("Native work attempt is not assigned to OMP")
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
            contract = (TurnContract if following else TaskContract).model_validate(
                contract
            )
        source_id = str(UUID(conversation_id)) if following else None
        destination = str(uuid4()) if fresh or not following else source_id
        task_id = str(UUID(reserved_task_id)) if reserved_task_id else str(uuid4())
        previous = None
        session_file, model = None, self.model
        previous_context_id, previous_execution = None, None
        policy = WorkPolicy()
        diagnostics = []
        if following:
            previous = self.tasks.latest(source_id)
            if previous is None:
                raise ValueError("Unknown conversation_id; use tandem_list")
            history = self.tasks.admission_history(source_id)
            self._check_continuation(history, fresh=fresh)
            cwd, mode, model = (previous[key] for key in ("cwd", "mode", "model"))
            policy = work_policy(previous)
            if previous.get("execution_json"):
                previous_execution = json.loads(previous["execution_json"])["effective"]
                if previous.get("actual_model"):
                    previous_execution["model"] = previous["actual_model"]
                if previous.get("actual_thinking"):
                    previous_execution["thinking"] = previous["actual_thinking"]
            if not fresh:
                session_file = previous["session_file"]
                if review_id is None:
                    review_id = previous.get("review_id")
                if review_stage is None:
                    review_stage = previous.get("review_stage")
                if session_file:
                    session_file = str(self.tasks.session_path(session_file))
                if not session_file or not Path(session_file).is_file():
                    raise ValueError(
                        "Conversation has no saved OMP history; explicitly request fresh with a handoff"
                    )
            previous_context_id = previous["project_context_id"]
            if project_context_id is None:
                project_context_id = previous_context_id
            if question_timeout_seconds is None:
                question_timeout_seconds = previous["question_timeout_seconds"] or 300
            if len(history) >= 20:
                diagnostics.append(
                    {
                        "code": "long_conversation",
                        "turn_count": len(history),
                        "recommendation": "Consider an explicit fresh continuation with a concise handoff; no automatic rotation.",
                    }
                )
            stop = json.loads(previous.get("execution_json") or "{}").get("stop", {})
            if stop.get("classification") == "length":
                diagnostics.append(
                    {
                        "code": "context_budget_stop",
                        "source_task_id": previous["task_id"],
                        "recommendation": "Consider explicit fresh after reconciling any effects; history is not silently rotated.",
                    }
                )
        elif contract is not None:
            policy = WorkPolicy(scope=contract.scope, constraints=contract.constraints)
        require_review_context_capture(review_id, review_stage, project_context_id)
        settings = resolve_execution(
            execution,
            previous=previous_execution,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        question_timeout_seconds = question_timeout_seconds or 300
        roots = tuple(
            dict.fromkeys(
                (self.scope.root, *(Path(root).resolve() for root in granted_roots))
            )
        )
        cwd = self.scope.validate_cwd(cwd, allowed_roots=roots)
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
        # Managed declarations come from the authenticated reservation, never its prose.
        requirements = (
            TaskRequirements.model_validate(attempt.get("requirements") or {})
            if attempt
            else contract.requirements
            if contract
            else TaskRequirements()
        )
        verification = verification_requirements(
            (
                attempt.get("verification")
                if attempt
                else contract.verification
                if contract
                else None
            )
            or {}
        )
        needs_shell = requirements.requires_shell or verification["requires_shell"]
        if attempt:
            shell = shell_permission(attempt) and not (
                attempt["kind"] == "review"
                and attempt.get("protocol") == "independent_first"
            )
            # A separate code-owned executor satisfies this declaration; the
            # model still has only the pinned reader and work protocol.
            shell = shell or has_review_runner(attempt)
            write = attempt["kind"] == "implement" and attempt["allow_work"]
        else:
            shell = write = mode == "work"
        if needs_shell and not shell:
            raise ValueError(
                "Declared shell requirements need work mode and an explicit shell grant for managed attempts; no worker launched"
            )
        if requirements.requires_write and not write:
            raise ValueError(
                "Declared write requirements need work mode and an explicit write grant for managed attempts; no worker launched"
            )
        if verification["estimated_seconds"] > settings["effective"]["timeout_seconds"]:
            raise ValueError(
                "Selected verification estimates including preparation exceed the whole task timeout"
            )
        if not verification["estimates_complete"]:
            diagnostics.append(
                {
                    "code": "verification_estimate_incomplete",
                    "message": "Unestimated checks remain unknown; the whole timeout is not proof of feasibility.",
                }
            )
        observations = []
        grouped = {}
        for value in (*requirements.entry_paths, *requirements.boundary_paths):
            path = Path(value)
            path = cwd / path
            candidates = [root for root in roots if path.is_relative_to(root)]
            if not candidates:
                raise ValueError(
                    f"Declared path {value!r} is outside the allowed roots"
                )
            root = max(candidates, key=lambda item: len(item.parts))
            if path == root:
                raise ValueError("Declare a file or boundary path below its Git root")
            grouped.setdefault(root, []).append(path.relative_to(root).as_posix())
        for root, paths in grouped.items():
            observations.append(
                observe_declared_paths(root, paths, action="task_admission")
            )
        for identifier in requirements.live_path_evidence:
            self.artifacts.info(identifier)
        if handoff:
            for identifier in handoff.evidence_artifact_ids:
                info = self.artifacts.info(identifier)
                if info.get("conversation_id") != source_id and not (
                    previous_context_id
                    and info.get("context_id") == previous_context_id
                ):
                    raise ValueError(
                        "Handoff evidence must belong to the source conversation or its pinned context"
                    )
        now = time.time()
        record = {
            "task_id": task_id,
            "conversation_id": destination,
            "created": now,
            "updated": now,
            "cwd": str(cwd),
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
            "review_run_id": review_run_id,
            "project_context_id": project_context_id,
            "previous_project_context_id": previous_context_id
            if previous_context_id != project_context_id
            else None,
            "question_timeout_seconds": question_timeout_seconds,
            "event_history_limit": MAX_EVENT_HISTORY,
            "workspace_roots": json.dumps([str(root) for root in roots]),
            "continuation": continuation,
            "previous_task_id": previous["task_id"] if previous else None,
            "previous_conversation_id": source_id,
            "handoff_json": handoff.model_dump_json() if handoff else None,
            "context_unchanged": int(
                following and not fresh and previous_context_id == project_context_id
            ),
        }
        if review_run_id is not None:
            self.tasks.validate_review_reservation(record)
            if verification["estimated_seconds"] > record["deadline"] - now:
                raise ValueError(
                    "Selected verification estimates exceed the remaining review-run timeout"
                )
        if len(self.messages.build(record, snapshot).encode("utf-8")) > 200_000:
            raise ValueError(
                "Resolved task and product context exceed 200000 UTF8 bytes; move details into artifacts"
            )
        for identifier in contract.artifact_ids if contract else ():
            self.artifacts.info(identifier)
        owned = owned_paths(cwd, policy)
        self.tasks.check_admission(record, owned)
        if self.closing:
            raise ValueError("MCP server is shutting down")
        result = {
            "admissible": True,
            "reservation": False,
            "cwd": str(cwd),
            "mode": mode,
            "continuation": continuation,
            "previous_task_id": record["previous_task_id"],
            "previous_conversation_id": source_id,
            "execution": settings,
            "requirements": requirements.model_dump(),
            "verification": verification,
            "repository_observations": observations,
            "live_path_evidence": {
                "artifact_ids": requirements.live_path_evidence,
                "status": "attributed_evidence_not_execution_proof",
            },
            "diagnostics": diagnostics,
        }
        if snapshot is not None:
            result["project_context"] = {
                key: value for key, value in snapshot.items() if key != "context"
            }
        return {
            "record": record,
            "owned": owned,
            "settings": settings,
            "snapshot": snapshot,
            "preflight": result,
        }

    def _check_continuation(self, history, *, fresh):
        identifiers = {task["task_id"] for task in history}
        for task in history:
            if task["status"] in {"starting", "running", "waiting_input", "cancelling"}:
                raise ValueError(
                    "Conversation has active or unrecovered work; inspect its result and use report-only reconciliation before continuing"
                )
            if task["mode"] == "work" and task["status"] in {
                "interrupted",
                "cancelled",
                "failed",
            }:
                raise ValueError(
                    "Prior work has uncertain effects; fresh cannot replay it. Inspect immutable evidence and use report-only reconciliation"
                )
            stop = json.loads(task.get("execution_json") or "{}").get("stop", {})
            if stop.get("classification") == "provider_policy_refusal":
                raise ValueError(
                    "Provider policy stopped this conversation; fresh/resume cannot retry it. Use report-only reconciliation, not automatic work"
                )
            if fresh and task.get("review_id"):
                raise ValueError(
                    "Fresh cannot carry review disclosure authority; create a separate independent review"
                )
            if (
                self.worker.work_items is not None
                and self.worker.work_items.native_attempt(task["task_id"])
            ):
                raise ValueError(
                    "Bound managed conversations cannot resume or transfer grants; use the work item's reconciliation and explicit assignment path"
                )
        if self.worker.work_items is not None:
            for attempt in self.worker.work_items.active_attempts():
                binding = attempt.get("binding") or {}
                if (
                    attempt.get("native_task_id") in identifiers
                    or binding.get("task_id") in identifiers
                    or binding.get("conversation_id") == history[0]["conversation_id"]
                ):
                    raise ValueError(
                        "Conversation has active claims or recovery-required work; use report-only reconciliation before continuing"
                    )

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
