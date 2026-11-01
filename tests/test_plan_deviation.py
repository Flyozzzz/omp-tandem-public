"""The declared ladder admits a success; this names what else actually ran."""

import unittest
from uuid import uuid4

from omp_tandem.models import VerificationPlan, decode_outcome
from omp_tandem.verification import enforce_verification, summarize_plan_deviation

SCOPE = {"kind": "content", "digest": "a" * 64}


def run(check_id, criterion, *, started=None, ended=None):
    return {
        "check_id": check_id,
        "run_id": str(uuid4()),
        "criterion": criterion,
        "role": "author",
        "command": "verify",
        "started_at": started,
        "ended_at": ended,
        "scope": SCOPE,
        "result": "passed",
    }


class PlanDeviationTests(unittest.TestCase):
    def setUp(self):
        self.plan = VerificationPlan(
            checks=[
                {"id": "declared", "criterion": "Boundary holds", "command": "verify"}
            ]
        )

    def test_a_ladder_run_exactly_as_declared_reports_no_deviation(self):
        summary = summarize_plan_deviation(
            self.plan, [run("declared", "Boundary holds", started=1, ended=3)]
        )
        self.assertEqual(summary["unmatched"], [])
        self.assertEqual(summary["repeated"], [])
        self.assertEqual(summary["unmatched_run_count"], 0)
        self.assertEqual(summary["repeat_run_count"], 0)
        self.assertEqual(summary["declared_check_count"], 1)
        self.assertEqual(summary["recorded_run_count"], 1)

    def test_runs_outside_the_plan_are_named_with_their_recorded_cost(self):
        summary = summarize_plan_deviation(
            self.plan,
            [
                run("declared", "Boundary holds", started=1, ended=3),
                run("mutants", "Mutants die", started=10, ended=70),
                run("mutants", "Mutants die", started=70, ended=130),
            ],
        )
        self.assertEqual(summary["repeated"], [])
        (unmatched,) = summary["unmatched"]
        self.assertEqual(unmatched["check_id"], "mutants")
        self.assertEqual(unmatched["run_count"], 2)
        self.assertEqual(unmatched["recorded_seconds"], 120.0)
        self.assertEqual(summary["unmatched_run_count"], 2)

    def test_repeating_a_declared_check_is_counted_apart_from_its_first_run(self):
        summary = summarize_plan_deviation(
            self.plan,
            [
                run("declared", "Boundary holds", started=1, ended=3),
                run("declared", "Boundary holds", started=4, ended=6),
                run("declared", "Boundary holds", started=7, ended=9),
            ],
        )
        self.assertEqual(summary["unmatched"], [])
        (repeated,) = summary["repeated"]
        self.assertEqual(repeated["run_count"], 3)
        self.assertEqual(repeated["repeat_count"], 2)
        self.assertEqual(summary["repeat_run_count"], 2)

    def test_an_unrecorded_duration_stays_unknown_rather_than_zero(self):
        summary = summarize_plan_deviation(
            self.plan,
            [
                run("declared", "Boundary holds", started=1, ended=3),
                run("fuzz", "Intervals agree"),
                run("fuzz", "Intervals agree", started=5, ended=11),
            ],
        )
        (unmatched,) = summary["unmatched"]
        self.assertEqual(unmatched["run_count"], 2)
        self.assertEqual(unmatched["runs_with_recorded_duration"], 1)
        self.assertEqual(unmatched["recorded_seconds"], 6.0)

    def test_a_run_with_no_usable_timestamps_reports_no_seconds(self):
        summary = summarize_plan_deviation(self.plan, [run("fuzz", "Intervals agree")])
        (unmatched,) = summary["unmatched"]
        self.assertIsNone(unmatched["recorded_seconds"])
        self.assertEqual(unmatched["runs_with_recorded_duration"], 0)

    def test_work_beside_the_ladder_is_reported_and_never_refused(self):
        declared = run("declared", "Boundary holds", started=1, ended=3)
        report = decode_outcome(
            {
                "outcome": "success",
                "answer": "Boundary holds",
                "summary": "Verified",
                "checks": [
                    {
                        "name": "Boundary holds",
                        "check_id": "declared",
                        "run_id": declared["run_id"],
                        "result": "passed",
                    }
                ],
                "check_runs": [
                    declared,
                    run("mutants", "Mutants die", started=4, ended=64),
                ],
            }
        )
        # The success still stands: this is reporting, not a new gate.
        enforce_verification(self.plan, report)
        summary = summarize_plan_deviation(self.plan, report.check_runs)
        self.assertEqual(summary["unmatched_run_count"], 1)


if __name__ == "__main__":
    unittest.main()
