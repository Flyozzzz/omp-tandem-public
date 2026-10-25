"""Declared work denominators survive local success, capture and plan changes."""

import json
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from uuid import uuid4

from omp_tandem.bridge import Bridge
from omp_tandem.errors import ProtocolError
from omp_tandem.models import (
    AcceptanceSet,
    VerificationPlan,
    acceptance_revision,
    check_revision,
)
from omp_tandem.recovery import RecoveryOverview
from omp_tandem.work_access import WorkToolRequest, perform_work
from omp_tandem.work_items import WorkCommand, WorkConflict, WorkPlan

from . import test_work_items as fixtures


def declaration(*names):
    items = [{"id": name, "text": "Observable " + name} for name in names]
    return AcceptanceSet(
        set_id=str(uuid4()), revision=acceptance_revision(items), items=items
    )


def verification(declared, *, strict=True):
    return VerificationPlan.model_validate(
        {
            "acceptance_coverage": "require_current_evidence"
            if strict
            else "report_only",
            "checks": [
                {
                    "id": item.id,
                    "criterion": item.text,
                    "acceptance_refs": [
                        {
                            "set_id": declared.set_id,
                            "revision": declared.revision,
                            "criterion_id": item.id,
                        }
                    ],
                }
                for item in declared.items
            ],
        }
    )


def observation(check, *, kind="commit", digest="a" * 40, **extra):
    return {
        "run_id": str(uuid4()),
        "check_id": check.id,
        "criterion": check.criterion,
        "role": "reviewer",
        "result": "passed",
        "scope": {"kind": kind, "digest": digest},
        "check_revision": check_revision(check),
        "acceptance_refs": [ref.model_dump() for ref in check.acceptance_refs],
        **extra,
    }


class WorkAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WorkItemsTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store
        self.local = declaration("local")
        self.global_set = declaration("whole", "environment")
        self.local_checks = verification(self.local)
        self.global_checks = verification(self.global_set)
        self.contract = fixtures.plan()
        self.contract["acceptance"] = [item.text for item in self.global_set.items]
        self.contract["steps"][0]["acceptance"] = [
            item.text for item in self.local.items
        ]
        self.contract["acceptance_set"] = self.global_set.model_dump()
        self.contract["verification"] = self.global_checks.model_dump()
        self.contract["steps"][0]["acceptance_set"] = self.local.model_dump()
        self.contract["steps"][0]["verification"] = self.local_checks.model_dump()
        self.case.revise(self.contract)
        self.case.agreed()

    def implement_backend(self, **observation_fields):
        attempt = self.case.reserve(autonomous=False)
        run = observation(
            self.local_checks.checks[0],
            kind="tree",
            digest="b" * 40,
            role="author",
            **observation_fields,
        )
        self.case.change(
            "submit",
            actor="omp",
            token=attempt["token"],
            step_id="backend",
            note="Implemented",
            evidence=["Exact tree checked"],
            commit="a" * 40,
            check_runs=[run],
        )
        self.case.submit(attempt, cost=None)
        return attempt, run

    def accept_backend(self):
        attempt, run = self.implement_backend()
        reviewer = self.case.reserve(actor="claude", kind="review", autonomous=False)
        self.case.verdict(reviewer)
        return attempt, run

    def test_local_acceptance_keeps_global_denominator_and_final_gate(self):
        self.accept_backend()
        coverage = self.case.view()["acceptance_coverage"]
        self.assertEqual(
            coverage["steps"][0]["implementation"]["admission"], "eligible"
        )
        self.assertEqual(coverage["global"]["denominator"]["evidence_units"], 2)
        self.assertEqual(coverage["global"]["admission"], "ineligible")
        integration = self.case.reserve("integration", actor="claude", autonomous=False)
        self.case.submit(integration, cost=None)
        reviewer = self.case.reserve(
            "integration", actor="omp", kind="review", autonomous=False
        )
        self.case.report(reviewer)
        with self.assertRaises(ProtocolError) as rejected:
            self.case.verdict(reviewer, report=False)
        self.assertEqual(
            rejected.exception.envelope["code"], "acceptance_evidence_required"
        )
        runs = [observation(check) for check in self.global_checks.checks]
        self.case.change(
            "accept",
            actor="omp",
            token=reviewer["token"],
            step_id="integration",
            submission_id=reviewer["submission"]["submission_id"],
            note="Reviewed global evidence",
            evidence=["Exact final integration commit"],
            check_runs=runs,
        )
        result = self.case.view()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["acceptance_coverage"]["global"]["admission"], "eligible"
        )
        self.assertEqual(
            result["acceptance_coverage"]["semantic_sufficiency"], "not_assessed"
        )

    def test_plan_revision_preserves_history_without_current_credit(self):
        author, run = self.accept_backend()
        changed = {**self.contract, "goal": "A newly agreed output obligation"}
        self.case.revise(changed)
        coverage = self.case.view()["acceptance_coverage"]
        self.assertIn(author["attempt_id"], coverage["stale_plan_attempt_ids"])
        history = next(
            row
            for row in coverage["history"]
            if row["attempt_id"] == author["attempt_id"]
        )
        self.assertEqual(history["runs"][0]["run_id"], run["run_id"])
        self.assertFalse(history["current_plan"])
        self.assertFalse(
            coverage["steps"][0]["implementation"]["units"][0][
                "current_passing_run_ids"
            ]
        )

    def test_wrong_output_tree_cannot_complete_strict_implementation(self):
        attempt = self.case.reserve(autonomous=False)
        run = observation(self.local_checks.checks[0], kind="tree", digest="c" * 40)
        self.case.change(
            "submit",
            actor="omp",
            token=attempt["token"],
            step_id="backend",
            note="Implemented",
            evidence=["Old tree"],
            commit="a" * 40,
            check_runs=[run],
        )
        with self.assertRaises(ProtocolError):
            self.case.submit(attempt, cost=None)
        self.assertIsNone(self.case.view()["steps"][0]["submission"])
        saved = self.store.attempt(attempt["attempt_id"])
        self.assertEqual(
            saved["submission_intent"]["check_runs"][0]["run_id"], run["run_id"]
        )

    def test_refused_capture_keeps_exact_output_for_reconciliation(self):
        from types import SimpleNamespace

        from omp_tandem.work_supervisor import WorkSupervisor

        attempt = self.case.reserve(autonomous=False)
        output = self.case.output(attempt)
        supervisor = SimpleNamespace(
            store=self.store,
            workspace=SimpleNamespace(finish=lambda *_: output),
        )
        WorkSupervisor._finish(
            supervisor,
            attempt,
            self.contract,
            {},
            {
                "outcome": "success",
                "answer": "Captured but not verified",
                "evidence": ["Output preserved"],
                "cost_usd": None,
            },
        )
        saved = self.store.attempt(attempt["attempt_id"])
        self.assertEqual(saved["state"], "recovery_required")
        self.assertEqual(saved["output"], output)
        self.assertEqual(saved["outcome"], "interrupted")
        self.assertIsNone(self.case.view()["steps"][0]["submission"])

    def test_run_identity_cannot_rewrite_original_failure(self):
        self.implement_backend()
        reviewer = self.case.reserve(actor="claude", kind="review", autonomous=False)
        failed = observation(self.local_checks.checks[0], result="failed")
        self.case.change(
            "report",
            actor="claude",
            token=reviewer["token"],
            step_id="backend",
            submission_id=reviewer["submission"]["submission_id"],
            resolution="success",
            note="Independent finding",
            evidence=["Failure retained"],
            check_runs=[failed],
        )
        with self.assertRaises(ProtocolError) as rejected:
            self.case.change(
                "accept",
                actor="claude",
                token=reviewer["token"],
                step_id="backend",
                submission_id=reviewer["submission"]["submission_id"],
                note="Changed claim",
                evidence=["Not a new run"],
                check_runs=[{**failed, "result": "passed"}],
            )
        self.assertEqual(
            rejected.exception.envelope["code"], "evidence_identity_conflict"
        )
        history = self.case.view()["acceptance_coverage"]["history"]
        saved = next(
            row for row in history if row["attempt_id"] == reviewer["attempt_id"]
        )
        self.assertEqual(saved["runs"][0]["result"], "failed")

    def test_blocked_native_report_cannot_poison_recorded_run_identity(self):
        bridge = Bridge(
            self.case.scope.base,
            "unused",
            "unused",
            project_root=self.case.scope.root,
            channel_enabled=False,
            migrate_legacy=False,
        )
        self.addCleanup(bridge.shutdown)
        attempt = self.case.reserve(autonomous=False)
        failed = observation(
            self.local_checks.checks[0], result="failed", role="author"
        )
        self.case.change(
            "submit",
            actor="omp",
            token=attempt["token"],
            step_id="backend",
            note="Incomplete output",
            evidence=["Original failed check"],
            commit="a" * 40,
            check_runs=[failed],
        )
        task_id = str(uuid4())
        now = time.time()
        with closing(bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) "
                "VALUES (?,?,?,?,?,'work','unused','bound work','running',?)",
                (task_id, str(uuid4()), now, now, str(self.case.root), now + 60),
            )
        self.store.started(
            attempt["attempt_id"], workspace=str(self.case.root), native_task_id=task_id
        )
        report = {
            "outcome": "blocked",
            "summary": "Needs correction",
            "answer": "Output remains incomplete",
            "blockers": ["Required behavior is not implemented"],
            "check_runs": [{**failed, "result": "passed"}],
        }
        with self.assertRaises(ProtocolError) as rejected:
            bridge.interaction.submit_report(task_id, report)
        self.assertEqual(
            rejected.exception.envelope["code"], "evidence_identity_conflict"
        )
        self.assertIsNone(bridge.tasks.get(task_id, refresh=False)["report_json"])
        bridge.interaction.submit_report(task_id, {**report, "check_runs": [failed]})
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="interrupted",
            answer="Preserved unfinished work",
            evidence=[],
            output=None,
            cost_usd=None,
        )
        self.assertEqual(self.case.view()["steps"][0]["state"], "recovery_required")

    def test_manual_strict_refusal_preserves_real_committed_output(self):
        attempt = self.case.reserve(autonomous=False)
        commit = self.case._git_commit("backend.py", "value = 1\n", "owned output")
        with self.assertRaises(ProtocolError) as rejected:
            perform_work(
                self.store,
                {
                    "action": "submit",
                    "work_id": self.case.work_id,
                    "step_id": "backend",
                    "expected_revision": self.case.view()["revision"],
                    "operation_id": str(uuid4()),
                    "commit": commit,
                    "note": "Output has no current verification",
                    "evidence": ["Committed output retained"],
                },
                actor="omp",
                attempt_token=attempt["token"],
            )
        self.assertEqual(
            rejected.exception.envelope["code"], "acceptance_evidence_required"
        )
        saved = self.store.attempt(attempt["attempt_id"])
        self.assertEqual(saved["state"], "recovery_required")
        self.assertEqual(saved["output"]["commit"], commit)
        self.assertIsNone(self.case.view()["steps"][0]["submission"])

    def test_list_coverage_respects_independent_claim_until_comparison(self):
        secret = "PRIVATE_AUTHOR_CHECK_INTERPRETATION"
        self.implement_backend(note=secret)
        reviewer = self.case.reserve(actor="claude", kind="review", autonomous=False)
        claims = {(self.case.work_id, "backend"): reviewer["token"]}
        before = perform_work(
            self.store, {"action": "list"}, actor="claude", claims=claims
        )
        self.assertNotIn(secret, json.dumps(before))
        self.assertEqual(before["items"][0]["visibility"], "independent_stage")
        self.case.report(reviewer)
        self.case.change(
            "compare",
            actor="claude",
            token=reviewer["token"],
            step_id="backend",
            submission_id=reviewer["submission"]["submission_id"],
        )
        after = perform_work(
            self.store, {"action": "list"}, actor="claude", claims=claims
        )
        coverage = after["items"][0]["acceptance_coverage"]
        if coverage.get("paged"):
            request = WorkToolRequest.model_validate(coverage["arguments"])
            parts, cursor = [], None
            while True:
                page = perform_work(
                    self.store,
                    request.request,
                    actor="claude",
                    claims=claims,
                    presentation=request.model_copy(update={"cursor": cursor}),
                )
                parts.append(page["content"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            coverage = json.loads("".join(parts))
        self.assertIn(secret, json.dumps(coverage))

    def test_recovery_overview_ignores_a_completed_remembered_claim(self):
        author, _ = self.implement_backend()
        bridge = SimpleNamespace(
            scope=self.case.scope,
            work_items=self.store,
            work_participant="omp",
            work_token=None,
            _work_claims={(self.case.work_id, "backend"): author["token"]},
        )
        with self.assertRaises(ValueError):
            self.store.authenticate(author["token"])
        observed = RecoveryOverview(bridge).get()
        self.assertIn(self.case.work_id, [item["id"] for item in observed["items"]])
        self.assertFalse(observed["replay_authorized"])

    def test_declared_work_sets_cannot_replace_original_acceptance_text(self):
        mismatched = fixtures.plan()
        mismatched["acceptance_set"] = self.global_set.model_dump()
        with self.assertRaises(ValueError):
            WorkPlan.model_validate(mismatched)
        mismatched = fixtures.plan()
        mismatched["steps"][0]["acceptance_set"] = self.local.model_dump()
        with self.assertRaises(ValueError):
            WorkPlan.model_validate(mismatched)

    def test_missing_id_is_a_repairable_field_diagnostic(self):
        with self.assertRaises(ProtocolError) as refused:
            perform_work(self.store, {"action": "get"}, actor="omp")
        self.assertEqual(refused.exception.envelope["fields"], ["work_id"])
        self.assertEqual(
            refused.exception.envelope["next_request"], {"request": {"action": "list"}}
        )

    def test_manual_claim_hint_uses_reservation_permission_gate(self):
        changed = fixtures.plan()
        changed["steps"][0]["requirements"] = {"requires_shell": True}
        self.case.revise(changed)
        self.case.agreed()
        current = self.case.view()
        hint = next(
            row
            for row in self.store.next_actions(current, actor="omp")
            if row["step_id"] == "backend" and row["action"] == "claim"
        )
        self.assertFalse(hint["allowed"])
        with self.assertRaises(ValueError):
            self.case.change("claim", actor="omp", step_id="backend")

    def test_absent_additions_replay_historical_receipt_but_changed_request_does_not(
        self,
    ):
        import hashlib
        import json

        command = WorkCommand(
            action="create",
            plan=fixtures.plan(),
            expected_revision=0,
            operation_id="legacy",
        )
        historical = command.model_dump()
        historical.pop("check_runs")
        historical["plan"].pop("acceptance_set")
        historical["plan"].pop("verification")
        for step in historical["plan"]["steps"]:
            for key in (
                "acceptance_set",
                "requirements",
                "review_requirements",
                "verification",
                "review_verification",
                "review_context_paths",
            ):
                step.pop(key)
        encoded = json.dumps(
            {"command": historical, "attempt_id": None},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        receipt = self.case.view()
        with self.store._transaction() as db:
            db.execute(
                "INSERT INTO work_operations VALUES (?,?,?,?)",
                (
                    "claude",
                    "legacy",
                    hashlib.sha256(encoded).hexdigest(),
                    json.dumps(receipt),
                ),
            )
        replayed = self.store.perform(command, actor="claude")
        self.assertEqual(replayed, receipt)
        self.assertEqual(
            len(self.store.perform({"action": "list"}, actor="claude")["items"]), 1
        )
        with self.assertRaises(WorkConflict):
            self.store.perform(
                command.model_copy(update={"note": "Changed semantics"}), actor="claude"
            )
