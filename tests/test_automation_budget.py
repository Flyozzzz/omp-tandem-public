"""The operator's ceiling on new execution, and the ways it must not be evaded."""

import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from omp_tandem.automation_budget import AutomationBudget
from omp_tandem.errors import ProtocolError


class AutomationBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "tasks.sqlite3"
        self.budget = AutomationBudget(self.database)

    def test_enforcement_is_off_until_an_operator_turns_it_on(self):
        status = self.budget.status()
        self.assertEqual(status["enforcement"], "off")
        self.assertIsNone(status["max_admissions"])
        # An absent grant must not silently stop ordinary work.
        for _ in range(50):
            self.assertFalse(
                self.budget.admit("review_run", str(uuid4()))["spent"],
            )

    def test_turning_it_on_requires_an_explicit_finite_ceiling(self):
        for value in (0, -1, "3", None, 10**6):
            with self.assertRaises(ValueError):
                self.budget.authorize(value)

    def test_admissions_are_spent_once_and_then_refused(self):
        self.budget.authorize(2)
        self.assertTrue(self.budget.admit("review_run", "run-a")["spent"])
        self.assertTrue(self.budget.admit("review_run", "run-b")["spent"])
        with self.assertRaises(ProtocolError) as refusal:
            self.budget.admit("review_run", "run-c")
        envelope = refusal.exception.envelope
        self.assertEqual(envelope["code"], "automation_budget_exhausted")
        self.assertFalse(envelope["retryable"])
        self.assertEqual(self.budget.status()["remaining_admissions"], 0)

    def test_a_replayed_owner_does_not_spend_a_second_admission(self):
        self.budget.authorize(2)
        first = self.budget.admit("task", "task-1")
        again = self.budget.admit("task", "task-1")
        self.assertTrue(first["spent"])
        self.assertFalse(again["spent"])
        self.assertEqual(first["admission_id"], again["admission_id"])
        self.assertEqual(self.budget.status()["used_admissions"], 1)

    def test_a_new_id_or_a_reopened_ledger_never_refills_the_grant(self):
        self.budget.authorize(1)
        self.budget.admit("review_run", "run-a")
        # A fresh ID is the evasion the correction-chain depth allowed.
        with self.assertRaises(ProtocolError):
            self.budget.admit("review_run", str(uuid4()))
        # Reopening the store is what an MCP restart does.
        restarted = AutomationBudget(self.database)
        with self.assertRaises(ProtocolError):
            restarted.admit("work_attempt", str(uuid4()))
        self.assertEqual(restarted.status()["used_admissions"], 1)

    def test_extend_raises_the_ceiling_and_keeps_the_spend(self):
        self.budget.authorize(1)
        self.budget.admit("review_run", "run-a")
        status = self.budget.status()
        extended = self.budget.extend(status["revision"], 2)
        self.assertEqual(extended["used_admissions"], 1)
        self.assertEqual(extended["max_admissions"], 3)
        self.assertEqual(extended["remaining_admissions"], 2)
        # Stale revisions are refused, as everywhere else in this protocol.
        with self.assertRaises(ValueError):
            self.budget.extend(status["revision"], 1)

    def test_a_replacement_grant_waits_for_open_admissions(self):
        self.budget.authorize(1)
        self.budget.admit("work_attempt", "attempt-1")
        with self.assertRaises(ValueError) as refusal:
            self.budget.authorize(5)
        self.assertIn("unfinished admissions", str(refusal.exception))
        self.budget.close("work_attempt", "attempt-1")
        replaced = self.budget.authorize(5)
        self.assertEqual(replaced["used_admissions"], 0)
        self.assertEqual(replaced["max_admissions"], 5)

    def test_closing_an_admission_never_returns_its_spend(self):
        self.budget.authorize(2)
        self.budget.admit("task", "task-1")
        self.budget.close("task", "task-1")
        self.assertEqual(self.budget.status()["used_admissions"], 1)
        self.assertEqual(self.budget.status()["remaining_admissions"], 1)

    def test_concurrent_requests_cannot_exceed_the_remaining_admissions(self):
        self.budget.authorize(5)
        granted, refused = [], []
        barrier = threading.Barrier(12)

        def claim(index):
            barrier.wait()
            try:
                granted.append(self.budget.admit("task", f"task-{index}")["spent"])
            except ProtocolError:
                refused.append(index)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for value in granted if value), 5)
        self.assertEqual(len(refused), 7)
        self.assertEqual(self.budget.status()["used_admissions"], 5)

    def test_disable_is_explicit_and_reports_honestly(self):
        self.budget.authorize(1)
        self.budget.admit("task", "task-1")
        self.budget.close("task", "task-1")
        self.assertEqual(self.budget.disable()["enforcement"], "off")
        # Off means off: it must not be shown as a ceiling that still protects.
        self.assertFalse(self.budget.admit("task", str(uuid4()))["spent"])

    def test_an_unknown_owner_kind_is_refused(self):
        self.budget.authorize(1)
        with self.assertRaises(ValueError):
            self.budget.admit("shell_command", "anything")


if __name__ == "__main__":
    unittest.main()
