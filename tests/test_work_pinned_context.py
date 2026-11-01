"""A shared plan pins one product snapshot; a brief must travel with the plan."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from omp_tandem.project_context import ProjectContext, ProjectContextStore
from omp_tandem.work_items import WorkPlan, WorkStore, _operation_fingerprint
from omp_tandem.workspace import resolve_scope
from tests.test_work_items import plan


class PinnedPlanContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        for command in (
            ["git", "init", "-q"],
            [
                "git",
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "base",
                "--allow-empty",
            ],
        ):
            subprocess.run(command, cwd=self.root, check=True)
        self.scope = resolve_scope(Path(temporary.name) / "state", self.root)
        self.database = self.scope.directory / "tasks.sqlite3"
        self.store = WorkStore(self.database, self.scope)
        self.contexts = ProjectContextStore(self.database)

    def publish(self):
        published = self.contexts.publish(
            ProjectContext(
                project_id="jedi",
                product_summary="The pinned brief the worker must honour.",
            ),
            publisher="claude",
        )
        return published["context_id"]

    def view(self, work_id):
        return self.store.perform({"action": "get", "work_id": work_id}, actor="claude")

    def create(self, **values):
        return self.store.perform(
            {
                "action": "create",
                "plan": plan() | values,
                "expected_revision": 0,
                "operation_id": str(uuid4()),
            },
            actor="claude",
        )

    def test_a_published_context_can_be_pinned_to_the_plan(self):
        context_id = self.publish()
        created = self.create(project_context_id=context_id)
        card = self.view(created["work_id"])
        self.assertEqual(card["plan"]["project_context_id"], context_id)

    def test_an_unknown_pinned_context_is_refused_before_any_work(self):
        with self.assertRaises(ValueError) as refusal:
            self.create(project_context_id=str(uuid4()))
        self.assertIn("unknown project context", str(refusal.exception))

    def test_a_plan_without_a_pinned_context_still_creates(self):
        created = self.create()
        card = self.view(created["work_id"])
        self.assertIsNone(card["plan"]["project_context_id"])

    def test_an_absent_pin_keeps_the_historical_operation_fingerprint(self):
        # Old receipts must replay identically after the field was added.
        from omp_tandem.work_items import WorkCommand

        command = WorkCommand.model_validate(
            {
                "action": "create",
                "plan": plan(),
                "expected_revision": 0,
                "operation_id": "create",
            }
        )
        payload = command.model_dump()
        self.assertIsNone(payload["plan"]["project_context_id"])
        fingerprint = _operation_fingerprint(command, None)
        pinned = WorkCommand.model_validate(
            {
                "action": "create",
                "plan": plan() | {"project_context_id": str(uuid4())},
                "expected_revision": 0,
                "operation_id": "create",
            }
        )
        self.assertNotEqual(_operation_fingerprint(pinned, None), fingerprint)

    def test_the_plan_model_defaults_the_pin_to_absent(self):
        self.assertIsNone(WorkPlan.model_validate(plan()).project_context_id)


if __name__ == "__main__":
    unittest.main()
