"""A card's Git repository is agreed in the plan, not inherited from the launch."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from omp_tandem.errors import ProtocolError
from omp_tandem.work_adapters import _require_root_repository
from omp_tandem.work_items import (
    WorkCommand,
    WorkPlan,
    WorkStore,
    _operation_fingerprint,
    _repository_selector,
    review_inputs,
)
from omp_tandem.work_workspace import WorkWorkspace
from omp_tandem.workspace import resolve_scope
from tests.test_work_items import plan

CHILD = "_repos/main-pipeline"


def _git(root, *args):
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _repository(path: Path, marker: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "shared.txt").write_text(marker, encoding="utf-8")
    _git(path, "add", "shared.txt")
    _git(path, "commit", "-qm", f"{marker} base")


class RepositorySelectorTests(unittest.TestCase):
    def test_null_selects_the_launch_root(self):
        self.assertIsNone(_repository_selector(None))
        self.assertIsNone(WorkPlan.model_validate(plan()).repository)

    def test_one_spelling_only(self):
        for value in ("", ".", "..", "/abs", "a/../b", "a//b", "a/./b", "x\\y", "./a"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _repository_selector(value)
        for value in (".git", "a/.git", "A/.GIT"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _repository_selector(value)
        self.assertEqual(_repository_selector(CHILD), CHILD)


class SelectedRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        _repository(self.root, "parent")
        (self.root / ".gitignore").write_text("_repos/\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-qm", "ignore nested checkouts")
        self.child = self.root / CHILD
        _repository(self.child, "child")
        self.plain = self.root / "docs"
        self.plain.mkdir()
        self.state = Path(temporary.name).resolve() / "state"
        self.scope = resolve_scope(self.state, self.root)
        self.store = WorkStore(self.scope.directory / "tasks.sqlite3", self.scope)

    # -- helpers ----------------------------------------------------------

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

    def view(self, work_id):
        return self.store.perform({"action": "get", "work_id": work_id}, actor="claude")

    def change(self, work_id, action, *, actor="claude", **fields):
        return self.store.perform(
            {
                "action": action,
                "work_id": work_id,
                "expected_revision": self.view(work_id)["revision"],
                "operation_id": str(uuid4()),
                **fields,
            },
            actor=actor,
        )

    def agreed(self, **values):
        created = self.create(**values)
        work_id = created["work_id"]
        self.change(work_id, "agree", actor="claude")
        self.change(work_id, "agree", actor="omp")
        return work_id

    def revise(self, work_id, new_plan):
        self.change(work_id, "propose", plan=new_plan)
        view = self.view(work_id)
        return self.store.transition_activate(
            work_id,
            note="Operator activates the agreed repository",
            proposal_id=view["proposal"]["proposal_id"],
        )

    def operator(self, *args):
        """The real operator entry point, as an operator actually runs it."""
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "omp_tandem.work_daemon",
                "--project-root",
                str(self.root),
                "--state-dir",
                str(self.state),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def head(self, root):
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    # -- selection --------------------------------------------------------

    def test_a_card_records_the_repository_its_plan_chose(self):
        work_id = self.create(repository=CHILD)["work_id"]
        card = self.view(work_id)
        self.assertEqual(card["plan"]["repository"], CHILD)
        self.assertEqual(card["repository"]["selected_directory"], CHILD)
        # The data namespace is still the launch project, unchanged.
        self.assertEqual(card["repository"]["project_root"], str(self.root))
        self.assertEqual(card["repository_observation"]["head"], self.head(self.child))

    def test_an_ordinary_subdirectory_is_not_a_repository(self):
        with self.assertRaises(ValueError) as refusal:
            self.create(repository="docs")
        self.assertIn("differs from detected Git repository", str(refusal.exception))

    def test_a_missing_directory_is_refused_at_creation(self):
        with self.assertRaises(ValueError):
            self.create(repository="_repos/absent")

    def test_the_launch_root_stays_the_default(self):
        card = self.view(self.create()["work_id"])
        self.assertIsNone(card["plan"]["repository"])
        self.assertIsNone(card["repository"]["selected_directory"])
        self.assertEqual(card["repository_observation"]["head"], self.head(self.root))

    def test_declared_paths_resolve_in_the_selected_repository(self):
        # The same relative name exists in both checkouts with different bytes.
        contract = plan()
        contract["steps"][0]["owned_files"] = ["shared.txt"]
        work_id = self.store.perform(
            {
                "action": "create",
                "plan": contract | {"repository": CHILD},
                "expected_revision": 0,
                "operation_id": str(uuid4()),
            },
            actor="claude",
        )["work_id"]
        observation = self.view(work_id)["repository_observation"]
        self.assertEqual(observation["status"], "verified")
        self.assertEqual(observation["git_toplevel"], str(self.child))
        self.assertEqual(observation["selected_directory"], CHILD)

    def test_a_root_card_still_refuses_a_path_inside_a_child_checkout(self):
        contract = plan()
        contract["steps"][0]["owned_files"] = [f"{CHILD}/shared.txt"]
        with self.assertRaises(ValueError) as refusal:
            self.store.perform(
                {
                    "action": "create",
                    "plan": contract,
                    "expected_revision": 0,
                    "operation_id": str(uuid4()),
                },
                actor="claude",
            )
        self.assertIn("repository boundary", str(refusal.exception))

    # -- agreement and the lock -------------------------------------------

    def test_both_participants_may_change_it_before_execution(self):
        work_id = self.agreed()
        self.revise(work_id, plan() | {"repository": CHILD})
        card = self.view(work_id)
        self.assertEqual(card["plan"]["repository"], CHILD)
        self.assertEqual(card["repository"]["selected_directory"], CHILD)
        # The old agreements did not survive the new plan revision.
        self.assertEqual(card["agreements"], {})

    def test_a_claim_fixes_the_repository_for_good(self):
        work_id = self.agreed(repository=CHILD)
        self.change(work_id, "claim", actor="omp", step_id="backend")
        lock = self.view(work_id)["repository_lock"]
        self.assertEqual(lock["selected_directory"], CHILD)
        self.assertEqual(lock["basis"], "first claim reservation")
        with self.assertRaises(ProtocolError) as refusal:
            self.change(work_id, "propose", plan=plan())
        self.assertEqual(refusal.exception.envelope["code"], "work_repository_locked")

    def test_the_lock_survives_a_cancelled_attempt(self):
        work_id = self.agreed(repository=CHILD)
        self.change(work_id, "claim", actor="omp", step_id="backend")
        self.store.revoke(work_id)
        with self.assertRaises(ProtocolError):
            self.change(work_id, "propose", plan=plan())

    def test_a_card_with_history_but_no_lock_field_counts_as_locked(self):
        # An older card recorded no lock; absence must not read as "still open".
        work_id = self.agreed()
        self.change(work_id, "claim", actor="omp", step_id="backend")
        with self.store._transaction() as db:
            card = self.store._load(db, work_id)
            del card["repository_lock"]
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), work_id),
            )
        with self.assertRaises(ProtocolError):
            self.change(work_id, "propose", plan=plan() | {"repository": CHILD})

    def test_an_untouched_card_is_not_locked(self):
        work_id = self.agreed()
        self.assertIsNone(self.view(work_id)["repository_lock"])

    def test_a_manual_claim_pins_the_selected_repository_head(self):
        work_id = self.agreed(repository=CHILD)
        claim = self.change(work_id, "claim", actor="omp", step_id="backend")
        attempt = self.store.attempt(claim["claim"]["attempt_id"])
        self.assertEqual(attempt["source_commit"], self.head(self.child))
        self.assertNotEqual(attempt["source_commit"], self.head(self.root))
        self.assertEqual(attempt["repository"], CHILD)

    # -- refusals this release carries ------------------------------------

    def test_unattended_authorization_is_refused_without_persisting_a_grant(self):
        work_id = self.agreed(repository=CHILD)
        with self.assertRaises(ProtocolError) as refusal:
            self.store.authorize(
                work_id,
                budget_seconds=600,
                max_launches=1,
                max_cost_usd=1.0,
                allow_work=True,
                allow_shell=False,
            )
        envelope = refusal.exception.envelope
        self.assertEqual(envelope["code"], "work_repository_managed_unsupported")
        card = self.view(work_id)
        self.assertIsNone(card["authorization"])
        self.assertIsNone(card["repository_lock"])

    def test_managed_workspaces_refuse_a_selected_repository(self):
        workspace = WorkWorkspace(self.scope, repository=CHILD)
        attempt = {"attempt_id": str(uuid4()), "repository": CHILD}
        for call in (
            lambda: workspace.prepare(attempt, plan(), []),
            lambda: workspace.finish(attempt, plan(), {"path": "x"}),
            lambda: workspace.validate_submission(attempt, plan()),
            lambda: workspace.verify_review(attempt, plan(), {"path": "x"}),
            lambda: workspace.prepare_verification(attempt, Path("x")),
        ):
            with self.assertRaises(ValueError) as refusal:
                call()
            self.assertIn("not supported", str(refusal.exception))

    def test_a_default_workspace_cannot_adopt_a_selected_attempt(self):
        # Omitting the selector must not redirect the attempt to the launch root.
        attempt = {"attempt_id": str(uuid4()), "repository": CHILD}
        with self.assertRaises(ValueError) as refusal:
            WorkWorkspace(self.scope).adopt_submission(attempt, plan(), "0" * 40)
        message = str(refusal.exception)
        self.assertIn("binding differs", message)
        # The refusal has to say which repository, and what to do about it.
        self.assertIn(CHILD, message)
        self.assertIn("the launch root", message)
        self.assertIn("operator CLI", message)

    def test_operator_transition_preserves_a_manual_child_commit(self):
        contract = plan()
        contract["steps"][0]["owned_files"] = ["shared.txt"]
        work_id = self.store.perform(
            {
                "action": "create",
                "plan": contract | {"repository": CHILD},
                "expected_revision": 0,
                "operation_id": str(uuid4()),
            },
            actor="claude",
        )["work_id"]
        self.change(work_id, "agree", actor="claude")
        self.change(work_id, "agree", actor="omp")
        claim = self.change(work_id, "claim", actor="omp", step_id="backend")
        attempt_id = claim["claim"]["attempt_id"]
        (self.child / "shared.txt").write_text("child edited", encoding="utf-8")
        _git(self.child, "add", "shared.txt")
        _git(self.child, "commit", "-qm", "manual step work")
        saved = self.head(self.child)
        revised = contract | {"repository": CHILD}
        revised["constraints"] = ["Do not change user branch", "Added constraint"]
        self.change(work_id, "propose", plan=revised)
        self.store.transition_begin(
            work_id,
            note="Operator begins the transition",
            proposal_id=self.view(work_id)["proposal"]["proposal_id"],
        )
        # The card's own repository holds the commit; a default workspace cannot
        # reach it, which is exactly what the binding refusal has to say.
        with self.assertRaises(ValueError) as refusal:
            self.store.transition_resolve(
                work_id,
                attempt_id,
                note="Preserve the manual commit",
                evidence=["Committed in the selected repository"],
                confirm_stopped=True,
                saved_commit=saved,
                transition_id=self.view(work_id)["transition"]["transition_id"],
                workspaces=WorkWorkspace(self.scope),
            )
        self.assertIn(CHILD, str(refusal.exception))
        # The operator command must itself choose the card's repository: this
        # fails again if work_daemon builds a default workspace.
        completed = self.operator(
            "transition",
            work_id,
            "resolve",
            "--attempt",
            attempt_id,
            "--transition",
            self.view(work_id)["transition"]["transition_id"],
            "--confirm-stopped",
            "--saved-commit",
            saved,
            "--note",
            "Preserve the manual commit",
            "--evidence",
            "Committed in the selected repository",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        entry = json.loads(completed.stdout)["transition"]["inventory"][attempt_id]
        self.assertEqual(entry["disposition"]["kind"], "superseded")
        self.assertEqual(entry["saved"]["preserved"]["commit"], saved)
        # The parent checkout was never touched by preserving the child's work.
        self.assertNotEqual(saved, self.head(self.root))

    def test_automatic_capture_refuses_a_selected_repository(self):
        with self.assertRaises(ValueError):
            _require_root_repository(plan() | {"repository": CHILD})
        self.assertIsNone(_require_root_repository(plan()))

    def test_a_team_task_cannot_execute_a_selected_card(self):
        work_id = self.agreed(repository=CHILD)
        with self.assertRaises(ProtocolError):
            self.store.require_team_execution(work_id)
        root_card = self.agreed()
        self.assertIsNone(self.store.require_team_execution(root_card))

    def test_a_manual_submission_resolves_in_the_selected_repository(self):
        from omp_tandem.work_access import perform_work

        contract = plan()
        contract["steps"][0]["owned_files"] = ["shared.txt"]
        work_id = self.store.perform(
            {
                "action": "create",
                "plan": contract | {"repository": CHILD},
                "expected_revision": 0,
                "operation_id": str(uuid4()),
            },
            actor="claude",
        )["work_id"]
        self.change(work_id, "agree", actor="claude")
        self.change(work_id, "agree", actor="omp")
        claim = self.change(work_id, "claim", actor="omp", step_id="backend")
        token = claim["claim"]["token"]
        (self.child / "shared.txt").write_text("child edited", encoding="utf-8")
        _git(self.child, "add", "shared.txt")
        _git(self.child, "commit", "-qm", "manual step work")
        submitted = perform_work(
            self.store,
            {
                "action": "submit",
                "work_id": work_id,
                "step_id": "backend",
                "expected_revision": self.view(work_id)["revision"],
                "operation_id": str(uuid4()),
                "commit": self.head(self.child),
                "note": "Committed in the selected repository",
                "evidence": ["shared.txt changed in the child checkout"],
            },
            actor="omp",
            attempt_token=token,
        )
        step = next(item for item in submitted["steps"] if item["id"] == "backend")
        # The submission was adopted from the child checkout and now waits for
        # the declared reviewer, which is the supported manual path.
        self.assertEqual(step["state"], "review")
        # The card records the exact child commit it adopted, nothing else.
        self.assertIn(self.head(self.child), json.dumps(step))
        self.assertNotIn(self.head(self.root), json.dumps(step))
        # A commit that exists only in the parent is not this card's evidence.
        (self.root / "shared.txt").write_text("parent edited", encoding="utf-8")
        _git(self.root, "add", "shared.txt")
        _git(self.root, "commit", "-qm", "unrelated parent work")
        saved = self.store.attempt(claim["claim"]["attempt_id"])
        attempt = {
            "attempt_id": saved["attempt_id"],
            "repository": CHILD,
            "source_commit": saved["source_commit"],
        }
        self.assertNotEqual(saved["source_commit"], self.head(self.root))
        with self.assertRaises(ValueError):
            WorkWorkspace(self.scope, repository=CHILD).adopt_submission(
                attempt, contract, self.head(self.root)
            )

    def test_apply_fast_forwards_the_selected_checkout_only(self):
        expected = self.head(self.child)
        (self.child / "shared.txt").write_text("accepted result", encoding="utf-8")
        _git(self.child, "add", "shared.txt")
        _git(self.child, "commit", "-qm", "accepted output")
        commit = self.head(self.child)
        tree = subprocess.run(
            ["git", "-C", str(self.child), "rev-parse", f"{commit}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _git(self.child, "reset", "-q", "--hard", expected)
        parent_head = self.head(self.root)
        result = WorkWorkspace(self.scope, repository=CHILD).apply(
            {"commit": commit, "tree_hash": tree}, expected
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["project_root"], str(self.child))
        self.assertEqual(self.head(self.child), commit)
        self.assertEqual(self.head(self.root), parent_head)
        self.assertEqual(
            (self.child / "shared.txt").read_text(encoding="utf-8"), "accepted result"
        )
        # The parent's own same-named file is untouched by the child's result.
        self.assertEqual(
            (self.root / "shared.txt").read_text(encoding="utf-8"), "parent"
        )

    # -- compatibility -----------------------------------------------------

    def test_an_absent_selection_keeps_the_historical_receipt(self):
        def fingerprint(contract, version):
            return _operation_fingerprint(
                WorkCommand.model_validate(
                    {
                        "action": "create",
                        "plan": contract,
                        "expected_revision": 0,
                        "operation_id": "create",
                    }
                ),
                None,
                version=version,
            )

        for version in (1, 2):
            with self.subTest(version=version):
                self.assertEqual(
                    fingerprint(plan(), version),
                    fingerprint(plan() | {"repository": None}, version),
                )
                self.assertNotEqual(
                    fingerprint(plan() | {"repository": CHILD}, version),
                    fingerprint(plan(), version),
                )
                self.assertNotEqual(
                    fingerprint(plan() | {"repository": CHILD}, version),
                    fingerprint(plan() | {"repository": "_repos/other"}, version),
                )

    def test_the_same_operation_id_with_another_repository_conflicts(self):
        from omp_tandem.work_items import WorkConflict

        operation = str(uuid4())
        request = {
            "action": "create",
            "plan": plan() | {"repository": CHILD},
            "expected_revision": 0,
            "operation_id": operation,
        }
        first = self.store.perform(request, actor="claude")
        self.assertEqual(self.store.perform(request, actor="claude"), first)
        with self.assertRaises(WorkConflict):
            self.store.perform({**request, "plan": plan()}, actor="claude")

    def test_review_inputs_translate_only_for_a_selected_repository(self):
        contract = plan()
        contract["steps"][0]["review_context_paths"] = ["callers.py"]
        attempt = {
            "step_id": "backend",
            "plan_revision": 1,
            "submission": {"base_commit": "a" * 40, "commit": "b" * 40},
        }
        root = review_inputs(attempt, contract)
        self.assertEqual(root["context_paths"], ["callers.py"])
        self.assertNotIn("review_directory", root)
        selected = review_inputs(attempt, contract | {"repository": CHILD})
        self.assertEqual(selected["review_directory"], CHILD)
        self.assertEqual(selected["context_paths"], [f"{CHILD}/callers.py"])


if __name__ == "__main__":
    unittest.main()
