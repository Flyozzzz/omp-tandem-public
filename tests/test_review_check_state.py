"""The supervisor ledger, not participant evidence, authorizes review acceptance."""

import hashlib
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.models import check_revision
from omp_tandem.review_check_state import has_review_runner
from omp_tandem.work_items import (
    WorkCommand,
    WorkConflict,
    WorkStore,
)

from . import test_work_items as fixtures

CONTAINER = {
    "executor": "docker",
    "socket": "/private/docker.sock",
    "daemon_id": "fixture-daemon",
    "api_version": "1.47",
    "image_id": "sha256:" + "a" * 64,
    "network_mode": "none",
    "platform": "linux",
}


class ReviewCheckStateTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WorkItemsTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store

    def declare(self, *, command="printf 'checked\\n'", extra=False):
        plan = fixtures.plan()
        checks = [
            {
                "id": "unit",
                "criterion": "Backend observable behavior",
                "command": command,
                "requires_shell": True,
            }
        ]
        if extra:
            checks.append(
                {
                    "id": "integration",
                    "criterion": "Integration behavior",
                    "command": "true",
                    "phase": "integration",
                }
            )
        plan["steps"][0]["review_verification"] = {
            "stage": "integration" if extra else "targeted",
            "checks": checks,
        }
        self.case.revise(plan)
        self.case.agreed()

    def reviewer(self, **grant):
        self.case.authorize(
            **{
                "allow_review_checks": True,
                "review_check_container": CONTAINER,
                **grant,
            }
        )
        implementation = self.case.reserve()
        self.case.submit(implementation)
        return self.case.reserve(actor="claude", kind="review")

    def compare(self, attempt):
        return self.case.change(
            "compare", actor=attempt["actor"], token=attempt["token"]
        )

    def execution(self, row):
        return {
            "executor": "docker",
            "container_id": "d" * 64,
            "container_name": "omp-tandem-check-" + row["run_id"],
            "image_id": row["policy"]["container"]["image_id"],
            "daemon_id": row["policy"]["container"]["daemon_id"],
            "cwd": "/workspace",
            "commit": row["commit"],
            "tree_hash": "b" * 40,
            "environment_hashes": row["policy"]["environment_hashes"],
            "timeout_seconds": row["policy"]["timeout_seconds"],
            "os_sandbox": False,
            "process_boundary": "docker_pid_namespace",
        }

    def finished_check(
        self,
        attempt,
        *,
        check_id="unit",
        result="passed",
        gone=True,
        unchanged=True,
        text="private verification result\n",
    ):
        row = self.store.reserve_review_check(attempt["attempt_id"], check_id)
        execution = self.execution(row)
        self.store.start_review_check(row["run_id"], execution=execution)
        directory = self.case.scope.directory / "work-checks" / attempt["attempt_id"]
        directory.mkdir(parents=True, exist_ok=True)
        data = text.encode()
        (directory / (row["run_id"] + ".log")).write_bytes(data)
        request = dict(
            result=result,
            exit_code=0 if result == "passed" else 1,
            error=None,
            output={
                "id": row["run_id"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "truncated": False,
            },
            execution=execution,
            process_confirmed_gone=gone,
            input_unchanged=unchanged,
        )
        return self.store.finish_review_check(row["run_id"], **request), request

    def assert_rejected_candidate(self, attempt, result):
        step = result["steps"][0]
        self.assertIsNone(step["acceptance"])
        self.assertIsNone(step["submission"])
        self.assertEqual(step["checkpoint"]["commit"], attempt["submission"]["commit"])
        self.assertIn(
            {
                "work_id": self.case.work_id,
                "step_id": "backend",
                "actor": "omp",
                "kind": "implement",
            },
            self.store.ready(),
        )

    def test_legacy_shell_grant_never_becomes_runner_authority(self):
        self.case.agreed()
        self.case.authorize(allow_shell=True)
        author = self.case.reserve()
        self.assertFalse(has_review_runner(author))
        self.case.submit(author)
        with self.assertRaisesRegex(ValueError, "Review launch blocked"):
            self.case.reserve(actor="claude", kind="review")
        self.assertNotIn("review_check_policy", self.case.view()["authorization"])

    def test_container_authority_requires_resolved_local_immutable_identity(self):
        self.declare()
        invalid = [
            None,
            {**CONTAINER, "executor": "host"},
            {**CONTAINER, "socket": "unix:///private/docker.sock"},
            {**CONTAINER, "socket": "/private/../docker.sock"},
            {**CONTAINER, "daemon_id": ""},
            {**CONTAINER, "api_version": "latest"},
            {**CONTAINER, "image_id": "python:3.12"},
            {**CONTAINER, "network_mode": "bridge"},
            {**CONTAINER, "platform": "darwin"},
            {**CONTAINER, "privileged": True},
        ]
        for container in invalid:
            with (
                self.subTest(container=container),
                self.assertRaisesRegex(
                    ValueError, "resolved local Docker container identity"
                ),
            ):
                self.case.authorize(
                    allow_review_checks=True, review_check_container=container
                )
        self.assertIsNone(self.case.view()["authorization"])
        grant = self.case.authorize(
            allow_review_checks=True,
            review_check_container={**CONTAINER, "network_mode": "f" * 64},
        )
        self.assertTrue(
            grant["authorization"]["preview"]["permissions"]["review_checks"]
        )
        self.assertEqual(
            grant["authorization"]["review_check_policy"]["container"]["network_mode"],
            "f" * 64,
        )

    def test_old_host_policy_cannot_launch_checks_or_bypass_acceptance(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        with self.store._transaction() as db:
            card = self.store._load(db, self.case.work_id)
            del card["authorization"]["review_check_policy"]["container"]
            self.store._record(db, card, "fixture_legacy_policy", "operator")
            del attempt["review_check_policy"]["container"]
            current = self.store._attempt(db, attempt["attempt_id"])
            current["review_check_policy"] = attempt["review_check_policy"]
            self.store._save_attempt(db, current)
        self.assertFalse(has_review_runner(attempt))
        self.assertFalse(
            self.case.view()["authorization"]["preview"]["permissions"]["review_checks"]
        )
        self.assertIsNone(self.store.review_check_context(attempt["attempt_id"]))
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.store.reserve_review_check(attempt["attempt_id"], "unit")
        with self.assertRaises(ValueError):
            self.compare(attempt)

    def test_pass_requires_observed_start_and_immutable_container_finish(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        row = self.store.reserve_review_check(attempt["attempt_id"], "unit")
        execution = self.execution(row)
        request = {
            "result": "passed",
            "exit_code": 0,
            "error": None,
            "output": {
                "id": row["run_id"],
                "sha256": hashlib.sha256(b"").hexdigest(),
                "bytes": 0,
                "truncated": False,
            },
            "execution": execution,
            "process_confirmed_gone": True,
            "input_unchanged": True,
        }
        with self.assertRaisesRegex(ValueError, "requires observed exit zero"):
            self.store.finish_review_check(row["run_id"], **request)
        self.store.start_review_check(row["run_id"], execution=execution)
        with self.assertRaisesRegex(ValueError, "execution identity changed"):
            self.store.finish_review_check(
                row["run_id"],
                **{**request, "execution": {**execution, "container_id": "e" * 64}},
            )
        finished = self.store.finish_review_check(row["run_id"], **request)
        self.assertEqual(finished["check_run"]["result"], "passed")

    def test_selected_ladder_and_environment_must_be_complete_before_grant(self):
        self.declare(command=None)
        with self.assertRaisesRegex(ValueError, "complete executable"):
            self.case.authorize(
                allow_review_checks=True, review_check_container=CONTAINER
            )
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "environment variable is missing"),
        ):
            self.case.authorize(
                allow_review_checks=True,
                review_check_container=CONTAINER,
                review_check_env=["CHECK_INPUT"],
            )
        self.assertIsNone(self.case.view()["authorization"])

    def test_new_policy_does_not_grant_reviewer_shell_or_trigger_legacy_blocker(self):
        self.declare()
        with patch.dict(os.environ, {"CHECK_INPUT": "secret-operator-value"}):
            attempt = self.reviewer(allow_shell=True, review_check_env=["CHECK_INPUT"])
        self.assertTrue(has_review_runner(attempt))
        self.assertFalse(attempt["allow_shell"])
        policy = attempt["review_check_policy"]
        self.assertEqual(policy["commit"], attempt["submission"]["commit"])
        self.assertEqual(
            policy["environment_hashes"],
            {
                "CHECK_INPUT": hashlib.sha256(b"secret-operator-value").hexdigest(),
            },
        )
        for action in ("get", "history"):
            payload = self.store.perform(
                {"action": action}, actor="claude", attempt_token=attempt["token"]
            )
            encoded = json.dumps(payload)
            self.assertNotIn("environment_hashes", encoded)
            self.assertNotIn("secret-operator-value", encoded)
            self.assertNotIn(policy["environment_hashes"]["CHECK_INPUT"], encoded)
            self.assertNotIn(CONTAINER["socket"], encoded)
        visible = self.case.view()["authorization"]["review_check_policy"]["container"]
        self.assertEqual(visible["image_id"], CONTAINER["image_id"])
        self.assertEqual(visible["network_mode"], "none")
        self.assertEqual(visible["platform"], "linux")
        self.case.report(attempt)
        row, _ = self.finished_check(attempt)
        self.assertEqual(
            row["check_run"]["environment"],
            {"platform": "linux", "image_id": CONTAINER["image_id"]},
        )

    def test_report_checks_comparison_acceptance_are_separate_gates(self):
        self.declare(extra=True)
        attempt = self.reviewer()
        self.assertIsNone(self.store.review_check_context(attempt["attempt_id"]))
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.store.reserve_review_check(attempt["attempt_id"], "unit")
        self.case.report(attempt)
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.compare(attempt)
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.case.verdict(attempt)
        row, _ = self.finished_check(attempt)
        run = row["check_run"]
        self.assertEqual(run["provenance"], "machine_observed")
        self.assertEqual(run["role"], "reviewer")
        self.assertEqual(run["scope"]["kind"], "commit")
        self.assertEqual(run["scope"]["digest"], attempt["submission"]["commit"])
        self.assertEqual(
            run["environment"],
            {"platform": "linux", "image_id": CONTAINER["image_id"]},
        )
        self.assertEqual(run["check_revision"], check_revision(row["check"]))
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.compare(attempt)
        self.finished_check(attempt, check_id="integration")
        with self.assertRaisesRegex(ValueError, "requires comparison"):
            self.case.verdict(attempt)
        self.compare(attempt)
        self.case.verdict(attempt)
        result = self.case.finish_review(attempt)
        self.assertEqual(result["steps"][0]["state"], "accepted")

    def test_forged_participant_evidence_cannot_satisfy_trusted_checks(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.case.change(
                "accept",
                token=attempt["token"],
                submission_id=attempt["submission"]["submission_id"],
                note="machine_observed: all checks passed",
                evidence=["run_id=forged; exit=0"],
            )
        request = {
            "action": "accept",
            "expected_revision": self.case.view()["revision"],
            "operation_id": str(uuid4()),
            "check_runs": [{"provenance": "machine_observed"}],
        }
        with self.assertRaises(ValueError):
            WorkCommand.model_validate(request)
        self.assertEqual(
            self.store.trusted_verification_context(attempt["attempt_id"])["runs"], []
        )

    def test_settled_failure_permits_comparison_and_successful_rejection(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        self.finished_check(attempt, result="failed")
        assessment = self.store.review_check_assessment(attempt["attempt_id"])
        self.assertEqual(
            (assessment["status"], assessment["settled"]), ("failed", True)
        )
        self.compare(attempt)
        with self.assertRaisesRegex(ValueError, "all trusted review checks passed"):
            self.case.verdict(attempt)
        self.case.verdict(attempt, action="reject")
        result = self.case.finish_review(attempt)
        self.assert_rejected_candidate(attempt, result)

    def test_early_rejection_prevents_spawns_and_requires_active_teardown(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        row = self.store.reserve_review_check(attempt["attempt_id"], "unit")
        self.case.verdict(attempt, action="reject")
        self.assertIsNone(self.store.review_check_context(attempt["attempt_id"]))
        with self.assertRaisesRegex(ValueError, "stopped before rejection"):
            self.case.finish_review(attempt)
        self.store.finish_review_check(
            row["run_id"],
            result="not_run",
            exit_code=None,
            error="Rejected before process creation",
            output={
                "id": row["run_id"],
                "sha256": hashlib.sha256(b"").hexdigest(),
                "bytes": 0,
                "truncated": False,
            },
            execution={},
            process_confirmed_gone=True,
            input_unchanged=True,
        )
        result = self.case.finish_review(attempt)
        self.assert_rejected_candidate(attempt, result)

    def test_execution_cannot_substitute_approved_container_or_inputs(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        row = self.store.reserve_review_check(attempt["attempt_id"], "unit")
        execution = self.execution(row)
        for changes in (
            {"executor": "host"},
            {"image_id": "sha256:" + "f" * 64},
            {"daemon_id": "other-daemon"},
            {"container_id": "short-id"},
            {"container_name": "omp-tandem-check-" + str(uuid4())},
            {"cwd": "/host/workspace"},
            {"commit": "f" * 40},
            {"tree_hash": "e" * 40},
            {"environment_hashes": {"UNAPPROVED": "e" * 64}},
            {"timeout_seconds": 301},
            {"process_boundary": "host_process_group"},
            {"os_sandbox": True},
            {"shell": "/bin/sh"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    ValueError, "approved commit, environment and container identity"
                ),
            ):
                self.store.start_review_check(
                    row["run_id"], execution={**execution, **changes}
                )
        self.assertEqual(
            self.store.review_check_assessment(attempt["attempt_id"])["status"],
            "uncertain",
        )
        self.store.start_review_check(row["run_id"], execution=execution)
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            self.store.start_review_check(
                row["run_id"], execution={**execution, "container_id": "e" * 64}
            )

    def test_reservation_survives_restart_without_replay_and_completion_is_immutable(
        self,
    ):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        row = self.store.reserve_review_check(attempt["attempt_id"], "unit")
        restarted = WorkStore(self.store.database, self.case.scope)
        replay = restarted.reserve_review_check(attempt["attempt_id"], "unit")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], row["run_id"])
        self.assertEqual(
            restarted.review_check_assessment(attempt["attempt_id"])["status"],
            "uncertain",
        )
        finished, request = self.finished_check(attempt)
        self.assertEqual(
            restarted.finish_review_check(row["run_id"], **request), finished
        )
        with self.assertRaisesRegex(ValueError, "completion is immutable"):
            restarted.finish_review_check(row["run_id"], **{**request, "exit_code": 7})

    def test_uncertain_teardown_and_changed_input_never_permit_comparison_or_next_check(
        self,
    ):
        self.declare(extra=True)
        attempt = self.reviewer()
        self.case.report(attempt)
        self.finished_check(attempt, result="failed", gone=False, unchanged=False)
        self.assertEqual(
            self.store.review_check_assessment(attempt["attempt_id"])["status"],
            "uncertain",
        )
        self.assertIsNone(self.store.review_check_context(attempt["attempt_id"]))
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.compare(attempt)
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.store.reserve_review_check(attempt["attempt_id"], "integration")

    def test_binding_changes_and_revocation_refuse_current_credit(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        self.finished_check(attempt)
        with self.store._transaction() as db:
            altered = self.store._attempt(db, attempt["attempt_id"])
            altered["submission"]["commit"] = "c" * 40
            self.store._save_attempt(db, altered)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            self.store.review_check_assessment(attempt["attempt_id"])
        with self.store._transaction() as db:
            self.store._save_attempt(db, attempt)
        self.store.revoke(self.case.work_id)
        with self.assertRaisesRegex(ValueError, "binding changed"):
            self.store.review_check_context(attempt["attempt_id"])

    def test_private_output_reader_rechecks_stage_scope_cursor_digest_and_symlinks(
        self,
    ):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        row, _ = self.finished_check(attempt, text="PRIVATE-LINE\n" * 1000)
        request = {"action": "get"}
        options = dict(actor="claude", attempt_token=attempt["token"])
        metadata = self.store.review_check_section(request, **options)
        self.assertFalse(metadata["output_visible"])
        self.assertEqual(metadata["runs"], [])
        section = "verification/" + row["run_id"]
        with self.assertRaisesRegex(ValueError, "hidden until comparison"):
            self.store.review_check_section(request, section=section, **options)
        self.compare(attempt)
        first = self.store.review_check_section(
            request, section=section, limit=16000, **options
        )
        self.assertLessEqual(len(first["content"].encode()), 16000)
        second = self.store.review_check_section(
            request, section=section, cursor=first["next_cursor"], **options
        )
        self.assertEqual(second["offset"], len(first["content"]))
        with self.assertRaisesRegex(ValueError, "different principal"):
            self.store.review_check_section(
                request, section=section, actor="omp", attempt_token=attempt["token"]
            )
        with self.assertRaisesRegex(ValueError, "bound to the review step"):
            self.store.review_check_section(
                {"action": "get", "step_id": "integration"}, **options
            )
        self.case.change("heartbeat", token=attempt["token"])
        stale = self.store.review_check_section(
            request, section=section, cursor=first["next_cursor"], **options
        )
        self.assertEqual(stale["error"]["code"], "cursor_stale")
        log = (
            self.case.scope.directory
            / "work-checks"
            / attempt["attempt_id"]
            / (row["run_id"] + ".log")
        )
        log.write_text("corrupted")
        with self.assertRaisesRegex(ValueError, "size changed"):
            self.store.review_check_section(request, section=section, **options)
        log.unlink()
        log.symlink_to(self.store.database)
        with self.assertRaises(OSError):
            self.store.review_check_section(request, section=section, **options)

    def test_submit_preflight_honest_intent_and_exact_receipts_outlive_attempt(self):
        self.case.agreed()
        self.case.authorize()
        attempt = self.case.reserve()
        request = {
            "action": "submit",
            "expected_revision": self.case.view()["revision"],
            "operation_id": "submit-intent",
            "note": "Ready for capture",
            "evidence": ["Behavior exercised"],
        }
        options = dict(actor="omp", attempt_token=attempt["token"])
        before = self.case.view()
        prepared = self.store.prepare_submission(request, **options)
        self.assertFalse(prepared["replayed"])
        self.assertEqual(self.case.view(), before)
        self.assertIsNone(
            self.store.attempt(attempt["attempt_id"])["submission_intent"]
        )
        saved = self.store.perform(request, **options)
        self.assertEqual(saved["submission_progress"], "intent_recorded")
        self.assertFalse(saved["output_committed"])
        self.assertEqual(saved["steps"][0]["submission_progress"], "intent_recorded")
        with self.assertRaises(WorkConflict):
            self.store.prepare_submission(
                {**request, "note": "different input"}, **options
            )
        self.case.submit(attempt)
        replay = self.store.prepare_submission(request, **options)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], saved)
        self.assertEqual(
            self.case.view()["steps"][0]["submission_progress"], "output_committed"
        )

    def test_failed_capture_is_not_a_committed_submission(self):
        self.case.agreed()
        self.case.authorize()
        attempt = self.case.reserve()
        self.case.change(
            "submit",
            actor="omp",
            token=attempt["token"],
            note="Capture intent",
            evidence=["check"],
        )
        self.store.finish_attempt(
            attempt["attempt_id"],
            outcome="failed",
            answer="Capture refused",
            evidence=[],
            output=None,
            cost_usd=0,
            error="undeclared file",
        )
        step = self.case.view()["steps"][0]
        self.assertEqual(step["submission_progress"], "capture_failed")
        self.assertFalse(step["output_committed"])
        self.assertIsNone(step["submission"])

    def test_declaration_change_cannot_reuse_passing_machine_evidence(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        self.finished_check(attempt)
        with self.store._transaction() as db:
            card = self.store._load(db, self.case.work_id)
            card["plan"]["steps"][0]["review_verification"]["checks"][0]["command"] = (
                "different-command"
            )
            self.store._record(db, card, "fixture_declaration_changed", "operator")
        with self.assertRaisesRegex(ValueError, "declaration binding changed"):
            self.store.review_check_assessment(attempt["attempt_id"])
        with self.assertRaisesRegex(ValueError, "declaration binding changed"):
            self.compare(attempt)

    def test_early_rejection_has_no_check_pass_claim_and_no_teardown_debt(self):
        self.declare()
        attempt = self.reviewer()
        self.case.report(attempt)
        self.case.verdict(attempt, action="reject")
        trusted = self.store.trusted_verification_context(attempt["attempt_id"])
        self.assertEqual(trusted["verdict"], "reject")
        self.assertEqual(trusted["status"], "not_run")
        self.assertFalse(trusted["settled"])
        self.assertFalse(trusted["comparison_opened"])
        self.assertTrue(trusted["teardown_confirmed"])
        self.assertEqual(trusted["runs"], [])
        result = self.case.finish_review(attempt)
        self.assert_rejected_candidate(attempt, result)
