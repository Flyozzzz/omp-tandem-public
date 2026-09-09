"""Composition facade for the project-scoped tandem runtime."""

from pathlib import Path
from uuid import UUID

from . import migration
from .artifacts import ArtifactStore
from .channel import ChannelDelivery
from .context_transfer import ContextTransfer
from .native_worker import NativeWorker
from .project_context import ProjectContextStore
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
        )
        self.migration = (
            migration.migrate_legacy(self.scope, database)
            if migrate_legacy
            else {"status": "disabled"}
        )
        self.tasks = TaskStore(self.scope, self.channel)
        messages = TaskMessages(self.scope, self.projects)
        self.interaction = TaskInteraction(self.tasks, self.artifacts, self.projects)
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

    def start(
        self,
        prompt=None,
        cwd=None,
        mode="analyze",
        conversation_id=None,
        timeout_seconds=1800,
        contract=None,
        project_context_id=None,
        question_timeout_seconds=None,
        granted_roots=(),
    ):
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
        )

    def view(self, task_id, details=False):
        return self.results.view(task_id, details)

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
        self.runtime.shutdown()
