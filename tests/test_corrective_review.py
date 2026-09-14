"""Immutable corrections, compact mandatory packets, and explicit verification gates."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.findings import FindingStore
from omp_tandem.models import VerificationPlan, decode_outcome
from omp_tandem.reviews import ReviewRequest, ReviewStore
from omp_tandem.verification import enforce_verification, verification_requirements
from omp_tandem.work_adapters import _prompt
from omp_tandem.workspace import resolve_scope


class CorrectiveReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.root = home / "project"
        self.root.mkdir()
        self.scope = resolve_scope(home / "state", self.root)
        self.db_path = self.scope.directory / "state.sqlite3"
        self.store = ReviewStore(self.db_path, self.scope, ArtifactStore(self.db_path))
        self.findings = FindingStore(self.db_path)
        self.conversation = str(uuid4())
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE tasks (conversation_id TEXT)")
            db.execute("INSERT INTO tasks VALUES (?)", (self.conversation,))
        (self.root / "source.py").write_text("broken\n")
        (self.root / "context.txt").write_text("old boundary\n")
        self.previous = self.capture(context_paths=["context.txt"])
        self.finding = self.findings.create(
            self.conversation,
            self.previous["review_id"],
            {
                "title": "Missing boundary check",
                "description": "The source fails to preserve its declared boundary.",
                "location": {"path": "source.py"},
                "reproduction_conditions": ["Supply an out-of-range value."],
                "evidence": ["The value escaped the declared range."],
                "reason": "Preserve the boundary.",
            },
        )

    def capture(self, **values):
        return self.store.create(
            ReviewRequest(
                requirements="Preserve the boundary",
                paths=["source.py"],
                author_rationale="PRIVATE AUTHOR RATIONALE",
                **values,
            )
        )

    def correction(self, **values):
        return {
            "previous_review_id": self.previous["review_id"],
            "previous_code_fingerprint": self.previous["code_fingerprint"],
            "open_findings": [
                {
                    "finding_id": self.finding["finding_id"],
                    "expected_revision": 1,
                }
            ],
            **values,
        }

    def test_delta_and_finding_references_remain_pinned_after_live_changes(self):
        (self.root / "source.py").write_text("fixed\n")
        (self.root / "extra.txt").write_text("new boundary\n")
        current = self.capture(
            context_paths=["extra.txt"], corrective=self.correction()
        )
        correction = current["corrective"]
        self.assertEqual(correction["exposure"], "prior_exposed")
        self.assertFalse(correction["previous_verdict_reused"])
        self.assertEqual(correction["applicability"]["review_id"], current["review_id"])
        self.assertEqual(
            [item["path"] for item in correction["delta"]["changed"]], ["source.py"]
        )
        self.assertEqual(
            [item["path"] for item in correction["delta"]["added"]], ["extra.txt"]
        )
        self.assertEqual(
            [item["path"] for item in correction["delta"]["removed"]], ["context.txt"]
        )
        self.findings.update(
            self.finding["finding_id"],
            {
                "action": "note",
                "review_id": self.previous["review_id"],
                "reason": "New observation",
            },
            1,
        )
        (self.root / "source.py").write_text("later live mutation\n")
        self.assertEqual(
            self.store.info(current["review_id"])["corrective"], correction
        )
        manifest = self.store.read(current["review_id"], limit=50000)["content"]
        self.assertNotIn("PRIVATE AUTHOR RATIONALE", manifest)
        with self.assertRaises(ValueError):
            self.store.read(current["review_id"], "author")
        self.assertEqual(
            self.store.read(current["review_id"], "selected", "source.py")["content"],
            "fixed\n",
        )
        with self.assertRaises(ValueError):
            self.capture(corrective=self.correction())

    def test_foreign_snapshot_and_stale_or_foreign_finding_refs_refused(self):
        for correction in (
            self.correction(previous_review_id=str(uuid4())),
            self.correction(previous_code_fingerprint="0" * 64),
            self.correction(
                open_findings=[{"finding_id": str(uuid4()), "expected_revision": 1}]
            ),
            self.correction(
                open_findings=[
                    {"finding_id": self.finding["finding_id"], "expected_revision": 2}
                ]
            ),
        ):
            with self.subTest(correction=correction), self.assertRaises(ValueError):
                self.capture(corrective=correction)
        other = self.capture()
        with self.assertRaises(ValueError):
            self.capture(
                corrective=self.correction(
                    previous_review_id=other["review_id"],
                    previous_code_fingerprint=other["code_fingerprint"],
                )
            )
        self.findings.update(
            self.finding["finding_id"],
            {
                "action": "reject",
                "review_id": self.previous["review_id"],
                "reason": "Contradicted by reproduction",
                "evidence": ["Boundary preserved"],
            },
            1,
        )
        with self.assertRaises(ValueError):
            self.capture(
                corrective=self.correction(
                    open_findings=[
                        {
                            "finding_id": self.finding["finding_id"],
                            "expected_revision": 2,
                        }
                    ]
                )
            )


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.plan = VerificationPlan.model_validate(
            {
                "stage": "candidate",
                "preparation_seconds": 7,
                "checks": [
                    {
                        "id": "release",
                        "criterion": "Installed package works",
                        "phase": "integration",
                        "command": "release-command",
                    },
                    {
                        "id": "candidate",
                        "criterion": "Candidate behavior works",
                        "phase": "candidate",
                        "command": "candidate-command",
                        "estimated_seconds": 11,
                    },
                    {
                        "id": "targeted",
                        "criterion": "Boundary holds",
                        "estimated_seconds": 3,
                    },
                ],
            }
        )

    def report(self):
        checks, runs = [], []
        for check in verification_requirements(self.plan)["checks"]:
            identifier = str(uuid4())
            checks.append(
                {
                    "name": check["criterion"],
                    "check_id": check["id"],
                    "result": "passed",
                    "run_id": identifier,
                }
            )
            runs.append(
                {
                    "check_id": check["id"],
                    "run_id": identifier,
                    "criterion": check["criterion"],
                    "command": check["command"],
                    "role": "author",
                    "scope": {"kind": "content", "digest": "a" * 64},
                    "result": "passed",
                }
            )
        return {
            "outcome": "success",
            "answer": "Boundary preserved",
            "summary": "Verified",
            "checks": checks,
            "check_runs": runs,
        }

    def test_selected_order_budget_and_unknown_estimates_never_execute(self):
        with patch("subprocess.run", side_effect=AssertionError("must not execute")):
            result = verification_requirements(self.plan)
            self.assertEqual(
                [check["id"] for check in result["checks"]], ["targeted", "candidate"]
            )
            self.assertEqual(result["estimated_seconds"], 21)
            self.assertTrue(result["requires_shell"])
            self.assertTrue(result["estimates_complete"])
            expanded = verification_requirements(
                self.plan.model_copy(update={"stage": "integration"})
            )
            self.assertFalse(expanded["estimates_complete"])
            self.assertEqual(expanded["estimated_seconds"], 21)

    def test_omission_or_historical_pass_cannot_satisfy_current_checks(self):
        report = self.report()
        report["checks"].pop()
        with self.assertRaises(ValueError):
            enforce_verification(self.plan, decode_outcome(report))
        report = self.report()
        report["check_runs"].append(
            {**report["check_runs"][-1], "run_id": str(uuid4()), "result": "failed"}
        )
        with self.assertRaises(ValueError):
            enforce_verification(self.plan, decode_outcome(report))
        report = self.report()
        report["check_runs"][0]["command"] = "different-command"
        with self.assertRaises(ValueError):
            enforce_verification(self.plan, decode_outcome(report))

    def test_current_pass_keeps_historical_failure_and_legacy_reports_parse(self):
        report = self.report()
        historical = {
            **report["check_runs"][0],
            "run_id": str(uuid4()),
            "result": "failed",
        }
        report["check_runs"].insert(0, historical)
        decoded = decode_outcome(report)
        enforce_verification(self.plan, decoded)
        self.assertEqual(decoded.check_runs[0].result, "failed")
        legacy = decode_outcome(
            {"outcome": "success", "answer": "Delivered", "summary": "Done"}
        )
        enforce_verification(None, legacy)
        self.assertEqual(legacy.outcome, "success")

    def test_capsule_preserves_global_rules_without_other_step_reports(self):
        step = {
            "id": "assigned",
            "title": "Assigned",
            "goal": "Deliver boundary fix",
            "owner": "omp",
            "reviewer": "claude",
            "owned_files": ["source.py"],
            "depends_on": ["dependency"],
            "acceptance": ["Reject out of range values"],
            "verification": self.plan.model_dump(),
        }
        plan = {
            "title": "Boundary project",
            "goal": "Preserve all boundaries",
            "constraints": ["Never relax the limit", "No provider secrets"],
            "acceptance": ["Existing callers continue working"],
            "context": "long context " * 4000,
            "steps": [
                {
                    **step,
                    "id": "dependency",
                    "goal": "UNRELATED FULL GOAL" * 500,
                    "acceptance": ["UNRELATED CRITERIA" * 500],
                    "depends_on": [],
                },
                step,
            ],
        }
        attempt = {
            "attempt_id": str(uuid4()),
            "work_id": str(uuid4()),
            "step_id": "assigned",
            "kind": "review",
            "protocol": "independent_first",
            "plan_revision": 4,
            "allow_shell": False,
            "dependencies": [
                {
                    "step_id": "dependency",
                    "submission_id": str(uuid4()),
                    "commit": "a" * 40,
                    "answer": "PRIVATE AUTHOR ANSWER",
                    "evidence": ["PRIVATE AUTHOR RATIONALE"],
                    "token": "PRIVATE CAPABILITY",
                }
            ],
        }
        prompt = _prompt(attempt, plan, {"path": "/saved/worktree"})
        capsule = json.loads(prompt.split("Saved attempt context:\n", 1)[1])
        self.assertEqual(capsule["plan"]["constraints"], plan["constraints"])
        self.assertEqual(capsule["plan"]["acceptance"], plan["acceptance"])
        self.assertEqual(
            capsule["plan"]["assigned_step"]["acceptance"], step["acceptance"]
        )
        self.assertEqual(capsule["plan"]["assigned_step"]["owned_files"], ["source.py"])
        self.assertIsNone(capsule["verification"])
        self.assertEqual(
            capsule["dependencies"][0]["submission_id"],
            attempt["dependencies"][0]["submission_id"],
        )
        for withheld in (
            "PRIVATE AUTHOR ANSWER",
            "PRIVATE AUTHOR RATIONALE",
            "PRIVATE CAPABILITY",
            "UNRELATED FULL GOAL",
            "UNRELATED CRITERIA",
        ):
            self.assertNotIn(withheld, prompt)
        self.assertLess(len(prompt), len(json.dumps(plan)))
