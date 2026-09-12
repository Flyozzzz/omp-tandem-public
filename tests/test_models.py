"""Consumer regressions for structured collaboration declarations."""

from __future__ import annotations

import json
import unittest
from typing import ClassVar
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.models import (
    CheckRun,
    TaskContract,
    TaskScope,
    assess_checks,
    outcome_contract_error,
    outcome_schema,
    parse_outcome,
    run_applies,
)
from omp_tandem.runtime_models import PublishRequest

try:
    import jsonschema
except ImportError:  # pragma: no cover - optional in minimal environments
    jsonschema = None

TREE_A = "a" * 40
TREE_B = "b" * 40


def _run(check_id, result, *, role="author", digest=TREE_A, ended_at, **fields):
    record = {
        "check_id": check_id,
        "run_id": str(uuid4()),
        "criterion": fields.pop("criterion", f"{check_id} passes"),
        "role": role,
        "command": fields.pop("command", f"run {check_id}"),
        "environment": fields.pop("environment", {"platform": "darwin"}),
        "ended_at": ended_at,
        "scope": {"kind": "tree", "digest": digest},
        "result": result,
    }
    record.update(fields)
    return CheckRun.model_validate(record)


class ContractRegressionTests(unittest.TestCase):
    def test_outcome_requires_actual_nonblank_answer(self):
        for fields in ({}, {"answer": ""}, {"answer": " \n"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                parse_outcome(
                    {"outcome": "success", "summary": "Answer delivered", **fields}
                )

    def test_success_rejects_unresolved_work(self):
        conflicts = [
            {"blockers": ["Credentials unavailable"]},
            {"checks": [{"name": "Smoke run", "result": "failed"}]},
            {"checks": [{"name": "Smoke run", "result": "not_run"}]},
        ]
        for conflict in conflicts:
            with self.subTest(conflict=conflict), self.assertRaises(ValidationError):
                parse_outcome(
                    {
                        "outcome": "success",
                        "summary": "Completed",
                        "answer": "The requested implementation is incomplete.",
                        **conflict,
                    }
                )

    def test_blocked_requires_a_reason(self):
        for blockers in ([], ["", " \n\t"]):
            with self.subTest(blockers=blockers), self.assertRaises(ValidationError):
                parse_outcome(
                    {
                        "outcome": "blocked",
                        "summary": "Cannot continue",
                        "answer": "A required decision is unavailable.",
                        "blockers": blockers,
                    }
                )

    def test_unknown_outcome_is_refused_without_rewriting(self):
        with self.assertRaises(ValidationError):
            parse_outcome({"outcome": "done", "summary": "s", "answer": "a"})

    def test_scope_rejects_non_file_declarations(self):
        for path in (
            "../secret.txt",
            "src/../secret.txt",
            "src\\..\\secret.txt",
            "src/*.py",
            "src/?.py",
            "src/[ab].py",
            "src/{a,b}.py",
            "/tmp/file.py",
            "C:\\temp\\file.py",
            "C:file.py",
            "\\\\host\\share\\file.py",
            "",
            " \t",
            "src/",
            "src\\",
            ".",
            "src/.",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                TaskScope(owned_files=[path])

    def test_structured_unicode_context_limit_counts_characters_not_bytes(self):
        payload = {
            "goal": "Update the localized greeting",
            "context": "用户" * 30000,
            "scope": {"owned_files": ["src/本地化.py", "./tests/test_greeting.py"]},
            "constraints": ["Do not change public API signatures"],
            "acceptance": ["Greeting renders correctly in each supported locale"],
            "artifact_ids": ["ad89fd58-0138-40ca-afaf-80a10ba88093"],
        }
        TaskContract.model_validate_json(json.dumps(payload, ensure_ascii=False))
        payload["context"] += "字"
        with self.assertRaises(ValidationError):
            TaskContract.model_validate_json(json.dumps(payload, ensure_ascii=False))

    def test_artifact_references_require_canonical_uuid_spelling(self):
        for artifact_id in (
            "not-a-uuid",
            "AD89FD58-0138-40CA-AFAF-80A10BA88093",
            "ad89fd58013840caafaf80a10ba88093",
        ):
            with (
                self.subTest(model="TaskContract", artifact_id=artifact_id),
                self.assertRaises(ValidationError),
            ):
                TaskContract(goal="Read the artifact", artifact_ids=[artifact_id])
            with (
                self.subTest(model="TaskOutcome", artifact_id=artifact_id),
                self.assertRaises(ValidationError),
            ):
                parse_outcome(
                    {
                        "outcome": "partial",
                        "summary": "Read the artifact",
                        "answer": "The artifact could not be resolved.",
                        "artifact_ids": [artifact_id],
                    }
                )


class OutcomeSchemaContractTests(unittest.TestCase):
    """The registered schema and the runtime decoder come from one definition."""

    CASES: ClassVar[dict] = {
        "success with failed check": (
            {
                "outcome": "success",
                "summary": "s",
                "answer": "a",
                "checks": [{"name": "pytest", "result": "failed"}],
            },
            False,
        ),
        "success with blocker": (
            {"outcome": "success", "summary": "s", "answer": "a", "blockers": ["x"]},
            False,
        ),
        "blocked without blockers": (
            {"outcome": "blocked", "summary": "s", "answer": "a"},
            False,
        ),
        "blocked with blank blocker": (
            {"outcome": "blocked", "summary": "s", "answer": "a", "blockers": [" "]},
            False,
        ),
        "short success": ({"outcome": "success", "summary": "s", "answer": "a"}, True),
        "partial with open checks": (
            {
                "outcome": "partial",
                "summary": "s",
                "answer": "a",
                "checks": [{"name": "linux", "result": "not_run"}],
            },
            True,
        ),
        "blocked with reason": (
            {"outcome": "blocked", "summary": "s", "answer": "a", "blockers": ["x"]},
            True,
        ),
    }

    def test_schema_uses_structural_variants_without_conditionals(self):
        schema = outcome_schema()
        self.assertEqual(schema["type"], "object")
        refs = [variant["$ref"].rsplit("/", 1)[-1] for variant in schema["anyOf"]]
        self.assertEqual(refs, ["SuccessOutcome", "PartialOutcome", "BlockedOutcome"])
        text = json.dumps(schema)
        for keyword in ('"if"', '"then"', '"else"', '"oneOf"', '"not"'):
            self.assertNotIn(keyword, text)
        success = schema["$defs"]["SuccessOutcome"]["properties"]
        self.assertEqual(success["outcome"]["const"], "success")
        self.assertEqual(success["blockers"]["maxItems"], 0)
        self.assertEqual(
            schema["$defs"]["PassedCheck"]["properties"]["result"]["const"], "passed"
        )
        blocked = schema["$defs"]["BlockedOutcome"]["properties"]
        self.assertEqual(blocked["blockers"]["minItems"], 1)

    def test_schema_and_runtime_agree_on_every_case(self):
        schema = outcome_schema()
        validator = (
            jsonschema.Draft202012Validator(schema) if jsonschema is not None else None
        )
        for name, (document, expected) in self.CASES.items():
            with self.subTest(case=name):
                try:
                    parse_outcome(document)
                    runtime = True
                except ValidationError:
                    runtime = False
                self.assertEqual(runtime, expected)
                if validator is not None:
                    self.assertEqual(validator.is_valid(document), expected)

    def test_refusal_names_fields_and_keeps_declared_outcome(self):
        with self.assertRaises(ValidationError) as failure:
            parse_outcome(
                {
                    "outcome": "success",
                    "summary": "s",
                    "answer": "a",
                    "checks": [{"name": "pytest", "result": "failed"}],
                    "blockers": ["waiting"],
                }
            )
        envelope = outcome_contract_error(failure.exception)
        self.assertEqual(envelope["code"], "outcome_contract")
        self.assertTrue(
            any(field.startswith("checks.0") for field in envelope["fields"])
        )
        self.assertIn("blockers", envelope["fields"])
        self.assertIn("partial", envelope["allowed_fix"])

    def test_participant_reports_cannot_claim_machine_observed_runs(self):
        run = _run("pytest", "passed", ended_at=1.0).model_dump()
        run["provenance"] = "machine_observed"
        with self.assertRaises(ValidationError):
            parse_outcome(
                {
                    "outcome": "partial",
                    "summary": "s",
                    "answer": "a",
                    "check_runs": [run],
                }
            )
        run["provenance"] = "participant_reported"
        report = parse_outcome(
            {"outcome": "partial", "summary": "s", "answer": "a", "check_runs": [run]}
        )
        self.assertEqual(report.check_runs[0].provenance, "participant_reported")


class CheckRunApplicabilityTests(unittest.TestCase):
    def test_history_is_visible_and_later_pass_is_a_new_observation(self):
        failed = _run("pytest", "failed", ended_at=1.0)
        passed = _run("pytest", "passed", ended_at=2.0)
        assessment = assess_checks([failed, passed])
        self.assertEqual(assessment["status"], "passed")
        criterion = assessment["criteria"][0]
        self.assertEqual(criterion["current_run_id"], passed.run_id)
        self.assertEqual(criterion["run_ids"], [failed.run_id, passed.run_id])
        self.assertEqual(
            assessment["known_issues"][0]["failed_run_id"],
            failed.run_id,
            "the unexplained failure stays a visible known issue after the pass",
        )

    def test_equal_tree_keeps_applicability_but_changed_bytes_do_not(self):
        passed = _run("pytest", "passed", ended_at=1.0, digest=TREE_A)
        self.assertTrue(run_applies(passed, {"kind": "tree", "digest": TREE_A}))
        self.assertFalse(run_applies(passed, {"kind": "tree", "digest": TREE_B}))
        self.assertFalse(run_applies(passed, {"kind": "commit", "digest": TREE_A}))
        same_tree = assess_checks([passed], {"kind": "tree", "digest": TREE_A})
        self.assertEqual(same_tree["status"], "passed")
        new_bytes = assess_checks([passed], {"kind": "tree", "digest": TREE_B})
        self.assertEqual(new_bytes["status"], "not_run")
        self.assertEqual(new_bytes["criteria"][0]["applicable_run_ids"], [])

    def test_forged_supersedes_cannot_clear_a_current_failure(self):
        failed = _run("pytest", "failed", ended_at=1.0)
        other_criterion = _run(
            "pytest",
            "passed",
            ended_at=2.0,
            criterion="lint passes",
            supersedes=failed.run_id,
        )
        other_role = _run(
            "pytest", "passed", ended_at=2.0, role="reviewer", supersedes=failed.run_id
        )
        other_bytes = _run(
            "pytest", "passed", ended_at=2.0, digest=TREE_B, supersedes=failed.run_id
        )
        other_command = _run(
            "pytest",
            "passed",
            ended_at=2.0,
            command="run pytest -k smoke",
            supersedes=failed.run_id,
        )
        for forged in (other_criterion, other_role, other_bytes, other_command):
            with self.subTest(forged=forged.run_id):
                assessment = assess_checks(
                    [failed, forged], {"kind": "tree", "digest": TREE_A}
                )
                pytest_author = next(
                    item
                    for item in assessment["criteria"]
                    if item["criterion"] == "pytest passes" and item["role"] == "author"
                )
                self.assertEqual(pytest_author["status"], "failed")
                self.assertEqual(
                    [item["run_id"] for item in assessment["invalid_supersedes"]],
                    [forged.run_id],
                )
        genuine = _run("pytest", "passed", ended_at=2.0, supersedes=failed.run_id)
        assessment = assess_checks(
            [failed, genuine], {"kind": "tree", "digest": TREE_A}
        )
        self.assertEqual(assessment["status"], "passed")
        self.assertEqual(assessment["invalid_supersedes"], [])

    def test_nonpassing_successor_never_discharges_a_failure(self):
        failed = _run("pytest", "failed", ended_at=1.0)
        not_run = _run("pytest", "not_run", ended_at=3.0, supersedes=failed.run_id)
        smoke = _run("pytest", "passed", ended_at=4.0, command="run pytest -k smoke")
        assessment = assess_checks([failed, not_run, smoke])
        self.assertEqual(assessment["status"], "failed")
        self.assertEqual(
            assessment["criteria"][0]["open_failure_run_ids"], [failed.run_id]
        )
        self.assertEqual(assessment["known_issues"], [])
        self.assertEqual(assessment["invalid_supersedes"], [])
        only_not_run = assess_checks([failed, not_run])
        self.assertEqual(only_not_run["status"], "failed")
        self.assertEqual(only_not_run["known_issues"], [])
        failed_again = _run("pytest", "failed", ended_at=5.0, supersedes=failed.run_id)
        self.assertEqual(assess_checks([failed, failed_again])["status"], "failed")

    def test_run_ids_are_required_and_reserved_names_are_refused(self):
        base = _run("pytest", "passed", ended_at=1.0).model_dump()
        del base["run_id"]
        with self.assertRaises(ValidationError):
            CheckRun.model_validate(base)
        for name in ("tandem:check-run", "TANDEM:anything", " tandem:x"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                PublishRequest(name=name, content="{}")
        PublishRequest(name="check-run", content="{}")

    def test_author_runs_do_not_satisfy_reviewer_criteria(self):
        author = _run("pytest", "passed", ended_at=1.0)
        reviewer = _run("pytest", "not_run", ended_at=2.0, role="reviewer")
        assessment = assess_checks([author, reviewer])
        by_role = {item["role"]: item["status"] for item in assessment["criteria"]}
        self.assertEqual(by_role, {"author": "passed", "reviewer": "not_run"})
        self.assertEqual(assessment["status"], "not_run")

    def test_run_records_reject_impossible_shapes(self):
        base = _run("pytest", "passed", ended_at=1.0).model_dump()
        for bad in (
            {"ended_at": 0.5, "started_at": 1.0},
            {"supersedes": base["run_id"]},
            {"scope": {"kind": "tree", "digest": "not hex"}},
            {"role": "auditor"},
            {"environment": {str(i): "x" for i in range(25)}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                CheckRun.model_validate({**base, **bad})


if __name__ == "__main__":
    unittest.main()
