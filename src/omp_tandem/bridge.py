"""Composition facade for the project-scoped tandem runtime."""

from contextlib import closing
from pathlib import Path
from uuid import UUID

from . import migration
from .artifacts import ArtifactStore
from .channel import ChannelDelivery
from .context_transfer import ContextTransfer
from .diagnostics import Diagnostics
from .findings import FindingStore
from .native_worker import NativeWorker
from .project_context import ProjectContextStore
from .receipts import ReceiptStore
from .review_runs import ReviewRuns
from .reviews import ReviewStore
from .runtime_models import ACTIVE
from .task_contracts import TaskMessages
from .task_interaction import TaskInteraction
from .task_results import TaskResults
from .task_runtime import TaskRuntime
from .task_store import TaskStore, initialize_database
from .workspace import WorkerSlots, resolve_scope


class Bridge:
    def __init__(
        self,
        state_dir: Path,
        executable: str,
        model: str | None,
        *,
        channel_enabled=True,
        webhook_enabled=True,
        webhook_port=0,
        project_root: Path | None = None,
        project_source: str | None = None,
        migrate_legacy=True,
    ):
        self.scope = resolve_scope(state_dir, project_root, source=project_source)
        slots = WorkerSlots(self.scope.base)
        # Identity validation must precede every store that can write to the DB.
        database = initialize_database(self.scope)
        self.artifacts = ArtifactStore(database)
        self.projects = ProjectContextStore(database)
        self.transfers = ContextTransfer(self.scope, self.projects, self.artifacts)
        self.channel = ChannelDelivery(
            database,
            enabled=channel_enabled,
            webhook_enabled=webhook_enabled,
            webhook_port=webhook_port,
            project_root=self.scope.root,
        )
        self.migration = (
            migration.migrate_legacy(self.scope, database)
            if migrate_legacy
            else {"status": "disabled"}
        )
        self.tasks = TaskStore(self.scope, self.channel)
        self.reviews = ReviewStore(database, self.scope, self.artifacts)
        self.findings = FindingStore(database)
        self.receipts = ReceiptStore(database)
        messages = TaskMessages(self.scope, self.projects, self.reviews)
        self.interaction = TaskInteraction(
            self.tasks, self.artifacts, self.projects, self.findings
        )
        self.results = TaskResults(self.tasks, self.artifacts, self.projects)
        worker = NativeWorker(
            self.tasks, self.artifacts, self.interaction, messages, executable
        )
        self.runtime = TaskRuntime(
            self.tasks,
            worker,
            messages,
            self.artifacts,
            self.projects,
            self.scope,
            slots,
            model,
        )
        self.diagnostics = Diagnostics(self)
        self.review_runs = ReviewRuns(
            self.tasks,
            self.reviews,
            self.start,
            self.view,
            self.reply,
            self.cancel,
            self.channel.owner,
        )

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
        execution=None,
        review_id=None,
        review_stage=None,
        reserved_task_id=None,
        review_run_id=None,
    ):
        previous = (
            self.tasks.latest(conversation_id) if conversation_id is not None else None
        )
        if previous is not None:
            effective_mode = previous["mode"]
            effective_review = (
                review_id if review_id is not None else previous.get("review_id")
            )
            effective_stage = review_stage
            if effective_stage is None and effective_review == previous.get(
                "review_id"
            ):
                effective_stage = previous.get("review_stage")
        else:
            effective_mode, effective_review, effective_stage = (
                mode,
                review_id,
                review_stage,
            )
        if effective_review is not None:
            self.reviews.info(effective_review)
            if effective_mode != "think":
                raise ValueError(
                    "Snapshot review requires think mode and the saved-material reader; start a separate work conversation for live edits"
                )
            effective_stage = effective_stage or "independent"
            if effective_stage not in ("independent", "comparison"):
                raise ValueError("review_stage must be independent or comparison")
            if effective_stage == "comparison":
                with closing(self.tasks.connect()) as db:
                    independent = db.execute(
                        "SELECT 1 FROM tasks WHERE conversation_id=? AND review_id=? "
                        "AND review_stage='independent' AND status='completed' LIMIT 1",
                        (conversation_id, effective_review),
                    ).fetchone()
                if independent is None:
                    raise ValueError(
                        "Read a completed independent assessment of this snapshot in this conversation before revealing the author proposal"
                    )
        elif effective_stage is not None:
            raise ValueError("review_stage requires a review_id")
        return self.runtime.start(
            prompt,
            cwd,
            mode,
            conversation_id,
            timeout_seconds,
            contract,
            project_context_id,
            question_timeout_seconds,
            granted_roots,
            execution=execution,
            review_id=effective_review,
            review_stage=effective_stage,
            reserved_task_id=reserved_task_id,
            review_run_id=review_run_id,
        )

    def view(self, task_id, details=False, *, refresh=True):
        result = self.results.view(task_id, details, refresh=refresh)
        if result.get("review") and result["status"] not in ACTIVE:
            result["review"]["applicability"] = self.reviews.assess(
                result["review"]["review_id"]
            )
        if result["status"] not in ACTIVE:
            result["receipt"] = self.receipts.status(task_id)
            result["findings"] = self.findings.for_task(task_id)
            result["findings_next_offset"] = (
                50 if len(result["findings"]) == 50 else None
            )
        return result

    def recent(self, limit=20):
        return self.results.recent(limit)

    def wait_snapshot(self, task_ids):
        return self.results.wait_snapshot(task_ids)

    def reply(self, task_id, question_id, answer):
        return self.interaction.reply(task_id, question_id, answer)

    def cancel(self, task_id):
        self.tasks.cancel(task_id)
        return self.results.view(task_id)

    def publish(self, conversation_id, name, content, media_type="text/plain"):
        conversation_id = str(UUID(conversation_id))
        task = self.tasks.latest(conversation_id)
        if task is None:
            raise ValueError("Unknown conversation_id; start a conversation first")
        return self.artifacts.publish(
            conversation_id, task["task_id"], name, content, media_type
        )

    def shutdown(self):
        try:
            self.review_runs.close()
        finally:
            self.runtime.shutdown()
