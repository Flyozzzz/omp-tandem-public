"""Counting declared evidence without ever claiming a requirement is met."""

from __future__ import annotations

import unittest
from uuid import uuid4

from omp_tandem.acceptance import enforce_coverage, evidence_units, project
from omp_tandem.models import (
    AcceptanceSet,
    CheckScope,
    SuccessOutcome,
    VerificationPlan,
    acceptance_revision,
    check_revision,
)

SET_ID = "11111111-1111-4111-8111-111111111111"
BYTES = {"kind": "commit", "digest": "a" * 40}
OTHER = {"kind": "commit", "digest": "b" * 40}


def declared(items) -> AcceptanceSet:
    return AcceptanceSet.model_validate(
        {"set_id": SET_ID, "revision": acceptance_revision(items), "items": items}
    )


def plan(checks, **values) -> VerificationPlan:
    return VerificationPlan.model_validate({"checks": checks, **values})


def ref(declared_set, criterion, obligation=None) -> dict:
    return {
        "set_id": declared_set.set_id,
        "revision": declared_set.revision,
        "criterion_id": criterion,
        "obligation_id": obligation,
    }


def run(check, declared_set, refs, **values) -> dict:
    return {
        "check_id": check.id,
        "run_id": values.pop("run_id", str(uuid4())),
        "criterion": check.criterion,
        "role": "author",
        "scope": values.pop("scope", BYTES),
        "result": values.pop("result", "passed"),
        "check_revision": values.pop("check_revision", check_revision(check)),
        "acceptance_refs": [ref(declared_set, *item) for item in refs],
        **values,
    }


FOUR_CLAUSES = [
    {
        "id": "AC-FR01-04",
        "text": "A checked demo task is struck through, moves into the completed group at its insertion position, the group counters recalculate, and the total is unchanged.",
        "obligations": [
            {"id": "strike", "text": "struck through"},
            {"id": "position", "text": "lands at its insertion position"},
            {"id": "counters", "text": "group counters recalculate"},
            {"id": "total", "text": "the total does not change"},
        ],
    }
]


class DenominatorTests(unittest.TestCase):
    def test_one_criterion_with_four_clauses_stays_four_rows(self):
        acceptance = declared(FOUR_CLAUSES)
        self.assertEqual(len(evidence_units(acceptance)), 4)
        check = plan(
            [
                {
                    "id": "landing",
                    "criterion": "the demo behaves",
                    "acceptance_refs": [ref(acceptance, "AC-FR01-04", "strike")],
                }
            ]
        )
        coverage = project(acceptance, check, [], report_scope=None)
        self.assertEqual(coverage["denominator"]["evidence_units"], 4)
        mapping = {
            unit["ref"]["obligation_id"]: unit["mapping"] for unit in coverage["units"]
        }
        # One mapped clause must not make the other three look covered.
        self.assertEqual(mapping["strike"], "mapped")
        self.assertEqual(
            [mapping[name] for name in ("position", "counters", "total")],
            ["unmapped"] * 3,
        )
        self.assertEqual(sum(gap["code"] == "unmapped" for gap in coverage["gaps"]), 3)

    def test_a_mapping_of_the_parent_credits_none_of_its_clauses(self):
        acceptance = declared(FOUR_CLAUSES)
        check = plan(
            [
                {
                    "id": "landing",
                    "criterion": "the demo behaves",
                    "acceptance_refs": [ref(acceptance, "AC-FR01-04")],
                }
            ]
        )
        coverage = project(acceptance, check, [], report_scope=None)
        self.assertEqual(
            {unit["mapping"] for unit in coverage["units"]}, {"parent_mapping_only"}
        )
        self.assertEqual(
            sum(gap["code"] == "parent_mapping_only" for gap in coverage["gaps"]), 4
        )

    def test_an_undecomposed_criterion_says_so_rather_than_claiming_one_clause(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        units = evidence_units(acceptance)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["decomposition"], "not_declared")
        self.assertIsNone(units[0]["ref"]["obligation_id"])


