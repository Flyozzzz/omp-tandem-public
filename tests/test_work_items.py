"""Observable collaboration races, immutable acceptance, and execution boundaries."""

from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.work_items import WorkCommand, WorkConflict, WorkPlan, WorkStore
from omp_tandem.workspace import resolve_scope


def plan(*, parallel=False):
    steps = [
        {
            "id": "backend",
            "title": "Backend",
            "goal": "Implement backend",
            "owner": "omp",
            "reviewer": "claude",
            "owned_files": ["backend.py"],
            "depends_on": [],
            "acceptance": ["Backend observable behavior"],
        }
    ]
    if parallel:
        steps.append(
            {
                "id": "frontend",
                "title": "Frontend",
                "goal": "Implement frontend",
                "owner": "claude",
                "reviewer": "omp",
                "owned_files": ["frontend.py"],
                "depends_on": [],
                "acceptance": ["Frontend observable behavior"],
            }
        )
    steps.append(
        {
            "id": "integration",
            "title": "Integration",
            "goal": "Verify combined result",
            "owner": "claude",
            "reviewer": "omp",
            "owned_files": ["integration.py"],
            "depends_on": [step["id"] for step in steps],
            "acceptance": ["Whole feature works"],
        }
    )
    return {
        "title": "Shared feature",
        "goal": "Deliver independently reviewed output",
        "constraints": ["Do not change user branch"],
        "acceptance": ["Whole feature works"],
        "steps": steps,
    }


class WorkItemsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-qm",
                "base",
            ],
            check=True,
        )
        self.scope = resolve_scope(Path(temporary.name) / "state", self.root)
        self.store = WorkStore(self.scope.directory / "tasks.sqlite3", self.scope)
        self.work_id = self.store.perform(
            {
                "action": "create",
                "plan": plan(),
                "expected_revision": 0,
                "operation_id": "create",
            },
            actor="claude",
        )["work_id"]

    def view(self):
        return self.store.perform(
            {"action": "get", "work_id": self.work_id}, actor="claude"
        )

    def change(self, action, *, actor="claude", token=None, **fields):
        return self.store.perform(
            {
                "action": action,
                "work_id": self.work_id,
                "expected_revision": self.view()["revision"],
                "operation_id": str(uuid4()),
                **fields,
            },
            actor=actor,
            attempt_token=token,
        )

    def agreed(self, *, parallel=False):
        if parallel:
            self.change("propose", plan=plan(parallel=True))
        self.change("agree", actor="claude")
        self.change("agree", actor="omp")

    def authorize(self, **changes):
        return self.store.authorize(
            self.work_id,
            **{
                "budget_seconds": 300,
                "max_launches": 8,
                "max_cost_usd": 8.0,
                "allow_work": True,
                "allow_shell": False,
                **changes,
            },
        )

    def reserve(
        self, step="backend", *, actor="omp", kind="implement", autonomous=True
    ):
        return self.store.reserve(
            self.work_id,
            step,
            actor=actor,
            kind=kind,
            owner_id="daemon",
            autonomous=autonomous,
        )

    def output(self, attempt):
        workspace = str(self.scope.directory / "worktrees" / attempt["attempt_id"])
        self.store.started(attempt["attempt_id"], workspace=workspace)
        return {
            "commit": "a" * 40,
            "base_commit": attempt["source_commit"],
            "workspace": workspace,
            "tree_hash": "b" * 40,
            "changed_files": [attempt["step_id"] + ".py"],
        }

    def submit(self, attempt, *, cost=0.25):
        return self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="success",
            answer="Implemented observable contract",
            evidence=["Focused behavior exercised"],
            output=self.output(attempt),
            cost_usd=cost,
        )

    def verdict(self, attempt, *, action="accept"):
        return self.change(
            action,
            actor=attempt["actor"],
            token=attempt["token"],
            step_id=attempt["step_id"],
            submission_id=attempt["submission"]["submission_id"],
            note="Reviewed exact snapshot against all listed criteria",
            evidence=["Exact output behavior supports criteria"],
        )

    def finish_review(self, attempt):
        return self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="success",
            answer="Review complete",
            evidence=["Recorded attributed verdict"],
            output=None,
            cost_usd=0.25,
        )

    def test_authorized_models_survive_reopen_and_cannot_change_active_attempt(self):
        self.agreed()
        grant = self.authorize(
            claude_model="claude-opus-4-6", omp_model="provider/authorized"
        )["authorization"]
        self.store = WorkStore(self.store.database, self.scope)
        grant["claude_model"] = "mutated-response"
        attempt = self.reserve()
        self.assertEqual(attempt["claude_model"], "claude-opus-4-6")
        self.assertEqual(attempt["omp_model"], "provider/authorized")
        self.assertEqual(
            attempt["model_provenance"], {"claude": "explicit", "omp": "explicit"}
        )
        with self.assertRaises(ValueError):
            self.authorize(claude_model="sonnet", omp_model="other/model")
        saved = self.store.attempt(attempt["attempt_id"])
        self.assertEqual(saved["omp_model"], "provider/authorized")
        self.assertEqual(
            self.view()["authorization"]["claude_model"], "claude-opus-4-6"
        )

    def test_invalid_model_does_not_create_authorization(self):
        self.agreed()
        for key in ("claude_model", "omp_model"):
            for value in ("", " ", "model\nother", "--flag", 12):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        self.authorize(**{key: value})
                    self.assertIsNone(self.view()["authorization"])

    def test_legacy_grant_is_labelled_without_rewriting_or_omp_launch(self):
        self.agreed(parallel=True)
        self.authorize()
        with sqlite3.connect(self.store.database) as db:
            card = json.loads(
                db.execute(
                    "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
                ).fetchone()[0]
            )
            for key in ("claude_model", "omp_model", "model_provenance"):
                card["authorization"].pop(key)
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), self.work_id),
            )
        self.store = WorkStore(self.store.database, self.scope)
        view = self.view()
        self.assertEqual(
            view["authorization"]["model_provenance"],
            {"claude": "legacy_default", "omp": "legacy_unpinned"},
        )
        self.assertEqual(view["steps"][0]["state"], "ready")
        with self.assertRaisesRegex(ValueError, "reauthorize"):
            self.reserve()
        self.assertEqual(self.view()["authorization"]["launches"], 0)
        with sqlite3.connect(self.store.database) as db:
            saved = json.loads(
                db.execute(
                    "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
                ).fetchone()[0]
            )
        self.assertNotIn("claude_model", saved["authorization"])
        attempt = self.reserve("frontend", actor="claude")
        self.assertEqual(attempt["claude_model"], "sonnet")
        self.assertEqual(attempt["model_provenance"]["claude"], "legacy_default")

    def test_two_claims_have_one_winner_and_no_public_credentials(self):
        self.agreed()
        revision = self.view()["revision"]

        def claim(index):
            try:
                return self.store.perform(
                    {
                        "action": "claim",
                        "work_id": self.work_id,
                        "step_id": "backend",
                        "expected_revision": revision,
                        "operation_id": f"claim-{index}",
                    },
                    actor="omp",
                )
            except WorkConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        winner = next(result for result in results if result is not None)
        self.assertEqual(sum(result is not None for result in results), 1)
        token = winner["claim"]["token"]
        self.assertFalse(winner["claim"]["allow_work"])
        self.assertNotIn(token, json.dumps(self.view()))
        self.assertNotIn(
            token, json.dumps(self.change("heartbeat", actor="omp", token=token))
        )
        history = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="claude"
        )
        self.assertNotIn(token, json.dumps(history))
        with self.assertRaises(ValueError):
            self.change(
                "submit",
                actor="omp",
                step_id="backend",
                note="No credential",
                evidence=["Untrusted text"],
                commit="a" * 40,
            )

    def test_conflicting_revisions_and_idempotency_preserve_agreement(self):
        command = {
            "action": "agree",
            "work_id": self.work_id,
            "expected_revision": self.view()["revision"],
            "operation_id": "agreement",
        }
        first = self.store.perform(command, actor="claude")
        self.assertEqual(self.store.perform(command, actor="claude"), first)
        with self.assertRaises(WorkConflict):
            self.store.perform(
                {**command, "note": "Different operation"}, actor="claude"
            )
        with self.assertRaises(WorkConflict):
            self.store.perform({**command, "operation_id": "stale"}, actor="omp")
        self.change("agree", actor="omp")
        before = self.view()
        self.change("propose", plan=before["plan"])
        self.assertEqual(self.view()["plan_revision"], before["plan_revision"])
        self.assertEqual(self.view()["agreements"], before["agreements"])
        modified = copy.deepcopy(before["plan"])
        modified["goal"] = "Different substantive requirement"
        self.change("propose", plan=modified)
        self.assertEqual(self.view()["agreements"], {})
        self.assertEqual(self.view()["status"], "draft")

    def test_pending_review_never_wakes_dependency_before_success(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        pending = self.verdict(review)
        self.assertNotEqual(pending["steps"][0]["state"], "accepted")
        self.assertEqual(self.store.ready(self.work_id), [])
        self.assertEqual(self.store.attempt(review["attempt_id"])["state"], "reserved")
        accepted = self.finish_review(review)
        self.assertEqual(accepted["steps"][0]["state"], "accepted")
        self.assertEqual(
            self.store.ready(self.work_id),
            [
                {
                    "work_id": self.work_id,
                    "step_id": "integration",
                    "actor": "claude",
                    "kind": "implement",
                }
            ],
        )
        final = self.reserve("integration", actor="claude")
        self.assertEqual(
            final["dependencies"][0]["submission_id"],
            accepted["steps"][0]["submission"]["submission_id"],
        )
        self.submit(final)
        review = self.reserve("integration", actor="omp", kind="review")
        self.verdict(review)
        completed = self.finish_review(review)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_step_id"], "integration")
        self.assertEqual(completed["result"]["commit"], "a" * 40)
        self.assertEqual(
            completed["steps"][1]["acceptance"]["global_criteria_attested"],
            completed["plan"]["acceptance"],
        )

    def test_failed_review_after_verdict_requires_recovery_not_acceptance(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        self.verdict(review)
        result = self.store.finish_attempt(
            review["attempt_id"],
            outcome="failed",
            answer="Partial result",
            evidence=[],
            output=None,
            cost_usd=0.25,
            error="Child exited unsuccessfully",
        )
        self.assertEqual(result["steps"][0]["state"], "recovery_required")
        self.assertIsNone(result["steps"][0]["acceptance"])
        self.assertEqual(self.store.ready(self.work_id), [])

    def test_stale_submission_and_plan_tokens_cannot_accept(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        with self.assertRaises(WorkConflict):
            self.change(
                "accept",
                actor="claude",
                token=review["token"],
                step_id="backend",
                submission_id="older-output",
                note="Claim acceptance",
                evidence=["Old result"],
            )
        revised = self.view()["plan"]
        revised["steps"][0]["goal"] = "Revised backend"
        self.change("propose", plan=revised)
        with self.assertRaises(ValueError):
            self.verdict(review)
        self.assertEqual(self.view()["steps"][0]["state"], "recovery_required")
        self.assertIsNotNone(self.view()["authorization"]["revoked_at"])
        history = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="omp"
        )
        self.assertTrue(
            any(
                event["snapshot"]["steps"][0]["submission"]
                for event in history["events"]
            )
        )

    def test_pause_fences_outputs_and_operator_reconciliation_needs_stop_proof(self):
        self.agreed()
        self.authorize()
        attempt = self.reserve()
        output = self.output(attempt)
        self.change("pause", note="Operator inspection")
        with self.assertRaises(ValueError):
            self.store.authenticate(attempt["token"])
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="success",
            answer="Late output",
            evidence=["Output exists"],
            output=output,
            cost_usd=0.25,
        )
        self.assertIsNone(self.view()["steps"][0]["submission"])
        self.change("resume")
        for actor in ("claude", "omp", "operator"):
            with self.assertRaises(ValueError):
                self.change(
                    "reconcile",
                    actor=actor,
                    step_id="backend",
                    resolution="retry",
                    note="Try again",
                    evidence=["Unverified text"],
                )
        self.store.confirm_stopped(attempt["attempt_id"])
        self.change(
            "reconcile",
            actor="operator",
            step_id="backend",
            resolution="retry",
            note="Verified process is gone and inspected worktree",
            evidence=["Supervisor exit status"],
        )
        replacement = self.reserve()
        self.assertNotEqual(replacement["token"], attempt["token"])
        with self.assertRaises(ValueError):
            self.store.authenticate(attempt["token"])

    def test_expiration_and_owner_recovery_never_release_claim(self):
        self.agreed()
        attempt = self.reserve(autonomous=False)
        with patch(
            "omp_tandem.work_items.time.time", return_value=attempt["deadline"] + 1
        ):
            self.assertEqual(self.store.ready(self.work_id), [])
        self.assertEqual(self.view()["steps"][0]["state"], "recovery_required")
        with self.assertRaises(WorkConflict):
            self.reserve(autonomous=False)
        self.assertFalse(
            self.store.attempt(attempt["attempt_id"])["process_confirmed_gone"]
        )
        self.store.recover_owner("daemon")
        self.assertEqual(self.store.ready(self.work_id), [])

    def test_bound_worker_cannot_cross_step_project_role_or_grant_authority(self):
        self.agreed()
        attempt = self.reserve(autonomous=False)
        for fields in (
            {"action": "heartbeat", "step_id": "integration"},
            {"action": "agree"},
            {"action": "resume"},
            {"action": "reconcile", "resolution": "retry"},
            {"action": "get", "work_id": str(uuid4())},
        ):
            with self.assertRaises(ValueError):
                self.store.perform(
                    {
                        "work_id": self.work_id,
                        "expected_revision": self.view()["revision"],
                        "operation_id": str(uuid4()),
                        **fields,
                    },
                    actor="omp",
                    attempt_token=attempt["token"],
                )
        with self.assertRaises(ValueError):
            self.change("heartbeat", actor="claude", token=attempt["token"])
        for field, value in (
            ("actor", "operator"),
            ("allow_work", True),
            ("attempt_token", attempt["token"]),
            ("authorization", {}),
        ):
            with self.assertRaises(ValidationError):
                WorkCommand.model_validate({"action": "get", field: value})

    def test_parallel_envelopes_and_unknown_cost_stop_whole_work(self):
        self.agreed(parallel=True)
        self.authorize(max_launches=2, max_cost_usd=2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.reserve),
                pool.submit(self.reserve, "frontend", actor="claude"),
            ]
            attempts = [future.result() for future in futures]
        self.assertEqual(sum(item["reserved_cost_usd"] for item in attempts), 2)
        self.assertEqual(self.view()["authorization"]["launches"], 2)
        result = self.submit(attempts[0], cost=None)
        self.assertTrue(result["authorization"]["unknown_cost"])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(
            self.store.attempt(attempts[1]["attempt_id"])["state"], "recovery_required"
        )
        self.assertEqual(self.store.ready(self.work_id), [])

    def test_no_grant_no_launch_and_consumed_caps_stay_consumed(self):
        self.agreed()
        with self.assertRaises(ValueError):
            self.reserve()
        self.authorize(max_launches=1, max_cost_usd=1)
        attempt = self.reserve()
        result = self.submit(attempt, cost=1)
        self.assertEqual(result["authorization"]["used_cost_usd"], 1)
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="success",
            answer="Implemented observable contract",
            evidence=["Focused behavior exercised"],
            output=self.store.attempt(attempt["attempt_id"])["output"],
            cost_usd=1,
        )
        self.assertEqual(self.view()["authorization"]["used_cost_usd"], 1)
        with self.assertRaises(WorkConflict):
            self.store.finish_attempt(
                attempt["attempt_id"],
                outcome="success",
                answer="Changed result",
                evidence=["Different"],
                output=None,
                cost_usd=0,
            )

    def test_manual_verdict_completes_claim_and_replay_has_no_new_authority(self):
        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        review = self.reserve(actor="claude", kind="review", autonomous=False)
        command = {
            "action": "accept",
            "work_id": self.work_id,
            "step_id": "backend",
            "expected_revision": self.view()["revision"],
            "operation_id": "manual-accept",
            "submission_id": review["submission"]["submission_id"],
            "note": "Attached reviewer attests exact criteria",
            "evidence": ["Reviewed immutable output"],
        }
        result = self.store.perform(
            command, actor="claude", attempt_token=review["token"]
        )
        self.assertEqual(result["steps"][0]["state"], "accepted")
        self.assertEqual(
            self.store.perform(command, actor="claude", attempt_token=review["token"]),
            result,
        )
        with self.assertRaises(ValueError):
            self.store.authenticate(review["token"])
        self.assertIsNone(self.view()["authorization"])

    def test_blocker_resolution_requires_author_and_evidence(self):
        self.agreed()
        blocked = self.change(
            "block",
            actor="omp",
            step_id="backend",
            note="Missing input",
            condition="Operator supplies schema",
        )
        blocker_id = blocked["steps"][0]["blockers"][0]["blocker_id"]
        self.assertEqual(self.store.ready(self.work_id), [])
        with self.assertRaises(ValueError):
            self.change(
                "unblock",
                actor="claude",
                step_id="backend",
                blocker_id=blocker_id,
                resolution="Guessed schema",
                evidence=["Guess"],
            )
        self.change(
            "unblock",
            actor="omp",
            step_id="backend",
            blocker_id=blocker_id,
            resolution="Schema provided",
            evidence=["schema.json"],
        )
        self.assertEqual(self.store.ready(self.work_id)[0]["step_id"], "backend")

    def test_event_cursor_survives_other_store_and_credential_echo_is_redacted(self):
        self.agreed()
        attempt = self.reserve(autonomous=False)
        cursor = self.store.event_head()
        self.change(
            "heartbeat",
            actor="omp",
            token=attempt["token"],
            note="Accidental echo " + attempt["token"],
        )
        observer = WorkStore(self.store.database, self.scope)
        events = observer.events(after=cursor)
        self.assertEqual([event["kind"] for event in events], ["heartbeat"])
        self.assertGreater(events[0]["event_id"], cursor)
        history = observer.perform(
            {"action": "history", "work_id": self.work_id}, actor="claude"
        )
        self.assertNotIn(attempt["token"], json.dumps(history))
        self.assertEqual(observer.events(after=events[0]["event_id"]), [])

    def test_foreign_database_identity_is_checked_before_writes(self):
        with sqlite3.connect(self.store.database) as db:
            db.execute("UPDATE bridge_scope SET scope_id='foreign'")
        with self.assertRaises(ValueError):
            self.change("agree")
        with self.assertRaises(ValueError):
            WorkStore(self.store.database, self.scope)
        with sqlite3.connect(self.store.database) as db:
            self.assertEqual(
                db.execute("SELECT scope_id FROM bridge_scope").fetchone()[0], "foreign"
            )

    def test_plan_rejects_cycles_escapes_ambiguous_ownership_and_missing_final(self):
        cases = []
        value = plan()
        value["steps"][0]["depends_on"] = ["integration"]
        cases.append(value)
        value = plan()
        value["steps"][0]["owned_files"] = ["../secret"]
        cases.append(value)
        value = plan(parallel=True)
        value["steps"][1]["owned_files"] = ["backend.py"]
        cases.append(value)
        value = plan()
        value["steps"][1]["depends_on"] = []
        cases.append(value)
        value = plan()
        value["steps"][1]["depends_on"] = ["missing"]
        cases.append(value)
        value = plan()
        value["steps"][0]["reviewer"] = "omp"
        cases.append(value)
        for value in cases:
            with self.subTest(plan=value), self.assertRaises(ValidationError):
                WorkPlan.model_validate(value)
        sequential = plan()
        sequential["steps"][1]["owned_files"] = ["backend.py"]
        accepted = WorkPlan.model_validate(sequential)
        self.assertEqual(accepted.steps[1].depends_on, ["backend"])

    def test_observers_do_not_acquire_writer_lock_even_when_expiry_is_due(self):
        self.agreed()
        attempt = self.reserve(autonomous=False)
        with sqlite3.connect(self.store.database, isolation_level=None) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                self.assertEqual(self.view()["work_id"], self.work_id)
                self.assertEqual(
                    self.store.authenticate(attempt["token"])["attempt_id"],
                    attempt["attempt_id"],
                )
                self.assertEqual(
                    self.store.active_attempts()[0]["attempt_id"], attempt["attempt_id"]
                )
                self.assertEqual(
                    self.store.attempt(attempt["attempt_id"])["state"], "reserved"
                )
                self.assertIsNone(self.store.native_attempt("not-bound"))
                cursor = self.store.event_head()
                self.assertEqual(self.store.events(after=cursor), [])
                with patch(
                    "omp_tandem.work_items.time.time",
                    return_value=attempt["deadline"] + 1,
                ):
                    self.assertEqual(self.store.ready(self.work_id), [])
                    self.assertEqual(
                        self.view()["steps"][0]["state"], "recovery_required"
                    )
                    with self.assertRaises(ValueError):
                        self.store.authenticate(attempt["token"])
            finally:
                writer.rollback()

    def test_cooperative_block_resumes_checkpoint_without_operator_recovery(self):
        self.agreed()
        self.authorize()
        attempt = self.reserve()
        output = self.output(attempt)
        blocked = self.change(
            "block",
            actor="omp",
            token=attempt["token"],
            step_id="backend",
            note="Need schema decision",
            condition="Approved schema supplied",
        )
        blocker_id = blocked["steps"][0]["blockers"][0]["blocker_id"]
        self.assertEqual(self.store.authenticate(attempt["token"])["state"], "running")
        self.assertEqual(self.store.ready(self.work_id), [])
        self.store.confirm_stopped(attempt["attempt_id"])
        checkpointed = self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="blocked",
            answer="Partial implementation saved",
            evidence=["Checkpoint contains completed parser"],
            output=output,
            cost_usd=0.25,
        )
        self.assertEqual(checkpointed["steps"][0]["state"], "blocked")
        self.assertIsNone(checkpointed["steps"][0]["submission"])
        checkpoint = checkpointed["steps"][0]["checkpoint"]
        self.assertEqual(checkpoint["commit"], output["commit"])
        self.assertEqual(checkpoint["plan_revision"], checkpointed["plan_revision"])
        self.assertEqual(self.store.active_attempts(), [])
        with self.assertRaises(ValueError):
            self.store.authenticate(attempt["token"])
        self.change(
            "unblock",
            actor="omp",
            step_id="backend",
            blocker_id=blocker_id,
            resolution="Approved schema supplied",
            evidence=["schema.json"],
        )
        resumed = self.reserve()
        self.assertEqual(resumed["checkpoint"], checkpoint)
        self.assertNotEqual(resumed["token"], attempt["token"])
        self.assertEqual(self.view()["authorization"]["launches"], 2)
        submitted = self.submit(resumed)
        self.assertEqual(submitted["steps"][0]["state"], "review")
        self.assertIsNone(submitted["steps"][0]["checkpoint"])

    def test_blocked_review_preserves_exact_submission_until_unblocked(self):
        self.agreed()
        self.authorize()
        original = self.submit(self.reserve())["steps"][0]["submission"]
        review = self.reserve(actor="claude", kind="review")
        blocked = self.change(
            "block",
            actor="claude",
            token=review["token"],
            step_id="backend",
            note="Review fixture unavailable",
            condition="Fixture restored",
        )
        with self.assertRaises(ValueError):
            self.verdict(review)
        self.store.confirm_stopped(review["attempt_id"])
        result = self.store.finish_attempt(
            review["attempt_id"],
            outcome="blocked",
            answer="Cannot run required fixture",
            evidence=["Fixture missing"],
            output=None,
            cost_usd=0.25,
        )
        self.assertEqual(result["steps"][0]["submission"], original)
        self.assertEqual(result["steps"][0]["state"], "blocked")
        self.change(
            "unblock",
            actor="claude",
            step_id="backend",
            blocker_id=blocked["steps"][0]["blockers"][0]["blocker_id"],
            resolution="Fixture restored",
            evidence=["fixture.json"],
        )
        retry = self.reserve(actor="claude", kind="review")
        self.assertEqual(retry["submission"], original)
        self.verdict(retry)
        accepted = self.finish_review(retry)
        self.assertEqual(accepted["steps"][0]["state"], "accepted")

    def test_external_block_still_requires_recovery_after_child_exit(self):
        self.agreed()
        self.authorize()
        attempt = self.reserve()
        output = self.output(attempt)
        blocked = self.change(
            "block",
            actor="claude",
            step_id="backend",
            note="Stop for inspection",
            condition="Inspection complete",
        )
        self.store.confirm_stopped(attempt["attempt_id"])
        result = self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="blocked",
            answer="Stopped externally",
            evidence=["Partial work"],
            output=output,
            cost_usd=0.25,
        )
        self.assertEqual(result["steps"][0]["state"], "recovery_required")
        self.assertIsNone(result["steps"][0]["checkpoint"])
        self.change(
            "unblock",
            actor="claude",
            step_id="backend",
            blocker_id=blocked["steps"][0]["blockers"][0]["blocker_id"],
            resolution="Inspection complete",
            evidence=["Inspection report"],
        )
        self.assertEqual(self.store.ready(self.work_id), [])

    def test_blocked_without_observed_exit_does_not_enable_replay(self):
        self.agreed()
        self.authorize()
        attempt = self.reserve()
        output = self.output(attempt)
        self.change(
            "block",
            actor="omp",
            token=attempt["token"],
            step_id="backend",
            note="Missing input",
            condition="Input supplied",
        )
        result = self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="blocked",
            answer="May still be executing",
            evidence=["Partial output"],
            output=output,
            cost_usd=0.25,
        )
        self.assertEqual(result["steps"][0]["state"], "recovery_required")
        self.assertIsNone(result["steps"][0]["checkpoint"])


