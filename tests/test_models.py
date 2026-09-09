"""Consumer regressions for structured collaboration declarations."""

from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from omp_tandem.models import TaskContract, TaskOutcome, TaskScope


class ContractRegressionTests(unittest.TestCase):
    def test_outcome_requires_actual_nonblank_answer(self):
        for fields in ({}, {"answer": ""}, {"answer": " \n"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                TaskOutcome(outcome="success", summary="Answer delivered", **fields)

    def test_success_rejects_unresolved_work(self):
        conflicts = [
            {"blockers": ["Credentials unavailable"]},
            {"checks": [{"name": "Smoke run", "result": "failed"}]},
            {"checks": [{"name": "Smoke run", "result": "not_run"}]},
        ]
        for conflict in conflicts:
            with self.subTest(conflict=conflict), self.assertRaises(ValidationError):
                TaskOutcome(
                    outcome="success",
                    summary="Completed",
                    answer="The requested implementation is incomplete.",
                    **conflict,
                )

    def test_blocked_requires_a_reason(self):
        for blockers in ([], ["", " \n\t"]):
            with self.subTest(blockers=blockers), self.assertRaises(ValidationError):
                TaskOutcome(
                    outcome="blocked",
                    summary="Cannot continue",
                    answer="A required decision is unavailable.",
                    blockers=blockers,
                )

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
            for model, fields in (
                (TaskContract, {"goal": "Read the artifact"}),
                (
                    TaskOutcome,
                    {
                        "outcome": "partial",
                        "summary": "Read the artifact",
                        "answer": "The artifact could not be resolved.",
                    },
                ),
            ):
                with (
                    self.subTest(model=model.__name__, artifact_id=artifact_id),
                    self.assertRaises(ValidationError),
                ):
                    model(**fields, artifact_ids=[artifact_id])


if __name__ == "__main__":
    unittest.main()
