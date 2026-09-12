"""Observable collaboration races, immutable acceptance, and execution boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import time
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


def six_step_fixture(case, *, count=6):
    """A real agreed card with accepted, submitted, blocked and running work."""
    contract = plan()
    template = contract["steps"][0]
    contract["steps"] = [
        {
            **template,
            "id": f"step{index}",
            "owned_files": [f"step{index}.py"],
            "acceptance": [f"Criterion {index}: " + "observable contract " * 120],
            "depends_on": []
            if index < count - 1
            else [f"step{prior}" for prior in range(count - 1)],
        }
        for index in range(count)
    ]
    case.change("propose", plan=contract)
    case.agreed()
    first = case.reserve("step0", autonomous=False)
    case.submit(first)
    reviewer = case.reserve("step0", actor="claude", kind="review", autonomous=False)
    case.verdict(reviewer)
    second = case.reserve("step1", autonomous=False)
    case.submit(second)
    case.change(
        "block",
        step_id="step2",
        note="Required fixture missing",
        condition="Restore the fixture",
    )
    case.reserve("step3", autonomous=False)
    return case.view()


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

    def report(self, attempt, *, outcome="success"):
        return self.change(
            "report",
            actor=attempt["actor"],
            token=attempt["token"],
            step_id=attempt["step_id"],
            submission_id=attempt["submission"]["submission_id"],
            resolution=outcome,
            note="Independent assessment of the exact snapshot",
            evidence=["Read selected bytes through the snapshot reader"],
        )

    def verdict(self, attempt, *, action="accept", report=True):
        if report and attempt.get("protocol") == "independent_first":
            current = self.store.attempt(attempt["attempt_id"])
            if not current.get("independent_report"):
                self.report(attempt)
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

    def test_six_step_summary_and_explicit_material(self):
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        full = six_step_fixture(self)
        request = {"action": "get", "work_id": self.work_id}
        summary = perform_work(self.store, request, actor="omp")
        self.assertLessEqual(
            len(
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode()
            ),
            16384,
        )
        self.assertEqual(
            {step["id"] for step in summary["steps"]},
            {step["id"] for step in full["steps"]},
        )
        blockers = [
            item["blocker_id"]
            for step in full["steps"]
            for item in step["blockers"]
            if item["resolved_at"] is None
        ]
        self.assertEqual(
            {item["blocker_id"] for item in summary["blockers"]}, set(blockers)
        )
        self.assertNotIn("markdown", summary)
        self.assertNotIn("plan", summary)
        selected = perform_work(
            self.store, request, actor="omp", presentation=WorkPresentation(view="plan")
        )
        self.assertEqual(selected["plan"], full["plan"])
        selected = perform_work(
            self.store,
            {**request, "step_id": "step1"},
            actor="omp",
            presentation=WorkPresentation(view="step"),
        )
        self.assertEqual(
            selected["step"]["submissions"], [full["steps"][1]["submission"]]
        )
        self.assertEqual(
            selected["step"]["acceptance"], full["plan"]["steps"][1]["acceptance"]
        )
        selected = perform_work(
            self.store, request, actor="omp", presentation=WorkPresentation(view="full")
        )
        self.assertEqual(selected["plan"], full["plan"])
        self.assertEqual(selected["steps"], full["steps"])
        rendered = perform_work(
            self.store,
            request,
            actor="omp",
            presentation=WorkPresentation(view="plan", format="markdown"),
        )
        self.assertEqual(set(rendered), {"markdown"})
        self.assertIn(full["plan"]["steps"][0]["acceptance"][0], rendered["markdown"])
        accepted = perform_work(
            self.store,
            {**request, "step_id": "step0"},
            actor="omp",
            presentation=WorkPresentation(view="step"),
        )["step"]
        self.assertEqual(
            {item["kind"] for item in accepted["attempts"]}, {"implement", "review"}
        )
        self.assertEqual(accepted["submissions"], [full["steps"][0]["submission"]])
        self.assertEqual(accepted["review_acceptance"], full["steps"][0]["acceptance"])

    def test_large_summary_has_explicit_continuation(self):
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        full = six_step_fixture(self, count=40)
        summary = perform_work(
            self.store,
            {"action": "get", "work_id": self.work_id},
            actor="omp",
            presentation=WorkPresentation(limit=10),
        )
        continuation = next(
            item for item in summary["continuation"] if item["section"] == "steps"
        )
        self.assertEqual(
            {step["id"] for step in summary["steps"]}
            | set(continuation["remaining_ids"]),
            {step["id"] for step in full["steps"]},
        )

    def test_history_pages_and_visibility_bound_cursors(self):
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        six_step_fixture(self)
        for _ in range(64):
            self.change("agree")
        request = {"action": "history", "work_id": self.work_id}
        first = perform_work(
            self.store, request, actor="omp", presentation=WorkPresentation(limit=10)
        )
        self.assertTrue(all("snapshot" not in event for event in first["events"]))
        revisions = [event["revision"] for event in first["events"]]
        cursor = first["next_cursor"]
        while cursor:
            page = perform_work(
                self.store,
                request,
                actor="omp",
                presentation=WorkPresentation(limit=10, cursor=cursor),
            )
            revisions.extend(event["revision"] for event in page["events"])
            cursor = page["next_cursor"]
        self.assertEqual(revisions, list(range(1, self.view()["revision"] + 1)))
        explicit = perform_work(
            self.store,
            request,
            actor="omp",
            presentation=WorkPresentation(limit=1, include_snapshots=True),
        )
        self.assertIn("plan", explicit["events"][0]["snapshot"])
        changed_visibility = perform_work(
            self.store,
            request,
            actor="claude",
            presentation=WorkPresentation(cursor=first["next_cursor"]),
        )
        self.assertEqual(changed_visibility["error"]["code"], "cursor_stale")
        self.change("agree")
        stale = perform_work(
            self.store,
            request,
            actor="omp",
            presentation=WorkPresentation(cursor=first["next_cursor"]),
        )
        self.assertEqual(stale["error"]["code"], "cursor_stale")
        self.assertEqual(stale["current"]["revision"], self.view()["revision"])

    def test_next_actions_follow_owner_capability_and_dependencies(self):
        from omp_tandem.work_access import perform_work

        request = {"action": "get", "work_id": self.work_id}
        draft = perform_work(self.store, request, actor="omp")
        claim = next(
            item for item in draft["next_actions"] if item["action"] == "claim"
        )
        self.assertEqual(claim["blocked_reason"], "plan_not_agreed")
        self.agreed()
        ready = perform_work(self.store, request, actor="omp")
        claim = next(
            item for item in ready["next_actions"] if item["step_id"] == "backend"
        )
        self.assertTrue(claim["allowed"])
        dependent = perform_work(self.store, request, actor="claude")
        self.assertEqual(
            next(
                item
                for item in dependent["next_actions"]
                if item["step_id"] == "integration"
            )["blocked_reason"],
            "dependency_not_accepted",
        )
        claims = {}
        result = perform_work(
            self.store,
            {
                "action": "claim",
                "work_id": self.work_id,
                "step_id": "backend",
                "expected_revision": ready["revision"],
                "operation_id": str(uuid4()),
            },
            actor="omp",
            claims=claims,
        )
        action = result["next_actions"][0]
        self.assertEqual(action["action"], "submit")
        self.assertTrue(action["allowed"])
        self.assertIn("commit", action["required_fields"])
        self.assertNotIn(result["claim"]["token"], json.dumps(result["next_actions"]))
        other = perform_work(self.store, request, actor="claude")
        self.assertEqual(
            other["next_actions"][0]["blocked_reason"], "another_claim_active"
        )
        self.assertEqual(
            perform_work(self.store, request, actor="operator")["next_actions"], []
        )
        self.change("pause")
        paused = perform_work(self.store, request, actor="omp")
        self.assertTrue(
            all(
                not item["allowed"] and item["blocked_reason"] == "paused"
                for item in paused["next_actions"]
            )
        )

    def test_public_cas_conflict_and_exact_replay_have_no_effects(self):
        from omp_tandem.work_access import perform_work

        command = {
            "action": "agree",
            "work_id": self.work_id,
            "expected_revision": self.view()["revision"],
            "operation_id": str(uuid4()),
        }
        first = perform_work(self.store, command, actor="claude")
        replay = perform_work(self.store, command, actor="claude")
        self.assertEqual(replay, first)
        before = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="claude"
        )
        conflict = perform_work(
            self.store, {**command, "operation_id": str(uuid4())}, actor="omp"
        )
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        self.assertEqual(conflict["current"]["revision"], first["revision"])
        self.assertNotIn("plan", conflict["current"])
        self.assertEqual(
            self.store.perform(
                {"action": "history", "work_id": self.work_id}, actor="claude"
            ),
            before,
        )

    def _recover(self, actor, origin, *, operation_id=None, step_id="backend"):
        return self.store.perform(
            {
                "action": "recover",
                "work_id": self.work_id,
                "step_id": step_id,
                "expected_revision": self.view()["revision"],
                "operation_id": operation_id or str(uuid4()),
            },
            actor=actor,
            origin=origin,
        )

    def _claim(self, actor, origin, *, step_id="backend"):
        return self.store.perform(
            {
                "action": "claim",
                "work_id": self.work_id,
                "step_id": step_id,
                "expected_revision": self.view()["revision"],
                "operation_id": str(uuid4()),
            },
            actor=actor,
            origin=origin,
        )["claim"]

    def _mutation(self, action, actor, origin, claims, **fields):
        from omp_tandem.work_access import perform_work

        return perform_work(
            self.store,
            {
                "action": action,
                "work_id": self.work_id,
                "step_id": "backend",
                "expected_revision": self.view()["revision"],
                "operation_id": str(uuid4()),
                **fields,
            },
            actor=actor,
            claims=claims,
            origin=origin,
        )

    def _stored_attempt(self, attempt_id):
        with sqlite3.connect(self.store.database) as db:
            return json.loads(
                db.execute(
                    "SELECT attempt FROM work_attempts WHERE attempt_id=?",
                    (attempt_id,),
                ).fetchone()[0]
            )

    def _store_attempt(self, attempt):
        with sqlite3.connect(self.store.database) as db:
            db.execute(
                "UPDATE work_attempts SET attempt=? WHERE attempt_id=?",
                (json.dumps(attempt), attempt["attempt_id"]),
            )

    def _attempt_rows(self):
        with sqlite3.connect(self.store.database) as db:
            return db.execute("SELECT count(*) FROM work_attempts").fetchone()[0]

    def test_own_host_claim_recovers_and_foreign_hosts_are_refused(self):
        self.agreed()
        home = {"host_owner": "host-a"}
        claim = self._claim("omp", home)
        before = self.view()["revision"]
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("omp", {"host_owner": "host-b"})
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("claude", home)
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("omp", None)
        self.assertEqual(self.view()["revision"], before)
        command = {
            "action": "recover",
            "work_id": self.work_id,
            "step_id": "backend",
            "expected_revision": before,
            "operation_id": str(uuid4()),
        }
        recovered = self.store.perform(command, actor="omp", origin=home)
        self.assertEqual(recovered["claim"]["token"], claim["token"])
        self.assertEqual(recovered["recovery"]["attempt_id"], claim["attempt_id"])
        self.assertNotIn("token", json.dumps(recovered["recovery"]))
        after = self.view()["revision"]
        self.assertEqual(after, before + 1)
        replay = self.store.perform(command, actor="omp", origin=home)
        self.assertEqual(replay["claim"]["token"], claim["token"])
        self.assertEqual(self.view()["revision"], after)
        with self.assertRaises(WorkConflict):
            self.store.perform(
                {**command, "step_id": "frontend"}, actor="omp", origin=home
            )
        self.assertEqual(self._attempt_rows(), 1)
        self.change("pause")
        with self.assertRaisesRegex(ValueError, "recovery_claim_fenced"):
            self._recover("omp", home)

    def test_successor_closes_failed_native_review_without_execution(self):
        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        # The backend reviewer (claude) claimed through a native task's own tool.
        origin = {
            "host_owner": "claude-host",
            "task_id": "task-1",
            "conversation_id": "conv-1",
        }
        claim = self._claim("claude", origin)
        submission_id = claim["submission"]["submission_id"]
        author_text = self.store.perform(
            {"action": "get", "work_id": self.work_id}, actor="operator"
        )["steps"][0]["submission"]["answer"]
        successor_host = {"host_owner": "omp-host"}
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("claude", origin)
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("omp", successor_host)
        with self.assertRaises(ValueError):
            self.store.authorize_successor(
                claim["attempt_id"],
                host_owner="omp-host",
                principal="omp",
                note="x",
                actor="omp",
            )
        authorized = self.store.authorize_successor(
            claim["attempt_id"],
            host_owner="omp-host",
            principal="omp",
            note="Operator handoff",
        )
        successor_id = authorized["successor"]["successor_id"]
        self.assertNotIn("omp-host", json.dumps(authorized["successor"]))
        with self.assertRaisesRegex(ValueError, "recovery_stop_unconfirmed"):
            self._recover("omp", successor_host)
        self.assertEqual(
            self.store.origin_settled(
                "task-1", status="failed", teardown_confirmed=False
            ),
            [claim["attempt_id"]],
        )
        with self.assertRaisesRegex(ValueError, "recovery_stop_unconfirmed"):
            self._recover("omp", successor_host)
        stored = self._stored_attempt(claim["attempt_id"])
        stored["binding"]["origin_settled"] = None
        self._store_attempt(stored)
        self.store.origin_settled("task-1", status="failed", teardown_confirmed=True)
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        claims = {}
        seen = perform_work(
            self.store,
            {"action": "get", "work_id": self.work_id, "step_id": "backend"},
            actor="omp",
            claims=claims,
            presentation=WorkPresentation(view="step"),
            origin=successor_host,
        )
        self.assertEqual(claims, {})
        descriptor = seen["step"]["recovery"]
        self.assertTrue(descriptor["origin"]["settled"]["teardown_confirmed"])
        self.assertFalse(descriptor["successors"][0]["consumed"])
        self.assertNotIn("token", json.dumps(descriptor))
        hint = next(
            item for item in seen["next_actions"] if item["action"] == "recover"
        )
        self.assertTrue(hint["allowed"])
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("claude", successor_host)
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("omp", {"host_owner": "another-host"})
        recovered = self._mutation("recover", "omp", successor_host, claims)
        self.assertEqual(claims[(self.work_id, "backend")], claim["token"])
        self.assertTrue(recovered["recovery"]["successors"][0]["consumed"])
        self.assertNotIn(author_text, json.dumps(recovered))
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self._recover("omp", successor_host)
        with self.assertRaisesRegex(ValueError, "recovery_report_only"):
            perform_work(
                self.store,
                {
                    "action": "claim",
                    "work_id": self.work_id,
                    "step_id": "frontend",
                    "expected_revision": self.view()["revision"],
                    "operation_id": str(uuid4()),
                },
                actor="omp",
                attempt_token=claim["token"],
                origin=successor_host,
            )
        with self.assertRaisesRegex(ValueError, "recovery_report_only"):
            self.store.perform(
                {
                    "action": "pause",
                    "work_id": self.work_id,
                    "expected_revision": self.view()["revision"],
                    "operation_id": str(uuid4()),
                },
                actor="omp",
                attempt_token=claim["token"],
            )
        reported = self._mutation(
            "report",
            "omp",
            successor_host,
            claims,
            submission_id=submission_id,
            resolution="success",
            note="Closure on the preserved finding",
            evidence=["artifact finding-1"],
        )
        self.assertNotIn(author_text, json.dumps(reported))
        accepted = self._mutation(
            "accept",
            "omp",
            successor_host,
            claims,
            submission_id=submission_id,
            note="Accepted on preserved evidence",
            evidence=["artifact finding-1"],
        )
        self.assertEqual(accepted["steps"][0]["state"], "accepted")
        step = next(item for item in self.view()["steps"] if item["id"] == "backend")
        self.assertEqual(step["acceptance"]["actor"], "claude")
        self.assertEqual(
            step["acceptance"]["recorded_by"],
            {
                "principal": "omp",
                "successor_id": successor_id,
                "scope": "report_only",
                "on_behalf_of": "claude",
            },
        )
        history = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="operator"
        )["events"]
        kinds = [event["kind"] for event in history]
        self.assertIn("successor_authorized", kinds)
        self.assertIn("claim_recovered", kinds)
        accept_event = next(event for event in history if event["kind"] == "accept")
        self.assertEqual(accept_event["actor"], "claude")
        self.assertEqual(accept_event["details"]["recorded_by"]["principal"], "omp")
        self.assertEqual(self._attempt_rows(), 2)
        with self.assertRaisesRegex(ValueError, "recovery_claim_completed"):
            self._recover("omp", successor_host)
        usage = self.store.work_usage(self.work_id)
        self.assertEqual(usage["linked_task_ids"], ["task-1"])
        self.assertEqual(usage["coverage"], "unknown")
        self.assertEqual(usage["unattributed"]["legacy_unlinked"], 1)

    def test_recovery_replay_and_same_principal_successor_stay_bounded(self):
        from omp_tandem.work_access import perform_work

        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        origin = {
            "host_owner": "claude-host",
            "task_id": "task-3",
            "conversation_id": "conv-3",
        }
        claim = self._claim("claude", origin)
        submission_id = claim["submission"]["submission_id"]
        self.store.origin_settled("task-3", status="failed", teardown_confirmed=True)
        # A successor with the claim's OWN principal on another host is still a
        # report-only successor, never the original holder.
        self.store.authorize_successor(
            claim["attempt_id"],
            host_owner="claude-host-2",
            principal="claude",
            note="same-principal handoff",
        )
        successor_host = {"host_owner": "claude-host-2"}
        # Contradictory request identity is refused before any credential.
        with self.assertRaisesRegex(ValueError, "recovery_context_changed"):
            self.store.perform(
                {
                    "action": "recover",
                    "work_id": self.work_id,
                    "step_id": "backend",
                    "expected_revision": self.view()["revision"],
                    "operation_id": str(uuid4()),
                    "submission_id": "different-submission",
                },
                actor="claude",
                origin=successor_host,
            )
        with self.assertRaisesRegex(ValueError, "recovery_context_changed"):
            self.store.perform(
                {
                    "action": "recover",
                    "work_id": self.work_id,
                    "step_id": "backend",
                    "expected_revision": self.view()["revision"],
                    "operation_id": str(uuid4()),
                    "commit": "f" * 40,
                },
                actor="claude",
                origin=successor_host,
            )
        claims = {}
        command = {
            "action": "recover",
            "work_id": self.work_id,
            "step_id": "backend",
            "expected_revision": self.view()["revision"],
            "operation_id": str(uuid4()),
        }
        recovered = perform_work(
            self.store, command, actor="claude", claims=claims, origin=successor_host
        )
        self.assertEqual(claims[(self.work_id, "backend")], claim["token"])
        revision = recovered["revision"]
        self.assertEqual(
            self.store.recovery_scope(claim["token"], "claude", "claude-host-2"),
            "report_only",
        )
        self.assertIsNone(
            self.store.recovery_scope(claim["token"], "claude", "claude-host")
        )
        # Identical replay from an unauthorized host with the same principal.
        foreign = {}
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            perform_work(
                self.store,
                command,
                actor="claude",
                claims=foreign,
                origin={"host_owner": "unauthorized-host"},
            )
        self.assertEqual(foreign, {})
        with self.assertRaisesRegex(ValueError, "recovery_not_authorized"):
            self.store.perform(command, actor="claude", origin=None)
        replay = perform_work(
            self.store, command, actor="claude", claims={}, origin=successor_host
        )
        self.assertEqual(replay["claim"]["token"], claim["token"])
        self.assertEqual(self.view()["revision"], revision)
        # Report-only scope applies to the same-principal successor host.
        for action, fields in (
            ("pause", {}),
            ("claim", {"step_id": "frontend"}),
        ):
            with self.assertRaisesRegex(ValueError, "recovery_report_only"):
                perform_work(
                    self.store,
                    {
                        "action": action,
                        "work_id": self.work_id,
                        "expected_revision": self.view()["revision"],
                        "operation_id": str(uuid4()),
                        **fields,
                    },
                    actor="claude",
                    attempt_token=claim["token"],
                    origin=successor_host,
                )
        self.assertEqual(self.view()["status"], "active")
        with self.assertRaisesRegex(ValueError, "recovery_report_only"):
            perform_work(
                self.store,
                {
                    "action": "submit",
                    "work_id": self.work_id,
                    "step_id": "backend",
                    "expected_revision": self.view()["revision"],
                    "operation_id": str(uuid4()),
                    "commit": "a" * 40,
                    "note": "x",
                    "evidence": ["x"],
                },
                actor="claude",
                claims=claims,
                origin=successor_host,
            )
        self._mutation(
            "report",
            "claude",
            successor_host,
            claims,
            submission_id=submission_id,
            resolution="success",
            note="Closure by the same-principal successor",
            evidence=["artifact finding-3"],
        )
        history = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="operator"
        )["events"]
        report_event = next(e for e in history if e["kind"] == "independent_report")
        self.assertEqual(report_event["details"]["recorded_by"]["principal"], "claude")
        self.assertEqual(
            report_event["details"]["recorded_by"]["on_behalf_of"], "claude"
        )
        self._mutation("compare", "claude", successor_host, claims)
        # The stage changed after recovery: the exact retry no longer re-issues.
        with self.assertRaisesRegex(ValueError, "recovery_context_changed"):
            perform_work(
                self.store, command, actor="claude", claims={}, origin=successor_host
            )

    def test_recovery_refuses_changed_context_expired_and_legacy_claims(self):
        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        origin = {
            "host_owner": "claude-host",
            "task_id": "task-2",
            "conversation_id": "conv-2",
        }
        claim = self._claim("claude", origin)
        self.store.origin_settled("task-2", status="failed", teardown_confirmed=True)
        self.store.authorize_successor(
            claim["attempt_id"], host_owner="omp-host", principal="omp", note="handoff"
        )
        stored = self._stored_attempt(claim["attempt_id"])
        stored["binding"]["successors"][0]["context"]["submission_id"] = "different"
        self._store_attempt(stored)
        with self.assertRaisesRegex(ValueError, "recovery_context_changed"):
            self._recover("omp", {"host_owner": "omp-host"})
        stored["binding"]["successors"][0]["context"]["submission_id"] = claim[
            "submission"
        ]["submission_id"]
        stored["deadline"] = time.time() - 1
        self._store_attempt(stored)
        with self.assertRaisesRegex(ValueError, "recovery_claim_(expired|fenced)"):
            self._recover("omp", {"host_owner": "omp-host"})
        legacy = self._stored_attempt(claim["attempt_id"])
        legacy.pop("binding")
        legacy["deadline"] = time.time() + 3600
        legacy["state"] = "reserved"
        self._store_attempt(legacy)
        with self.assertRaisesRegex(ValueError, "recovery_legacy_unbound"):
            self._recover("claude", origin)
        with self.assertRaisesRegex(ValueError, "recovery_legacy_unbound"):
            self.store.authorize_successor(
                claim["attempt_id"], host_owner="h", principal="omp", note="n"
            )

    def test_application_records_are_explicit_and_progress_never_runs_git(self):
        self.agreed()
        for spec in self.view()["plan"]["steps"]:
            worker = self.reserve(spec["id"], actor=spec["owner"], autonomous=False)
            self.submit(worker, cost=None)
            self.verdict(
                self.reserve(
                    spec["id"], actor=spec["reviewer"], kind="review", autonomous=False
                )
            )
        view = self.view()
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["application"]["status"], "not_recorded")
        self.assertIn("not_recorded", view["next_action"])
        self.assertNotIn("not merged", view["next_action"])
        with self.assertRaises(ValueError):
            self.store.record_application(self.work_id, {"kind": "guess"})
        with self.assertRaises(ValueError):
            self.store.record_application(
                self.work_id, {"kind": "git_assessment"}, actor="claude"
            )
        recorded = self.store.record_application(
            self.work_id,
            {
                "kind": "git_assessment",
                "expected_head": "a" * 40,
                "observed_head": "a" * 40,
                "target_commit": view["result"]["commit"],
                "relation": "descendant",
                "observed_at": 1.0,
            },
        )
        self.assertEqual(recorded["application"]["status"], "observed")
        record = recorded["application"]["records"][0]
        self.assertEqual(record["final_submission_id"], view["result"]["submission_id"])
        self.assertIn("descendant", recorded["next_action"])
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        with patch(
            "omp_tandem.work_items.subprocess.run", side_effect=AssertionError("git")
        ):
            self.store.perform({"action": "get", "work_id": self.work_id}, actor="omp")
            self.store.perform({"action": "list"}, actor="omp")
            self.store.perform(
                {"action": "history", "work_id": self.work_id}, actor="omp"
            )
            self.store.progress(self.work_id)
            perform_work(
                self.store,
                {"action": "history", "work_id": self.work_id},
                actor="omp",
                presentation=WorkPresentation(limit=5),
            )
            rendered = perform_work(
                self.store,
                {"action": "get", "work_id": self.work_id},
                actor="omp",
                presentation=WorkPresentation(format="markdown"),
            )["markdown"]
        self.assertIn("## Application", rendered)
        self.assertIn("git_assessment", rendered)
        self.assertIn("## Agreements", rendered)
        self.assertIn("Verdict: accepted", rendered)
        self.assertIn("coverage: unknown", rendered)
        self.assertNotIn("```json", rendered)

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

    def test_legacy_receipts_replay_original_meaning_with_empty_review_context(self):
        # Persist the old unversioned model-dump hash and original response bytes.
        commands = [
            {
                "action": "create",
                "expected_revision": 0,
                "operation_id": "legacy-create",
                "plan": plan(),
            }
        ]
        created = self.store.perform(commands[0], actor="claude")
        work_id = created["work_id"]
        commands.append(
            {
                "action": "propose",
                "work_id": work_id,
                "expected_revision": created["revision"],
                "operation_id": "legacy-propose",
                "plan": plan(),
            }
        )
        self.store.perform(commands[1], actor="claude")
        for actor in ("claude", "omp"):
            view = self.store.perform(
                {"action": "get", "work_id": work_id}, actor=actor
            )
            self.store.perform(
                {
                    "action": "agree",
                    "work_id": work_id,
                    "expected_revision": view["revision"],
                    "operation_id": "legacy-agree-" + actor,
                },
                actor=actor,
            )
        view = self.store.perform({"action": "get", "work_id": work_id}, actor="omp")
        commands.append(
            {
                "action": "claim",
                "work_id": work_id,
                "step_id": "backend",
                "expected_revision": view["revision"],
                "operation_id": "legacy-claim",
            }
        )
        self.store.perform(commands[-1], actor="omp")
        for command in commands:
            actor = "omp" if command["action"] == "claim" else "claude"
            legacy = WorkCommand.model_validate(command).model_dump()
            for step in (legacy.get("plan") or {}).get("steps", []):
                step.pop("review_context_paths", None)
            digest = hashlib.sha256(
                json.dumps(
                    {"command": legacy, "attempt_id": None},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            with self.store._transaction() as db:
                saved = db.execute(
                    "SELECT response FROM work_operations WHERE actor=? AND operation_id=?",
                    (actor, command["operation_id"]),
                ).fetchone()["response"]
                response = json.loads(saved)
                for step in response["plan"]["steps"]:
                    step.pop("review_context_paths", None)
                saved = json.dumps(response)
                db.execute(
                    "UPDATE work_operations SET fingerprint=?,response=? WHERE actor=? AND operation_id=?",
                    (digest, saved, actor, command["operation_id"]),
                )
            repeated = copy.deepcopy(command)
            for step in (repeated.get("plan") or {}).get("steps", []):
                step["review_context_paths"] = []
            result = self.store.perform(repeated, actor=actor)
            if "claim" in result:
                result["claim"].pop("token", None)
            self.assertEqual(result, json.loads(saved))
            with self.assertRaises(WorkConflict):
                self.store.perform(
                    {**repeated, "note": "Different command"}, actor=actor
                )
            if repeated.get("plan"):
                repeated["plan"]["steps"][0]["review_context_paths"] = ["caller.py"]
                with self.assertRaises(WorkConflict):
                    self.store.perform(repeated, actor=actor)

    def test_review_context_is_read_only_deduplicated_and_versioned(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        self.verdict(review)
        self.finish_review(review)
        previous = self.view()
        self.assertIsNotNone(previous["steps"][0]["acceptance"])
        declaration = plan()
        declaration["steps"][0]["review_context_paths"] = ["caller.py", "caller.py"]
        revised = self.change("propose", plan=declaration)
        self.assertEqual(revised["plan_revision"], previous["plan_revision"] + 1)
        self.assertEqual(revised["agreements"], {})
        self.assertEqual(
            revised["plan"]["steps"][0]["review_context_paths"], ["caller.py"]
        )
        self.assertEqual(revised["plan"]["steps"][0]["owned_files"], ["backend.py"])
        self.assertIsNone(revised["steps"][0]["acceptance"])
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        expanded = self.reserve(actor="claude", kind="review")
        self.assertNotEqual(
            expanded["review_scope"]["snapshot_input_fingerprint"],
            review["review_scope"]["snapshot_input_fingerprint"],
        )
        declaration["steps"][0]["review_context_paths"] = [
            f"caller{i}.py" for i in range(257)
        ]
        with self.assertRaises(ValidationError):
            WorkPlan.model_validate(declaration)

    def test_review_context_rejects_noncanonical_paths(self):
        for path in (
            "../secret",
            "/absolute",
            "dir/../file",
            "./file",
            "dir//file",
            "dir/",
            ".git/config",
            ".GIT/config",
            "state://file",
            "local:config",
            "C:/file",
            "dir\\file",
            "file*.py",
            "file?.py",
            "[ab].py",
            "file\x00",
        ):
            declaration = plan()
            declaration["steps"][0]["review_context_paths"] = [path]
            with self.subTest(path=path), self.assertRaises(ValidationError):
                WorkPlan.model_validate(declaration)

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
        self.report(review)
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

    def test_corrective_plan_preserves_operational_block_and_resolution_history(self):
        self.agreed()
        self.authorize()
        attempt = self.reserve()
        output = self.output(attempt)
        blocked = self.change(
            "block",
            actor="omp",
            token=attempt["token"],
            note="Schema unavailable",
            condition="Approved schema supplied",
        )
        original = blocked["steps"][0]["blockers"][0]
        self.store.confirm_stopped(attempt["attempt_id"])
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="blocked",
            answer="Saved parser pending schema",
            evidence=["Parser checkpoint"],
            output=output,
            cost_usd=0.25,
        )
        corrected = plan()
        corrected["steps"][0]["goal"] = "Implement backend using corrected schema"
        proposed = self.change("propose", plan=corrected)
        carried = proposed["steps"][0]["blockers"][0]
        self.assertEqual(carried["blocker_id"], original["blocker_id"])
        self.assertEqual(carried["origin"], {"plan_revision": 1, "step_id": "backend"})
        self.assertIsNone(carried["resolved_at"])
        self.assertEqual(proposed["agreements"], {})
        self.assertIsNotNone(proposed["authorization"]["revoked_at"])
        self.assertIsNone(proposed["steps"][0]["checkpoint"])
        self.agreed()
        self.assertEqual(self.store.ready(self.work_id), [])
        self.assertEqual(self.store.ready(), [])
        self.authorize()
        with self.assertRaises(WorkConflict):
            self.reserve()
        with self.assertRaises(WorkConflict):
            self.change("claim", actor="omp", step_id="backend")
        self.assertEqual(self.view()["authorization"]["launches"], 0)
        with self.assertRaises(ValueError):
            self.change(
                "unblock",
                actor="claude",
                step_id="backend",
                blocker_id=carried["blocker_id"],
                resolution="not_applicable",
                note="Agreed to a correction",
                evidence=["New plan"],
            )
        resolved = self.change(
            "unblock",
            actor="omp",
            step_id="backend",
            blocker_id=carried["blocker_id"],
            resolution="resolved",
            note="Approved schema supplied",
            evidence=["schema.json"],
        )["steps"][0]["blockers"][0]
        self.assertEqual(resolved["resolution_history"][0]["kind"], "resolved")
        self.assertEqual(
            resolved["resolution_history"][0]["note"], "Approved schema supplied"
        )
        corrected["goal"] = "Deliver revised independently reviewed output"
        revised = self.change("propose", plan=corrected)["steps"][0]["blockers"][0]
        self.assertEqual(revised["origin"], original["origin"])
        self.assertEqual(revised["resolution_history"], resolved["resolution_history"])
        self.assertEqual(revised["carried_from"][:1], carried["carried_from"])
        self.assertEqual(len(revised["carried_from"]), 2)
        with self.assertRaises(ValueError):
            self.change(
                "unblock",
                actor="operator",
                step_id="backend",
                blocker_id=carried["blocker_id"],
                resolution="Rewrite resolution",
                evidence=["Replacement"],
            )

    def test_removed_step_blocker_blocks_entire_card_until_authorized_inapplicability(
        self,
    ):
        self.agreed()
        original = self.change(
            "block",
            actor="omp",
            step_id="backend",
            note="Missing service contract",
            condition="Supply contract or justify removing service",
        )["steps"][0]["blockers"][0]
        replacement = plan()
        replacement["steps"] = replacement["steps"][1:]
        replacement["steps"][0]["depends_on"] = []
        proposed = self.change("propose", plan=replacement)
        self.assertEqual(proposed["blockers"][0]["blocker_id"], original["blocker_id"])
        self.assertEqual(proposed["blockers"][0]["origin"], original["origin"])
        rendered = self.store.report_markdown(proposed, actor="claude")
        self.assertIn(original["blocker_id"], rendered)
        self.assertIn("omp or operator", rendered)
        self.assertNotIn("markdown", proposed)
        self.agreed()
        self.authorize()
        self.assertEqual(self.store.ready(self.work_id), [])
        with self.assertRaises(WorkConflict):
            self.reserve("integration", actor="claude")
        with self.assertRaises(WorkConflict):
            self.change("claim", step_id="integration")
        replacement["goal"] = "Corrective replacement without service"
        self.change("propose", plan=replacement)
        self.change("propose", plan=replacement)
        orphan = self.view()["blockers"][0]
        self.assertEqual(len(self.view()["blockers"]), 1)
        self.assertEqual(len(orphan["carried_from"]), 2)
        self.assertIsNone(orphan["carried_from"][-1]["step_id"])
        self.assertEqual(orphan["origin"], original["origin"])
        request = {
            "blocker_id": orphan["blocker_id"],
            "resolution": "not_applicable",
            "note": "Replacement has no service dependency",
            "evidence": ["Dependency audit"],
        }
        with self.assertRaises(ValueError):
            self.change("unblock", actor="claude", **request)
        with self.assertRaises(ValueError):
            self.change("unblock", actor="operator", **{**request, "evidence": []})
        with self.assertRaises(ValueError):
            self.change("unblock", actor="omp", **{**request, "note": None})
        resolved = self.change("unblock", actor="operator", **request)["blockers"][0]
        self.assertEqual(resolved["resolution_kind"], "not_applicable")
        self.assertEqual(resolved["resolution_history"][0]["actor"], "operator")
        self.agreed()
        claim = self.change("claim", step_id="integration")["claim"]
        self.assertEqual(claim["step_id"], "integration")
        with self.assertRaises(ValueError):
            self.change("unblock", actor="operator", **request)
        self.assertEqual(
            self.view()["blockers"][0]["resolution_history"],
            resolved["resolution_history"],
        )

    def test_active_publication_enforces_step_and_card_blockers(self):
        for kind in ("implement", "review"):
            for card_scope in (False, True):
                with self.subTest(kind=kind, card_scope=card_scope):
                    self.work_id = self.store.perform(
                        {
                            "action": "create",
                            "plan": plan(),
                            "expected_revision": 0,
                            "operation_id": str(uuid4()),
                        },
                        actor="claude",
                    )["work_id"]
                    self.agreed()
                    self.authorize()
                    if kind == "review":
                        self.submit(self.reserve())
                    actor = "omp" if kind == "implement" else "claude"
                    attempt = self.reserve(actor=actor, kind=kind)
                    output = self.output(attempt) if kind == "implement" else None
                    self.change(
                        "block",
                        actor=actor,
                        token=attempt["token"],
                        note="Required verification unavailable",
                        condition="Restore verification",
                    )
                    if card_scope:
                        # Persist a card-level blocker alongside an active attempt
                        # to exercise publication independently of scheduler gates.
                        with sqlite3.connect(self.store.database) as db:
                            card = json.loads(
                                db.execute(
                                    "SELECT card FROM work_cards WHERE work_id=?",
                                    (self.work_id,),
                                ).fetchone()[0]
                            )
                            card["blockers"] = card["steps"][0]["blockers"]
                            card["steps"][0]["blockers"] = []
                            db.execute(
                                "UPDATE work_cards SET card=? WHERE work_id=?",
                                (json.dumps(card), self.work_id),
                            )
                    self.agreed()
                    with self.assertRaises(ValueError):
                        if kind == "implement":
                            self.change(
                                "submit",
                                actor=actor,
                                token=attempt["token"],
                                note="Claimed complete",
                                evidence=["Insufficient check"],
                            )
                        else:
                            self.verdict(attempt)
                    finished = self.store.finish_attempt(
                        attempt["attempt_id"],
                        outcome="success",
                        answer="Claimed success cannot override blocker",
                        evidence=["Insufficient check"],
                        output=output,
                        cost_usd=0.25,
                    )
                    self.assertEqual(finished["steps"][0]["state"], "recovery_required")
                    self.assertIsNone(finished["steps"][0]["acceptance"])
                    if kind == "implement":
                        self.assertIsNone(finished["steps"][0]["submission"])
                    blockers = (
                        finished["blockers"]
                        if card_scope
                        else finished["steps"][0]["blockers"]
                    )
                    self.assertIsNone(blockers[0]["resolved_at"])

    def test_legacy_blockers_migrate_without_restarting_or_rewriting_history(self):
        self.agreed()
        self.authorize()
        resolved = self.change(
            "block",
            actor="omp",
            step_id="backend",
            note="Previous input absent",
            condition="Input supplied",
        )["steps"][0]["blockers"][0]
        self.change(
            "unblock",
            actor="omp",
            step_id="backend",
            blocker_id=resolved["blocker_id"],
            resolution="Input supplied",
            evidence=["input.json"],
        )
        attempt = self.reserve()
        blocked = self.change(
            "block",
            actor="claude",
            step_id="backend",
            note="Stop for inspection",
            condition="Inspection complete",
        )
        changed = plan()
        changed["goal"] = "Corrected plan awaiting inspection"
        self.change("propose", plan=changed)
        with sqlite3.connect(self.store.database) as db:
            card = json.loads(
                db.execute(
                    "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
                ).fetchone()[0]
            )
            card.pop("blockers")
            for blocker in card["steps"][0]["blockers"]:
                for key in (
                    "origin",
                    "carried_from",
                    "resolution_history",
                    "resolution_kind",
                ):
                    blocker.pop(key, None)
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), self.work_id),
            )
            before = list(db.iterdump())
        self.store = WorkStore(self.store.database, self.scope)
        migrated = self.view()
        self.assertEqual(migrated["blockers"], [])
        self.assertEqual(migrated["steps"][0]["state"], "recovery_required")
        self.assertEqual(
            migrated["authorization"],
            blocked["authorization"]
            | {"revoked_at": migrated["authorization"]["revoked_at"]},
        )
        history = migrated["steps"][0]["blockers"][0]["resolution_history"]
        self.assertEqual(history[0]["note"], "Input supplied")
        self.assertEqual(history[0]["evidence"], ["input.json"])
        for blocker in migrated["steps"][0]["blockers"]:
            self.assertEqual(
                blocker["origin"], {"plan_revision": 1, "step_id": "backend"}
            )
        self.assertIsNone(migrated["steps"][0]["blockers"][1]["resolved_at"])
        self.assertEqual(self.store.ready(), [])
        listed = self.store.perform({"action": "list"}, actor="claude")["items"][0]
        self.assertEqual(
            listed["steps"][0]["blockers"], migrated["steps"][0]["blockers"]
        )
        with sqlite3.connect(self.store.database) as db:
            self.assertEqual(list(db.iterdump()), before)
        self.store.confirm_stopped(attempt["attempt_id"])
        self.change(
            "reconcile",
            actor="operator",
            step_id="backend",
            resolution="retry",
            note="Process stopped",
            evidence=["Exit observed"],
        )
        self.agreed()
        self.assertEqual(self.store.ready(self.work_id), [])
        self.assertEqual(
            self.view()["steps"][0]["blockers"][0]["resolution_history"], history
        )

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

    def test_preview_authorization_shows_policy_without_storing(self):
        self.agreed()
        preview = self.store.preview_authorization(
            budget_seconds=60,
            max_launches=8,
            max_cost_usd=8.0,
            allow_work=True,
            allow_tests=True,
        )
        self.assertIs(preview["stored"], False)
        self.assertEqual(preview["preview"]["max_attempt_cost_usd"], 4.0)
        self.assertEqual(preview["preview"]["attempt_cost_policy"], "default_share")
        self.assertIn("unreserved remainder", preview["preview"]["reserve_policy"])
        self.assertIs(preview["preview"]["permissions"]["shell"], True)
        self.assertIsNone(self.view()["authorization"])
        with self.assertRaises(ValueError):
            self.store.preview_authorization(
                budget_seconds=60,
                max_launches=1,
                max_cost_usd=1.0,
                allow_work=True,
                allow_shell=True,
                allow_tests=False,
            )


class IndependentReviewTests(WorkItemsTests):
    SENTINEL = "Implemented observable contract"

    def _bound_get(self, review):
        return self.store.perform(
            {"action": "get", "work_id": self.work_id},
            actor=review["actor"],
            attempt_token=review["token"],
        )

    def _bound_history(self, review):
        return self.store.perform(
            {"action": "history", "work_id": self.work_id},
            actor=review["actor"],
            attempt_token=review["token"],
        )

    def test_author_material_is_withheld_until_comparison_opens(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        self.assertIn(self.SENTINEL, json.dumps(self.view()))
        review = self.reserve(actor="claude", kind="review")
        self.assertEqual(review["protocol"], "independent_first")
        self.assertIs(review["allow_shell"], False)
        hidden = self._bound_get(review)
        self.assertEqual(hidden["visibility"], "independent_stage")
        self.assertNotIn(self.SENTINEL, json.dumps(hidden))
        self.assertIn("withheld", json.dumps(hidden["steps"][0]["submission"]))
        self.assertEqual(
            hidden["steps"][0]["submission"]["commit"], "a" * 40
        )  # raw provenance stays visible
        self.assertNotIn(self.SENTINEL, json.dumps(self._bound_history(review)))
        # Unbound coordinator reads are unchanged.
        self.assertIn(self.SENTINEL, json.dumps(self.view()))
        with self.assertRaises(ValueError):
            self.verdict(review, report=False)
        with self.assertRaises(ValueError):
            self.change(
                "compare",
                actor="claude",
                token=review["token"],
                step_id="backend",
            )
        self.report(review)
        with self.assertRaises(ValueError):
            self.report(review)
        self.assertNotIn(self.SENTINEL, json.dumps(self._bound_get(review)))
        opened = self.change(
            "compare", actor="claude", token=review["token"], step_id="backend"
        )
        self.assertNotIn("visibility", opened)
        self.assertIn(self.SENTINEL, json.dumps(self._bound_get(review)))
        with self.assertRaises(ValueError):
            self.change(
                "compare", actor="claude", token=review["token"], step_id="backend"
            )
        self.verdict(review)
        accepted = self.finish_review(review)
        self.assertEqual(accepted["steps"][0]["state"], "accepted")
        stored = self.store.attempt(review["attempt_id"])
        self.assertEqual(stored["independent_report"]["outcome"], "success")
        self.assertIsNotNone(stored["comparison_opened_at"])
        self.assertEqual(stored["verdict"]["verdict"], "accept")

    def test_partial_or_blocked_independent_report_cannot_accept(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        self.report(review, outcome="partial")
        with self.assertRaises(ValueError):
            self.verdict(review, report=False)
        with self.assertRaises(ValueError):
            self.change(
                "compare", actor="claude", token=review["token"], step_id="backend"
            )
        self.verdict(review, action="reject", report=False)
        rejected = self.finish_review(review)
        self.assertIn(rejected["steps"][0]["state"], {"ready", "changes_requested"})
        stored = self.store.attempt(review["attempt_id"])
        self.assertEqual(stored["verdict"]["verdict"], "reject")
        self.assertEqual(stored["independent_report"]["outcome"], "partial")

    def test_report_binds_to_exact_submission_and_reviewer(self):
        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        with self.assertRaises(ValueError):
            self.change(
                "report",
                actor="claude",
                token=review["token"],
                step_id="backend",
                submission_id="other-output",
                resolution="success",
                note="Wrong target",
                evidence=["x"],
            )
        with self.assertRaises(ValueError):
            self.change(
                "report",
                actor="claude",
                token=review["token"],
                step_id="backend",
                submission_id=review["submission"]["submission_id"],
                resolution="great",
                note="Invalid outcome",
                evidence=["x"],
            )
        with self.assertRaises(ValueError):
            self.change(
                "report",
                actor="omp",
                step_id="backend",
                submission_id=review["submission"]["submission_id"],
                resolution="success",
                note="Unbound principal",
                evidence=["x"],
            )

    def test_shell_grant_blocks_independent_review_before_launch(self):
        self.agreed()
        self.authorize(allow_shell=True)
        self.submit(self.reserve())
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        step = self.view()["steps"][0]
        self.assertEqual(step["state"], "blocked")
        blocker = step["blockers"][0]
        self.assertEqual(blocker["note"], WorkStore.SHELL_REVIEW_BLOCK)
        self.assertEqual(blocker["actor"], "operator")
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        with self.assertRaises(ValueError):  # not the author, not the operator
            self.change(
                "unblock",
                actor="claude",
                step_id="backend",
                blocker_id=blocker["blocker_id"],
                resolution="resolved",
                note="Reviewer cannot lift it",
                evidence=["none"],
            )
        self.change(
            "unblock",
            actor="operator",
            step_id="backend",
            blocker_id=blocker["blocker_id"],
            resolution="not_applicable",
            note="Review proceeds without shell checks",
            evidence=["operator decision"],
        )
        review = self.reserve(actor="claude", kind="review")
        self.assertIs(review["allow_shell"], False)
        self.assertEqual(
            review["shell_check_policy"], "blocked_no_stage_scoped_execution"
        )

    def test_operator_can_retire_carried_policy_before_new_submission(self):
        self.agreed()
        self.authorize(allow_shell=True)
        self.submit(self.reserve())
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        blocker = self.view()["steps"][0]["blockers"][-1]
        revised = plan()
        revised["goal"] = "Revised requirements need a new implementation"
        self.change("propose", plan=revised)
        self.agreed()
        self.change(
            "unblock",
            actor="operator",
            step_id="backend",
            blocker_id=blocker["blocker_id"],
            resolution="not_applicable",
            note="Retire the old submission's policy decision before new implementation",
            evidence=["The new plan has no submission yet"],
        )
        historical = self.view()["steps"][0]["blockers"][-1]
        self.authorize(allow_shell=True)
        self.submit(self.reserve())
        self._assert_applicability_required(historical)

    def _waived_shell_review(self):
        self.agreed()
        self.authorize(allow_shell=True)
        self.submit(self.reserve())
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        blocker = self.view()["steps"][0]["blockers"][-1]
        self.change(
            "unblock",
            actor="operator",
            step_id="backend",
            blocker_id=blocker["blocker_id"],
            resolution="not_applicable",
            note="Review without shell for this submitted context",
            evidence=["Operator accepts the unexecuted shell-check limitation"],
        )
        return self.view()["steps"][0]["blockers"][-1]

    def _assert_applicability_required(self, historical):
        before = self.view()["authorization"]["launches"]
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        current = self.view()
        self.assertEqual(current["authorization"]["launches"], before)
        self.assertIn(historical, current["steps"][0]["blockers"])
        self.assertEqual(
            current["steps"][0]["blockers"][-1]["reason"],
            "applicability_review_required",
        )

    def test_shell_waiver_does_not_transfer_to_new_plan_grant_submission(self):
        historical = self._waived_shell_review()
        revised = plan()
        revised["steps"][0]["acceptance"] = ["Revised observable behavior"]
        self.change("propose", plan=revised)
        self.agreed()
        self.authorize(allow_shell=True)
        self.submit(self.reserve())
        # Propose adds provenance to the historical blocker, not fresh authority.
        historical = self.view()["steps"][0]["blockers"][0]
        self._assert_applicability_required(historical)

    def test_shell_waiver_does_not_transfer_to_new_grant(self):
        historical = self._waived_shell_review()
        self.authorize(allow_shell=True)
        self._assert_applicability_required(historical)

    def test_shell_waiver_does_not_transfer_to_new_submission(self):
        historical = self._waived_shell_review()
        review = self.reserve(actor="claude", kind="review")
        self.report(review)
        self.change(
            "reject",
            actor="claude",
            token=review["token"],
            step_id="backend",
            submission_id=review["submission"]["submission_id"],
            note="Observable contract needs correction",
            evidence=["Boundary case fails"],
        )
        self.store.finish_attempt(
            review["attempt_id"],
            outcome="success",
            answer="Rejected",
            evidence=["Boundary case fails"],
            cost_usd=0.1,
            output=None,
        )
        self.submit(self.reserve())
        self._assert_applicability_required(historical)

    def test_legacy_unscoped_shell_waiver_is_only_historical(self):
        self._waived_shell_review()
        with self.store._transaction() as db:
            card = self.store._load(db, self.work_id)
            blocker = card["steps"][0]["blockers"][-1]
            for resolution in blocker["resolution_history"]:
                resolution.pop("scope", None)
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), self.work_id),
            )
        self._assert_applicability_required(self.view()["steps"][0]["blockers"][-1])

    def test_unmarked_operator_resolution_does_not_authorize_shell_review(self):
        self._waived_shell_review()
        with self.store._transaction() as db:
            card = self.store._load(db, self.work_id)
            card["steps"][0]["blockers"][-1].pop("policy")
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id=?",
                (json.dumps(card), self.work_id),
            )
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        blocker = self.view()["steps"][0]["blockers"][-1]
        self.assertEqual(blocker["policy"], "shell_review")
        self.assertIsNone(blocker["resolved_at"])

    def test_exact_shell_waiver_scope_reuses_without_new_effects(self):
        historical = self._waived_shell_review()
        before = self.view()
        self.assertIsNone(self.store._block_shell_review(self.work_id, "backend"))
        self.assertIsNone(self.store._block_shell_review(self.work_id, "backend"))
        self.assertEqual(self.view(), before)
        review = self.reserve(actor="claude", kind="review")
        self.assertEqual(
            review["review_scope"], historical["resolution_history"][-1]["scope"]
        )

    def test_participant_cannot_forge_or_waive_the_shell_policy_blocker(self):
        self.agreed()
        self.authorize(allow_shell=True)
        implementation = self.reserve()
        # The implementer records and resolves a same-text blocker of its own.
        self.change(
            "block",
            actor="omp",
            token=implementation["token"],
            step_id="backend",
            note=WorkStore.SHELL_REVIEW_BLOCK,
            condition="Resolve my own blocker",
        )
        forged = self.view()["steps"][0]["blockers"][0]
        self.change(
            "unblock",
            actor="omp",
            token=implementation["token"],
            step_id="backend",
            blocker_id=forged["blocker_id"],
            resolution="not_applicable",
            note="Implementation waives its own note",
            evidence=["author decision, not operator"],
        )
        self.submit(implementation)
        with self.assertRaises(ValueError):
            self.reserve(actor="claude", kind="review")
        blockers = self.view()["steps"][0]["blockers"]
        policy = [item for item in blockers if item.get("policy") == "shell_review"]
        self.assertEqual(len(policy), 1)
        self.assertEqual(policy[0]["actor"], "operator")
        self.assertIsNone(policy[0]["resolved_at"])
        self.assertNotIn("policy", forged)

    def test_claim_response_and_replay_are_projected_for_the_reviewer(self):
        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        command = {
            "action": "claim",
            "work_id": self.work_id,
            "step_id": "backend",
            "expected_revision": self.view()["revision"],
            "operation_id": "manual-review-claim",
        }
        first = self.store.perform(command, actor="claude")
        self.assertEqual(first["claim"]["protocol"], "independent_first")
        self.assertIn("token", first["claim"])
        self.assertNotIn(self.SENTINEL, json.dumps(first))
        self.assertEqual(first["claim"]["submission"]["commit"], "a" * 40)
        replay = self.store.perform(command, actor="claude")
        self.assertNotIn(self.SENTINEL, json.dumps(replay))
        self.assertEqual(replay["claim"]["token"], first["claim"]["token"])

    def test_reviewer_actions_keep_report_comparison_and_verdict_separate(self):
        from omp_tandem.work_access import perform_work

        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        review = self.reserve(actor="claude", kind="review", autonomous=False)
        request = {"action": "get", "work_id": self.work_id}

        def actions():
            return perform_work(
                self.store, request, actor="claude", attempt_token=review["token"]
            )["next_actions"]

        initial = actions()
        self.assertEqual([item["action"] for item in initial], ["report"])
        self.assertTrue(initial[0]["allowed"])
        self.assertEqual(
            initial[0]["submission_id"], review["submission"]["submission_id"]
        )
        self.assertNotIn(self.SENTINEL, json.dumps(initial))
        conflict = perform_work(
            self.store,
            {
                "action": "report",
                "work_id": self.work_id,
                "step_id": "backend",
                "expected_revision": self.view()["revision"] - 1,
                "operation_id": str(uuid4()),
                "submission_id": review["submission"]["submission_id"],
                "resolution": "success",
                "note": "Independent review",
                "evidence": ["Pinned bytes"],
            },
            actor="claude",
            attempt_token=review["token"],
        )
        self.assertEqual(conflict["error"]["code"], "revision_conflict")
        self.assertNotIn(self.SENTINEL, json.dumps(conflict))
        self.report(review)
        self.assertEqual(
            {item["action"] for item in actions() if item["allowed"]},
            {"compare", "accept", "reject"},
        )
        self.change("compare", actor="claude", token=review["token"], step_id="backend")
        self.assertEqual(
            {item["action"] for item in actions() if item["allowed"]},
            {"accept", "reject"},
        )
        self.verdict(review, report=False)
        unbound = perform_work(self.store, request, actor="claude")
        self.assertFalse(
            any(
                item["step_id"] == "backend" and item["allowed"]
                for item in unbound["next_actions"]
            )
        )

    def test_presentation_preserves_independent_disclosure(self):
        from omp_tandem.work_access import perform_work
        from omp_tandem.work_items import WorkPresentation

        self.agreed()
        self.submit(self.reserve(autonomous=False), cost=None)
        review = self.reserve(actor="claude", kind="review", autonomous=False)
        for view in ("summary", "plan", "step", "full"):
            result = perform_work(
                self.store,
                {"action": "get", "work_id": self.work_id, "step_id": "backend"},
                actor="claude",
                attempt_token=review["token"],
                presentation=WorkPresentation(view=view),
            )
            self.assertNotIn(self.SENTINEL, json.dumps(result))
        for snapshots in (False, True):
            result = perform_work(
                self.store,
                {"action": "history", "work_id": self.work_id},
                actor="claude",
                attempt_token=review["token"],
                presentation=WorkPresentation(include_snapshots=snapshots),
            )
            self.assertNotIn(self.SENTINEL, json.dumps(result))
        self.report(review)
        self.change("compare", actor="claude", token=review["token"], step_id="backend")
        result = perform_work(
            self.store,
            {"action": "get", "work_id": self.work_id, "step_id": "backend"},
            actor="claude",
            attempt_token=review["token"],
            presentation=WorkPresentation(view="step"),
        )
        self.assertIn(self.SENTINEL, json.dumps(result))

    def test_inferred_reader_never_fails_open(self):
        from omp_tandem.work_access import perform_work

        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        claims = {(self.work_id, "backend"): review["token"]}
        bound = perform_work(
            self.store,
            {"action": "get", "work_id": self.work_id},
            actor="claude",
            claims=claims,
        )
        self.assertNotIn(self.SENTINEL, json.dumps(bound))
        with self.assertRaises(ValueError):
            perform_work(
                self.store,
                {"action": "get", "work_id": self.work_id, "step_id": "integration"},
                actor="claude",
                claims=claims,
            )
        with self.assertRaises(ValueError):
            perform_work(
                self.store,
                {
                    "action": "history",
                    "work_id": self.work_id,
                    "step_id": "integration",
                },
                actor="claude",
                claims=claims,
            )

    def test_native_artifact_reader_is_withheld_until_comparison(self):
        from types import SimpleNamespace

        from omp_tandem.native_worker import NativeWorker
        from omp_tandem.runtime_models import ArtifactReadRequest

        self.agreed()
        self.authorize()
        self.submit(self.reserve())
        review = self.reserve(actor="claude", kind="review")
        self.store.started(
            review["attempt_id"], native_task_id="native-review", workspace="/tmp"
        )
        worker = NativeWorker.__new__(NativeWorker)
        worker.work_items = self.store
        worker.artifacts = SimpleNamespace(read=lambda **_: {"content": self.SENTINEL})
        request = ArtifactReadRequest(artifact_id=str(uuid4()))
        with self.assertRaises(ValueError):
            worker._read_artifact("native-review", request)
        self.report(review)
        with self.assertRaises(ValueError):
            worker._read_artifact("native-review", request)
        self.change("compare", actor="claude", token=review["token"], step_id="backend")
        self.assertEqual(
            worker._read_artifact("native-review", request)["content"], self.SENTINEL
        )


def measure_view_fixture(*, include_schemas=False):
    """Repeatable transport-size and indexed-observation experiment: run this module."""
    import asyncio
    from contextlib import closing
    from time import perf_counter

    from omp_tandem.work_access import perform_work

    case = WorkItemsTests()
    case.setUp()
    try:
        six_step_fixture(case)
        while case.view()["revision"] < 64:
            case.change("agree")
        get = {"action": "get", "work_id": case.work_id}
        history = {"action": "history", "work_id": case.work_id}
        sizes = {}
        for label, value in (
            ("before_summary_bytes", case.store.perform(get, actor="omp")),
            ("after_summary_bytes", perform_work(case.store, get, actor="omp")),
            ("before_history_bytes", case.store.perform(history, actor="omp")),
            (
                "after_history_page_bytes",
                perform_work(case.store, history, actor="omp"),
            ),
        ):
            sizes[label] = len(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            )
        start = perf_counter()
        for _ in range(100):
            case.store.progress(case.work_id)
        sizes["progress_query_mean_seconds"] = (perf_counter() - start) / 100
        for label, operation in (
            ("full_get_mean_seconds", lambda: case.store.perform(get, actor="omp")),
            (
                "summary_get_mean_seconds",
                lambda: perform_work(case.store, get, actor="omp"),
            ),
        ):
            start = perf_counter()
            for _ in range(20):
                operation()
            sizes[label] = (perf_counter() - start) / 20
        with closing(sqlite3.connect(case.store.database)) as db:
            sizes["progress_query_plan"] = db.execute(
                "EXPLAIN QUERY PLAN SELECT revision,status,updated_at FROM work_cards WHERE work_id=?",
                (case.work_id,),
            ).fetchall()
        if include_schemas:
            from fastmcp import Client

            from omp_tandem.api import build_server
            from omp_tandem.bridge import Bridge
            from omp_tandem.runtime_identity import runtime_identity

            async def wire():
                bridge = Bridge(
                    case.scope.base,
                    "unused",
                    None,
                    project_root=case.root,
                    channel_enabled=False,
                    webhook_enabled=False,
                    migrate_legacy=False,
                )
                async with Client(build_server(bridge)) as client:
                    mcp = next(
                        tool.inputSchema
                        for tool in await client.list_tools()
                        if tool.name == "tandem_work"
                    )
                    native = next(
                        tool.parameters
                        for tool in bridge.runtime.worker.worker_tools(
                            {"task_id": "wire-fixture"}
                        )
                        if tool.name == "tandem_work"
                    )
                    # Lossless common-definition factoring keeps the evidence readable.
                    definitions = mcp.get("$defs")
                    if definitions == native.get("$defs"):
                        return {
                            "shared_$defs": definitions,
                            "mcp": {
                                key: value
                                for key, value in mcp.items()
                                if key != "$defs"
                            },
                            "native": {
                                key: value
                                for key, value in native.items()
                                if key != "$defs"
                            },
                        }
                    return {"mcp": mcp, "native": native}

            sizes["wire_schema"] = asyncio.run(wire())
            sizes["runtime_identity"] = runtime_identity()
        print(json.dumps(sizes, ensure_ascii=False, indent=2))
    finally:
        case.doCleanups()


if __name__ == "__main__":
    import sys

    if "--measure-view" in sys.argv:
        measure_view_fixture(include_schemas="--schemas" in sys.argv)
    else:
        unittest.main()