class ClaimedMappingTests(unittest.TestCase):
    """A run says what it exercised; only the declaration says what counts."""

    def test_a_parent_only_check_cannot_be_handed_runs_for_its_children(self):
        acceptance = declared(FOUR_CLAUSES)
        strict = plan(
            [
                {
                    "id": "landing",
                    "criterion": "the demo behaves",
                    "scope": BYTES,
                    # Mapped to the criterion, never to its clauses.
                    "acceptance_refs": [ref(acceptance, "AC-FR01-04")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        claiming = run(
            strict.checks[0],
            acceptance,
            [
                ("AC-FR01-04", "strike"),
                ("AC-FR01-04", "position"),
                ("AC-FR01-04", "counters"),
                ("AC-FR01-04", "total"),
            ],
        )
        coverage = project(acceptance, strict, [claiming])
        for unit in coverage["units"]:
            self.assertEqual(unit["mapping"], "parent_mapping_only")
            self.assertEqual(unit["current_passing_run_ids"], [])
            self.assertEqual(unit["unmapped_claim_run_ids"], [claiming["run_id"]])
        self.assertEqual(coverage["admission"], "ineligible")
        self.assertIn("unmapped_claim", [gap["code"] for gap in coverage["gaps"]])
        with self.assertRaisesRegex(ValueError, "still open"):
            enforce_coverage(
                acceptance,
                strict,
                SuccessOutcome.model_validate(
                    {
                        "outcome": "success",
                        "answer": "done",
                        "summary": "done",
                        "check_runs": [claiming],
                    }
                ),
            )


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.acceptance = declared(
            [
                {
                    "id": "AC-13",
                    "text": "theme",
                    "obligations": [
                        {
                            "id": "webkit",
                            "text": "applies in Safari",
                            "environment": {"browser": "webkit"},
                        },
                        {
                            "id": "chromium",
                            "text": "applies in Chrome",
                            "environment": {"browser": "chromium"},
                        },
                    ],
                }
            ]
        )
        self.check = plan(
            [
                {
                    "id": "theme",
                    "criterion": "theme applies",
                    "scope": BYTES,
                    "acceptance_refs": [
                        ref(self.acceptance, "AC-13", "webkit"),
                        ref(self.acceptance, "AC-13", "chromium"),
                    ],
                }
            ],
            coverage_scope=BYTES,
        )

    def unit(self, coverage, obligation):
        return next(
            row
            for row in coverage["units"]
            if row["ref"]["obligation_id"] == obligation
        )

    def test_one_browser_does_not_answer_for_another(self):
        observed = run(
            self.check.checks[0],
            self.acceptance,
            [("AC-13", "webkit"), ("AC-13", "chromium")],
            environment={"browser": "chromium"},
        )
        coverage = project(self.acceptance, self.check, [observed])
        self.assertEqual(
            self.unit(coverage, "chromium")["current_passing_run_ids"],
            [observed["run_id"]],
        )
        webkit = self.unit(coverage, "webkit")
        self.assertEqual(webkit["current_passing_run_ids"], [])
        self.assertEqual(webkit["environment_mismatch_run_ids"], [observed["run_id"]])
        self.assertIn("environment_mismatch", [gap["code"] for gap in coverage["gaps"]])

    def test_an_unrecorded_environment_is_unknown_not_a_match(self):
        observed = run(
            self.check.checks[0], self.acceptance, [("AC-13", "webkit")], environment={}
        )
        coverage = project(self.acceptance, self.check, [observed])
        webkit = self.unit(coverage, "webkit")
        self.assertEqual(webkit["current_passing_run_ids"], [])
        self.assertEqual(webkit["environment_unknown_run_ids"], [observed["run_id"]])

    def test_a_failure_and_an_older_pass_are_both_visible(self):
        failed = run(
            self.check.checks[0],
            self.acceptance,
            [("AC-13", "webkit")],
            environment={"browser": "webkit"},
            result="failed",
        )
        stale = run(
            self.check.checks[0],
            self.acceptance,
            [("AC-13", "webkit")],
            environment={"browser": "webkit"},
            scope=OTHER,
        )
        coverage = project(self.acceptance, self.check, [failed, stale])
        webkit = self.unit(coverage, "webkit")
        self.assertEqual(webkit["current_failed_run_ids"], [failed["run_id"]])
        self.assertEqual(webkit["other_bytes_run_ids"], [stale["run_id"]])
        self.assertEqual(webkit["current_passing_run_ids"], [])
        codes = [gap["code"] for gap in coverage["gaps"]]
        self.assertIn("current_failure", codes)
        self.assertIn("other_bytes", codes)

    def test_editing_the_check_leaves_its_old_runs_as_history(self):
        observed = run(
            self.check.checks[0],
            self.acceptance,
            [("AC-13", "webkit")],
            environment={"browser": "webkit"},
            check_revision="0" * 64,
        )
        coverage = project(self.acceptance, self.check, [observed])
        webkit = self.unit(coverage, "webkit")
        self.assertEqual(webkit["stale_reference_run_ids"], [observed["run_id"]])
        self.assertEqual(webkit["current_passing_run_ids"], [])

    def test_a_run_without_its_mapping_is_named_rather_than_credited(self):
        observed = run(self.check.checks[0], self.acceptance, [])
        coverage = project(self.acceptance, self.check, [observed])
        self.assertEqual(
            self.unit(coverage, "webkit")["mapping_unrecorded_run_ids"],
            [observed["run_id"]],
        )

    def test_evidence_about_unknown_bytes_is_not_evidence_about_these(self):
        bare = plan([{**self.check.checks[0].model_dump()}])
        observed = run(
            bare.checks[0],
            self.acceptance,
            [("AC-13", "webkit")],
            environment={"browser": "webkit"},
        )
        coverage = project(self.acceptance, bare, [observed], report_scope=None)
        self.assertEqual(coverage["target"]["source"], "unknown")
        self.assertEqual(
            self.unit(coverage, "webkit")["unknown_target_run_ids"],
            [observed["run_id"]],
        )


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.acceptance = declared(
            [{"id": "AC-1", "text": "one thing", "obligations": []}]
        )
        self.strict = plan(
            [
                {
                    "id": "only",
                    "criterion": "one thing holds",
                    "scope": BYTES,
                    "acceptance_refs": [ref(self.acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )

    def report(self, runs):
        return SuccessOutcome.model_validate(
            {
                "outcome": "success",
                "answer": "done",
                "summary": "done",
                "check_runs": runs,
            }
        )

    def test_report_only_refuses_nothing_and_still_shows_the_gap(self):
        relaxed = plan([{**self.strict.checks[0].model_dump()}], coverage_scope=BYTES)
        coverage = project(self.acceptance, relaxed, [])
        self.assertEqual(coverage["admission"], "not_enforced")
        self.assertIn("declared_unrun", [gap["code"] for gap in coverage["gaps"]])
        enforce_coverage(self.acceptance, relaxed, self.report([]))

    def test_strict_refuses_a_success_with_no_evidence(self):
        with self.assertRaisesRegex(ValueError, "still open"):
            enforce_coverage(self.acceptance, self.strict, self.report([]))

    def test_strict_accepts_a_success_whose_unit_has_a_current_pass(self):
        observed = run(self.strict.checks[0], self.acceptance, [("AC-1", None)])
        enforce_coverage(self.acceptance, self.strict, self.report([observed]))
        coverage = project(self.acceptance, self.strict, [observed])
        self.assertEqual(coverage["admission"], "eligible")
        # Even then the tool says nothing about whether the requirement is met.
        self.assertEqual(coverage["semantic_sufficiency"], "not_assessed")
        self.assertEqual(coverage["acceptance"], "not_assessed")

    def test_strict_without_a_declared_set_is_refused_rather_than_downgraded(self):
        with self.assertRaisesRegex(ValueError, "needs an acceptance_set"):
            enforce_coverage(None, self.strict, self.report([]))

    def test_an_empty_declaration_is_not_full_coverage(self):
        empty = declared([])
        coverage = project(empty, self.strict, [])
        self.assertEqual(coverage["denominator"]["evidence_units"], 0)
        self.assertEqual(coverage["admission"], "not_assessable")


class PersistedReportTests(unittest.TestCase):
    """A saved report is JSON: its scope arrives as a dict, not as a model."""

    def test_a_scope_read_back_from_storage_projects_the_same_way(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        checks = plan(
            [
                {
                    "id": "only",
                    "criterion": "one thing holds",
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ]
        )
        observed = run(checks.checks[0], acceptance, [("AC-1", None)])
        live = project(
            acceptance,
            checks,
            [observed],
            report_scope=CheckScope.model_validate(BYTES),
        )
        stored = project(acceptance, checks, [observed], report_scope=dict(BYTES))
        self.assertEqual(stored["target"], live["target"])
        self.assertEqual(
            stored["units"][0]["current_passing_run_ids"], [observed["run_id"]]
        )


class AnsweredFailureTests(unittest.TestCase):
    """A retry that answered a failure is history, exactly as check runs already say."""

    def test_a_superseding_pass_releases_its_failure(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        strict = plan(
            [
                {
                    "id": "only",
                    "criterion": "one thing holds",
                    "scope": BYTES,
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        failed = run(
            strict.checks[0],
            acceptance,
            [("AC-1", None)],
            result="failed",
            ended_at=1.0,
        )
        passed = run(
            strict.checks[0],
            acceptance,
            [("AC-1", None)],
            ended_at=2.0,
            supersedes=failed["run_id"],
        )
        coverage = project(acceptance, strict, [failed, passed])
        unit = coverage["units"][0]
        self.assertEqual(unit["current_failed_run_ids"], [])
        # The failure is not erased; it is recorded as answered.
        self.assertEqual(unit["answered_failure_run_ids"], [failed["run_id"]])
        self.assertEqual(coverage["admission"], "eligible")
        enforce_coverage(
            acceptance,
            strict,
            SuccessOutcome.model_validate(
                {
                    "outcome": "success",
                    "answer": "done",
                    "summary": "done",
                    "check_runs": [failed, passed],
                }
            ),
        )

    def test_an_unanswered_failure_still_blocks(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        strict = plan(
            [
                {
                    "id": "only",
                    "criterion": "one thing holds",
                    "scope": BYTES,
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        failed = run(
            strict.checks[0],
            acceptance,
            [("AC-1", None)],
            result="failed",
            ended_at=2.0,
        )
        earlier = run(strict.checks[0], acceptance, [("AC-1", None)], ended_at=1.0)
        coverage = project(acceptance, strict, [earlier, failed])
        self.assertEqual(
            coverage["units"][0]["current_failed_run_ids"], [failed["run_id"]]
        )
        self.assertEqual(coverage["admission"], "ineligible")


class SiblingEvidenceTests(unittest.TestCase):
    """A pass recorded for one obligation is no answer to another one's failure."""

    def test_a_siblings_pass_does_not_release_this_units_failure(self):
        acceptance = declared(
            [
                {
                    "id": "AC-1",
                    "text": "two clauses",
                    "obligations": [
                        {"id": "a", "text": "first clause"},
                        {"id": "b", "text": "second clause"},
                    ],
                }
            ]
        )
        strict = plan(
            [
                {
                    "id": "both",
                    "criterion": "the pair holds",
                    "scope": BYTES,
                    "acceptance_refs": [
                        ref(acceptance, "AC-1", "a"),
                        ref(acceptance, "AC-1", "b"),
                    ],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        check = strict.checks[0]
        first = run(check, acceptance, [("AC-1", "a")], ended_at=1.0)
        broke = run(check, acceptance, [("AC-1", "a")], result="failed", ended_at=2.0)
        sibling = run(check, acceptance, [("AC-1", "b")], ended_at=3.0)
        coverage = project(acceptance, strict, [first, broke, sibling])
        unit = next(
            row for row in coverage["units"] if row["ref"]["obligation_id"] == "a"
        )
        # The later pass records evidence for b only; a's failure stays open.
        self.assertEqual(unit["current_failed_run_ids"], [broke["run_id"]])
        self.assertEqual(unit["answered_failure_run_ids"], [])
        self.assertEqual(coverage["admission"], "ineligible")
        with self.assertRaisesRegex(ValueError, "still open"):
            enforce_coverage(
                acceptance,
                strict,
                SuccessOutcome.model_validate(
                    {
                        "outcome": "success",
                        "answer": "done",
                        "summary": "done",
                        "check_runs": [first, broke, sibling],
                    }
                ),
            )

    def test_a_retry_for_the_same_obligation_still_answers_it(self):
        acceptance = declared(
            [
                {
                    "id": "AC-1",
                    "text": "two clauses",
                    "obligations": [
                        {"id": "a", "text": "first clause"},
                        {"id": "b", "text": "second clause"},
                    ],
                }
            ]
        )
        strict = plan(
            [
                {
                    "id": "both",
                    "criterion": "the pair holds",
                    "scope": BYTES,
                    "acceptance_refs": [
                        ref(acceptance, "AC-1", "a"),
                        ref(acceptance, "AC-1", "b"),
                    ],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        check = strict.checks[0]
        broke = run(check, acceptance, [("AC-1", "a")], result="failed", ended_at=1.0)
        fixed = run(check, acceptance, [("AC-1", "a")], ended_at=2.0)
        other = run(check, acceptance, [("AC-1", "b")], ended_at=3.0)
        coverage = project(acceptance, strict, [broke, fixed, other])
        unit = next(
            row for row in coverage["units"] if row["ref"]["obligation_id"] == "a"
        )
        self.assertEqual(unit["answered_failure_run_ids"], [broke["run_id"]])
        self.assertEqual(unit["current_failed_run_ids"], [])
        self.assertEqual(coverage["admission"], "eligible")


class RecordedInputTests(unittest.TestCase):
    """Both gates must be satisfied by the same observation, not one each."""

    def test_a_run_of_a_different_command_cannot_carry_the_mapping(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        strict = plan(
            [
                {
                    "id": "C",
                    "criterion": "one thing holds",
                    "command": "pytest target.py",
                    "scope": BYTES,
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        check = strict.checks[0]
        # Carries the mapping and the right revision, but ran something else.
        mapped_but_other = run(
            check, acceptance, [("AC-1", None)], command="echo ok", ended_at=1.0
        )
        # Ran the declared command, but records no mapping at all.
        declared_but_unmapped = run(
            check, acceptance, [], command="pytest target.py", ended_at=2.0
        )
        coverage = project(
            acceptance, strict, [mapped_but_other, declared_but_unmapped]
        )
        unit = coverage["units"][0]
        self.assertEqual(unit["current_passing_run_ids"], [])
        self.assertEqual(unit["input_mismatch_run_ids"], [mapped_but_other["run_id"]])
        self.assertEqual(
            unit["mapping_unrecorded_run_ids"], [declared_but_unmapped["run_id"]]
        )
        self.assertEqual(coverage["admission"], "ineligible")
        self.assertIn("input_mismatch", [gap["code"] for gap in coverage["gaps"]])
        with self.assertRaisesRegex(ValueError, "still open"):
            enforce_coverage(
                acceptance,
                strict,
                SuccessOutcome.model_validate(
                    {
                        "outcome": "success",
                        "answer": "done",
                        "summary": "done",
                        "check_runs": [mapped_but_other, declared_but_unmapped],
                    }
                ),
            )

    def test_one_observation_satisfying_both_is_credited(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        strict = plan(
            [
                {
                    "id": "C",
                    "criterion": "one thing holds",
                    "command": "pytest target.py",
                    "scope": BYTES,
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        honest = run(
            strict.checks[0],
            acceptance,
            [("AC-1", None)],
            command="pytest target.py",
        )
        coverage = project(acceptance, strict, [honest])
        self.assertEqual(
            coverage["units"][0]["current_passing_run_ids"], [honest["run_id"]]
        )
        self.assertEqual(coverage["admission"], "eligible")


class CheckScopeTests(unittest.TestCase):
    """The check names its own bytes; both gates must mean the same ones."""

    def build(self):
        acceptance = declared([{"id": "AC-1", "text": "one thing", "obligations": []}])
        strict = plan(
            [
                {
                    "id": "C",
                    "criterion": "one thing holds",
                    "command": "pytest target.py",
                    # The check is about other bytes than the coverage target.
                    "scope": OTHER,
                    "acceptance_refs": [ref(acceptance, "AC-1")],
                }
            ],
            acceptance_coverage="require_current_evidence",
            coverage_scope=BYTES,
        )
        return acceptance, strict

    def test_a_run_outside_the_checks_own_bytes_earns_nothing(self):
        acceptance, strict = self.build()
        check = strict.checks[0]
        # Matches the coverage target and the mapping, but not the check's bytes.
        on_target = run(
            check,
            acceptance,
            [("AC-1", None)],
            command="pytest target.py",
            scope=BYTES,
            ended_at=1.0,
        )
        # Matches the check's bytes, but records no mapping at all.
        on_check = run(
            check, acceptance, [], command="pytest target.py", scope=OTHER, ended_at=2.0
        )
        coverage = project(acceptance, strict, [on_target, on_check])
        unit = coverage["units"][0]
        self.assertEqual(unit["current_passing_run_ids"], [])
        self.assertEqual(unit["check_scope_mismatch_run_ids"], [on_target["run_id"]])
        self.assertEqual(coverage["admission"], "ineligible")
        self.assertIn("check_scope_mismatch", [gap["code"] for gap in coverage["gaps"]])
        with self.assertRaisesRegex(ValueError, "still open"):
            enforce_coverage(
                acceptance,
                strict,
                SuccessOutcome.model_validate(
                    {
                        "outcome": "success",
                        "answer": "done",
                        "summary": "done",
                        "check_runs": [on_target, on_check],
                    }
                ),
            )


class UndeclaredTests(unittest.TestCase):
    def test_a_task_without_a_set_is_unassessed_not_uncovered(self):
        coverage = project(None, None, [], task_id=SET_ID)
        self.assertEqual(coverage["assessment"], "not_declared")
        self.assertEqual(coverage["reason"], "acceptance_set_absent")
        self.assertIsNone(coverage["denominator"])
        # No fabricated rows, no zero percent, no accusation.
        self.assertNotIn("units", coverage)
        self.assertNotIn("gaps", coverage)
        self.assertEqual(coverage["admission"], "not_enforced")


if __name__ == "__main__":
    unittest.main()
