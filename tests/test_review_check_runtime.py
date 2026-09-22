"""Real supervisor/seat protocols against a simulated Unix Docker HTTP engine.

These tests exercise transport and the domain ledger, not real container isolation.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

from omp_tandem.bridge import Bridge
from omp_tandem.review_checks import ReviewChecks
from omp_tandem.work_access import perform_work
from omp_tandem.work_supervisor import WorkSupervisor
from omp_tandem.work_workspace import WorkWorkspace
from tests import test_work_items as fixtures
from tests.fixtures.docker_engine import DockerEngine, Scenario


class ReviewCheckRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WorkItemsTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.engine = self.enterContext(
            DockerEngine(self.case.scope.directory / "work-checks")
        )
        fixture = Path(__file__).parent / "fixtures" / "review_check_peer.py"
        executable = self.case.root.parent / "review-peer"
        executable.write_text(
            f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(fixture)!r}, run_name='__main__')\n"
        )
        executable.chmod(0o700)
        self.bridge = Bridge(
            self.case.scope.base,
            str(executable),
            None,
            project_root=self.case.root,
            channel_enabled=False,
            webhook_enabled=False,
            migrate_legacy=False,
        )
        self.case.store = self.bridge.work_items
        self.addCleanup(self.bridge.shutdown)
        self.workspace = WorkWorkspace(self.case.scope)
        self.supervisor = WorkSupervisor(self.bridge, claude=str(executable))
        for adapter in self.supervisor.adapters.values():
            self.addCleanup(adapter.close)

    def failure_detail(self, observed):
        log = (
            self.case.scope.directory
            / "work-adapters"
            / observed["attempt_id"]
            / "stderr.txt"
        )
        detail = observed.get("error") or ""
        if log.exists():
            detail += "\n" + log.read_text(errors="replace")[-6000:]
        if observed.get("native_task_id"):
            task = self.bridge.tasks.get(observed["native_task_id"], refresh=False)
            detail += (
                "\n" + str(task.get("error")) + "\n" + str(task.get("answer"))[-4000:]
            )
        return detail

    def prepare(self, reviewer="claude", *, command=None, extra=None):
        definition = fixtures.plan()
        step = definition["steps"][0]
        definition["steps"] = [step]
        owner = "omp" if reviewer == "claude" else "claude"
        step.update(owner=owner, reviewer=reviewer)
        step["review_requirements"] = {"requires_shell": True}
        command = command or "python -m unittest"
        checks = [
            {
                "id": "behavior",
                "criterion": "The exact submitted behavior is exercised",
                "command": command,
            }
        ]
        if extra:
            checks.append(extra)
        step["review_verification"] = {"checks": checks}
        self.case.revise(definition)
        self.case.agreed()
        self.case.authorize(
            allow_shell=True,
            allow_review_checks=True,
            review_check_container=self.engine.policy,
            review_check_timeout=10,
            omp_model="local/peer",
            claude_model="fixture",
        )
        implementation = self.case.reserve(actor=owner)
        prepared = self.workspace.prepare(implementation, self.case.view()["plan"], [])
        self.case.store.started(
            implementation["attempt_id"], workspace=prepared["path"]
        )
        Path(prepared["path"], "backend.py").write_text("VALUE = 42\n")
        output = self.workspace.finish(
            implementation, self.case.view()["plan"], prepared
        )
        self.case.store.confirm_stopped(implementation["attempt_id"])
        self.case.store.finish_attempt(
            implementation["attempt_id"],
            outcome="success",
            answer="Candidate ready",
            evidence=["Real immutable Git capture"],
            output=output,
            cost_usd=0,
        )
        return self.case.reserve(actor=reviewer, kind="review"), output

    def test_claude_reviewer_uses_separate_checks_without_shell(self):
        self._successful_review("claude")

    def test_omp_finish_consumes_trusted_checks_without_forged_report_runs(self):
        self._successful_review("omp")

    def _successful_review(self, actor):
        attempt, output = self.prepare(actor)
        self.supervisor._run_attempt(attempt)
        card = self.case.view()
        observed = self.case.store.attempt(attempt["attempt_id"])
        self.assertEqual(observed["state"], "succeeded", self.failure_detail(observed))
        self.assertEqual(card["status"], "completed")
        self.assertEqual(len(card["steps"]), 1)
        self.assertFalse(observed["allow_shell"])
        assessment = self.case.store.review_check_assessment(
            attempt["attempt_id"], current=False
        )
        self.assertEqual(assessment["status"], "passed")
        self.assertEqual(assessment["runs"][0]["scope"]["digest"], output["commit"])
        self.assertEqual(assessment["runs"][0]["provenance"], "machine_observed")
        self.assertEqual(self.engine.start_count, 1)
        self.assertEqual(self.engine.live_containers, ())
        self.assertEqual(self.engine.errors, [])
        if actor == "omp":
            result = self.bridge.view(
                observed["native_task_id"], details=True, refresh=False
            )
            self.assertEqual(result["report"]["check_runs"], [])
            self.assertEqual(result["verification"]["source"], "supervisor_checks_v1")
            self.assertEqual(result["facts"]["checks"]["status"], "passed")

    def test_failed_command_can_be_read_and_rejected_not_accepted(self):
        self.engine.scenarios = [
            Scenario(exit_code=7, stdout=b"simulated failure\n", stderr=b"diagnostic\n")
        ]
        attempt, _ = self.prepare("omp", command="python -m unittest")
        self.supervisor._run_attempt(attempt)
        observed = self.case.store.attempt(attempt["attempt_id"])
        self.assertEqual(observed["state"], "succeeded", self.failure_detail(observed))
        self.assertEqual(observed["verdict"]["verdict"], "reject")
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])
        result = self.bridge.view(
            observed["native_task_id"], details=True, refresh=False
        )
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["facts"]["checks"]["status"], "failed")
        self.assertEqual(self.engine.live_containers, ())
        section = self.case.store.review_check_section(
            {"action": "get", "work_id": self.case.work_id, "step_id": "backend"},
            actor="operator",
        )
        captured = self.case.store.review_check_section(
            {"action": "get", "work_id": self.case.work_id, "step_id": "backend"},
            actor="operator",
            section=section["runs"][0]["section"],
        )
        self.assertIn("simulated failure", captured["content"])
        self.assertIn("diagnostic", captured["content"])

    def test_changed_input_stops_the_remaining_ladder(self):
        self.engine.scenarios = [Scenario(mutate_source=True), Scenario()]
        attempt, _ = self.prepare(
            command="printf 'VALUE = 99\\n' > backend.py",
            extra={
                "id": "later",
                "criterion": "Must not execute against altered input",
                "command": "echo SHOULD_NOT_RUN",
            },
        )
        self.case.report(attempt)
        checker = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        self.addCleanup(checker.close)
        checker.start()
        self.wait_checker(checker)
        self.assertEqual(checker.poll()["status"], "uncertain")
        assessment = self.case.store.review_check_assessment(attempt["attempt_id"])
        self.assertEqual(len(assessment["runs"]), 1)
        self.assertNotEqual(assessment["status"], "passed")
        self.assertEqual(self.engine.start_count, 1)
        self.assertEqual(len(self.engine.created), 1)
        self.assertEqual(self.engine.live_containers, ())
        with self.assertRaises(ValueError):
            self.case.verdict(attempt)

    def test_revocation_stops_running_command_and_preserves_readable_evidence(self):
        self.engine.scenarios = [
            Scenario(hang=True, stdout=b"simulated running check\n")
        ]
        attempt, _ = self.prepare(command="sleep 20")
        self.case.report(attempt)
        checker = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        checker.start()
        self.addCleanup(checker.close)
        until = time.monotonic() + 10
        while time.monotonic() < until:
            status = self.case.store.review_check_assessment(attempt["attempt_id"])[
                "status"
            ]
            if status == "running" and self.engine.start_count == 1:
                break
            time.sleep(0.02)
        self.assertEqual(status, "running")
        self.case.store.revoke(self.case.work_id)
        checker.cancel()
        self.wait_checker(checker)
        self.assertEqual(self.engine.start_count, 1)
        self.assertEqual(checker.poll()["status"], "uncertain")
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])
        metadata = self.case.store.review_check_section(
            {"action": "get", "work_id": self.case.work_id, "step_id": "backend"},
            actor="operator",
        )
        self.assertTrue(metadata["output_visible"])
        self.assertEqual(len(metadata["runs"]), 1)
        self.assertTrue(checker.poll()["teardown_confirmed"])
        self.assertEqual(self.engine.live_containers, ())
        self.assertEqual(self.engine.removal_count, 1)

    def test_lost_create_acknowledgement_is_removed_without_execution_or_replay(self):
        self._lost_acknowledgement(Scenario(lose_create_response=True), starts=0)

    def test_lost_start_acknowledgement_never_replays_execution(self):
        self._lost_acknowledgement(Scenario(lose_start_response=True), starts=1)

    def _lost_acknowledgement(self, scenario, *, starts):
        self.engine.scenarios = [scenario]
        attempt, _ = self.prepare()
        self.case.report(attempt)
        checker = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        self.addCleanup(checker.close)
        checker.start()
        self.wait_checker(checker)
        self.assertEqual(checker.poll()["status"], "uncertain")
        self.assertTrue(checker.poll()["teardown_confirmed"])
        self.assertEqual(self.engine.live_containers, ())
        self.assertEqual(self.engine.start_count, starts)
        self.assertEqual(len(self.engine.created), 1)
        with self.assertRaises(ValueError):
            self.case.verdict(attempt)
        # A new supervisor checker sees the existing ledger reservation. It must
        # not create a replacement or repeat a possibly acknowledged start.
        resumed = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        self.addCleanup(resumed.close)
        resumed.start()
        self.wait_checker(resumed)
        self.assertEqual(self.engine.start_count, starts)
        self.assertEqual(len(self.engine.created), 1)
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])

    def test_failed_removal_cannot_turn_exit_zero_into_success(self):
        self._unconfirmed_cleanup(Scenario(fail_removal=True), removed=False)

    def test_unavailable_absence_check_cannot_turn_exit_zero_into_success(self):
        self._unconfirmed_cleanup(
            Scenario(unavailable_after_removal=True), removed=True
        )

    def _unconfirmed_cleanup(self, scenario, *, removed):
        self.engine.scenarios = [scenario]
        attempt, _ = self.prepare()
        self.case.report(attempt)
        checker = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        self.addCleanup(self._close_unconfirmed_checker, checker)
        checker.start()
        self.wait_checker(checker)
        self.assertEqual(checker.poll()["status"], "uncertain")
        self.assertFalse(checker.poll()["teardown_confirmed"])
        assessment = self.case.store.review_check_assessment(attempt["attempt_id"])
        self.assertEqual(assessment["status"], "uncertain")
        self.assertFalse(assessment["settled"])
        self.assertEqual(self.engine.start_count, 1)
        self.assertEqual(bool(self.engine.live_containers), not removed)
        with self.assertRaises(ValueError):
            self.case.verdict(attempt)
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])

    def test_output_limit_cannot_pass_and_retains_bounded_evidence(self):
        self.engine.scenarios = [Scenario(stdout=b"x" * (65536 + 17))]
        attempt, _ = self.prepare()
        self.case.report(attempt)
        checker = ReviewChecks(self.case.store, self.workspace, attempt["attempt_id"])
        self.addCleanup(checker.close)
        checker.start()
        self.wait_checker(checker)
        self.assertEqual(checker.poll()["status"], "failed")
        self.assertTrue(checker.poll()["teardown_confirmed"])
        metadata = self.case.store.review_check_section(
            {"action": "get", "work_id": self.case.work_id, "step_id": "backend"},
            actor="operator",
        )
        observed = self.case.store.review_check_section(
            {"action": "get", "work_id": self.case.work_id, "step_id": "backend"},
            actor="operator",
            section=metadata["runs"][0]["section"],
            limit=128,
        )
        self.assertEqual(observed["output"]["bytes"], 65536)
        self.assertTrue(observed["output"]["truncated"])
        self.assertEqual(observed["content"], "x" * 128)
        self.assertEqual(observed["total_characters"], 65536)
        self.case.change(
            "compare",
            actor=attempt["actor"],
            token=attempt["token"],
            step_id=attempt["step_id"],
        )
        with self.assertRaises(ValueError):
            self.case.verdict(attempt)

    def _close_unconfirmed_checker(self, checker):
        with self.assertRaises(RuntimeError):
            checker.close()

    @staticmethod
    def wait_checker(checker):
        until = time.monotonic() + 15
        while checker.poll() is None and time.monotonic() < until:
            time.sleep(0.02)
        if checker.poll() is None:
            raise AssertionError("Verifier did not stop within its bounded lifetime")

    def test_managed_submit_replay_does_not_reinspect_a_removed_workspace(self):
        self.case.agreed()
        self.case.authorize()
        attempt = self.case.reserve()
        workspace = self.workspace.prepare(attempt, self.case.view()["plan"], [])
        self.case.store.started(attempt["attempt_id"], workspace=workspace["path"])
        Path(workspace["path"], "backend.py").write_text("VALUE = 42\n")
        request = {
            "action": "submit",
            "work_id": self.case.work_id,
            "step_id": "backend",
            "expected_revision": self.case.view()["revision"],
            "operation_id": "intent",
            "note": "Candidate ready",
            "evidence": ["Owned source present"],
        }
        first = perform_work(
            self.case.store, request, actor="omp", attempt_token=attempt["token"]
        )
        self.assertIsNone(self.case.view()["steps"][0]["submission"])
        saved = Path(workspace["path"]).with_name("saved-workspace")
        Path(workspace["path"]).rename(saved)
        try:
            replay = perform_work(
                self.case.store, request, actor="omp", attempt_token=attempt["token"]
            )
        finally:
            saved.rename(workspace["path"])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertIsNone(self.case.view()["steps"][0]["submission"])