if __name__ == "__main__":
    unittest.main()


class AttemptBudgetAndShellTests(WorkItemsTests):
    def _stored_grant(self):
        with sqlite3.connect(self.store.database) as db:
            return json.loads(
                db.execute(
                    "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
                ).fetchone()[0]
            )["authorization"]

    def _rewrite_grant(self, changes):
        with sqlite3.connect(self.store.database) as db:
            card = json.loads(
                db.execute(
                    "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
                ).fetchone()[0]
            )
            for key in changes.pop("__drop__", ()):
                card["authorization"].pop(key, None)
            card["authorization"].update(changes)
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), self.work_id),
            )
            db.commit()

    def test_attempt_ceiling_defaults_independently_of_max_launches(self):
        self.agreed()
        view = self.authorize(max_launches=8, max_cost_usd=8.0)
        grant = view["authorization"]
        self.assertEqual(grant["max_attempt_cost_usd"], 4.0)
        self.assertEqual(grant["attempt_cost_policy"], "default_share")
        self.assertEqual(grant["preview"]["max_attempt_cost_usd"], 4.0)
        self.assertIs(grant["preview"]["permissions"]["shell"], False)
        self.assertIs(grant["preview"]["permissions"]["os_sandbox"], False)
        self.assertNotIn("allow_tests", self._stored_grant())
        # Eight launches would previously have capped one attempt at 1.0.
        self.assertEqual(self.reserve()["reserved_cost_usd"], 4.0)

    def test_explicit_attempt_ceiling_and_remaining_budget(self):
        self.agreed(parallel=True)
        view = self.authorize(max_launches=8, max_cost_usd=8.0, max_attempt_cost_usd=3)
        self.assertEqual(view["authorization"]["attempt_cost_policy"], "explicit")
        first = self.reserve()
        self.assertEqual(first["reserved_cost_usd"], 3.0)
        second = self.reserve("frontend", actor="claude")
        self.assertEqual(second["reserved_cost_usd"], 3.0)
        with self.assertRaises(ValueError):
            self.authorize(max_launches=1, max_cost_usd=1.0, max_attempt_cost_usd=0)

    def test_legacy_grant_keeps_launch_share_and_shell_semantics(self):
        self.agreed()
        self.authorize(max_launches=8, max_cost_usd=8.0)
        self._rewrite_grant(
            {
                "__drop__": (
                    "max_attempt_cost_usd",
                    "attempt_cost_policy",
                    "allow_shell",
                ),
                "allow_tests": True,
            }
        )
        grant = self.view()["authorization"]
        self.assertEqual(grant["preview"]["attempt_cost_policy"], "legacy_launch_share")
        self.assertEqual(grant["preview"]["max_attempt_cost_usd"], 1.0)
        self.assertIs(grant["preview"]["permissions"]["shell"], True)
        attempt = self.reserve()
        self.assertEqual(attempt["reserved_cost_usd"], 1.0)
        self.assertIs(attempt["allow_shell"], True)

    def test_allow_tests_is_a_compatible_alias_not_a_second_permission(self):
        self.agreed()
        granted = self.authorize(allow_shell=None, allow_tests=True)["authorization"]
        self.assertIs(granted["allow_shell"], True)
        self.assertNotIn("allow_tests", self._stored_grant())
        canonical = self.authorize(allow_shell=True)["authorization"]
        self.assertIs(canonical["allow_shell"], True)
        with self.assertRaises(ValueError):
            self.authorize(allow_shell=True, allow_tests=False)
        denied = self.authorize(allow_shell=False)["authorization"]
        self.assertIs(denied["allow_shell"], False)
        self.assertIs(self.reserve()["allow_shell"], False)

    def test_malformed_model_selection_is_labelled_not_fatal(self):
        self.agreed()
        self.authorize()
        self._rewrite_grant({"__drop__": ("model_provenance",)})
        grant = self.view()["authorization"]
        self.assertIn("model_selection_error", grant)
        self.assertIn("preview", grant)
