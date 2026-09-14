"""Corrective finding provenance and honest chronological verification admission."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.findings import FindingConflict, FindingStore
from omp_tandem.models import VerificationPlan, assess_checks, decode_outcome
from omp_tandem.reviews import ReviewRequest, ReviewStore
from omp_tandem.verification import assess_verification, enforce_verification
from omp_tandem.workspace import resolve_scope


class CorrectiveFindingIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.root = home / "project"
        self.root.mkdir()
        scope = resolve_scope(home / "state", self.root)
        self.path = scope.directory / "state.sqlite3"
        self.reviews = ReviewStore(self.path, scope, ArtifactStore(self.path))
        self.findings = FindingStore(self.path)
        self.original_conversation = str(uuid4())
        self.corrective_conversation = str(uuid4())
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    review_id TEXT, status TEXT NOT NULL, report_json TEXT,
                    review_run_id TEXT, review_stage TEXT);
                CREATE TABLE review_runs (
                    run_id TEXT PRIMARY KEY, review_id TEXT,
                    independent_task_id TEXT, comparison_task_id TEXT);
            """)
        (self.root / "source.py").write_text("broken\n")
        self.previous = self.capture()
        self.add_task(self.previous, conversation=self.original_conversation)
        self.draft = {
            "title": "Missing boundary check",
            "description": "Out-of-range input escapes validation.",
            "location": {"path": "source.py", "start_line": 1},
            "reproduction_conditions": ["Supply an out-of-range value."],
            "evidence": ["The observed value escaped its range."],
            "reason": "PRIVATE ORIGINAL REASON",
            "validity": "confirmed",
        }
        original = self.findings.create(
            self.original_conversation, self.previous["review_id"], self.draft
        )
        self.finding = self.change(original, "claim_fixed", self.previous)
        (self.root / "source.py").write_text("fixed\n")
        self.current = self.capture(
            corrective={
                "previous_review_id": self.previous["review_id"],
                "previous_code_fingerprint": self.previous["code_fingerprint"],
                "open_findings": [
                    {
                        "finding_id": self.finding["finding_id"],
                        "expected_revision": self.finding["revision"],
                    }
                ],
            }
        )
        self.run_id = str(uuid4())
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO review_runs VALUES (?, ?, NULL, NULL)",
                (self.run_id, self.current["review_id"]),
            )
        self.independent = self.add_task(
            self.current, stage="independent", status="running"
        )
        self.comparison = self.add_task(self.current, stage="comparison")

    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def capture(self, **values):
        return self.reviews.create(
            ReviewRequest(
                requirements="Preserve the boundary",
                paths=["source.py"],
                author_rationale="PRIVATE AUTHOR RATIONALE",
                **values,
            )
        )

    def add_task(
        self, review, *, stage=None, conversation=None, status="completed", managed=True
    ):
        identifier = str(uuid4())
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    conversation or self.corrective_conversation,
                    review["review_id"],
                    status,
                    json.dumps({"outcome": "success"}),
                    self.run_id if stage and managed else None,
                    stage,
                ),
            )
            if stage and managed:
                db.execute(
                    f"UPDATE review_runs SET {stage}_task_id=? WHERE run_id=?",
                    (identifier, self.run_id),
                )
        return identifier

    def change(self, finding, action, review, *, task_id=None, **values):
        return self.findings.update(
            finding["finding_id"],
            {
                "action": action,
                "review_id": review["review_id"],
                "reason": "PRIVATE FIX RATIONALE",
                "evidence": ["PRIVATE LATER FIX EVIDENCE"],
                **values,
            },
            finding["revision"],
            task_id=task_id,
        )

    def read_manifest(self):
        chunks, offset = [], 0
        while True:
            page = self.reviews.read(
                self.current["review_id"], offset=offset, limit=113
            )
            chunks.append(page["content"])
            offset = page["next_offset"]
            if offset is None:
                return json.loads("".join(chunks))

    def test_corrective_reader_keeps_original_details_without_author_history(self):
        self.change(self.finding, "note", self.previous)
        changed_draft = {
            **self.draft,
            "title": "Later title",
            "evidence": ["Later evidence"],
        }
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE findings SET draft_json=? WHERE finding_id=?",
                (json.dumps(changed_draft), self.finding["finding_id"]),
            )
        manifest = self.read_manifest()
        captured = manifest["corrective"]["finding_details"][0]
        self.assertEqual(captured["title"], self.draft["title"])
        self.assertEqual(captured["description"], self.draft["description"])
        self.assertEqual(
            captured["reproduction_conditions"], self.draft["reproduction_conditions"]
        )
        self.assertEqual(captured["evidence"], self.draft["evidence"])
        self.assertEqual(
            captured["location"],
            {
                **self.draft["location"],
                "end_line": None,
                "review_id": self.previous["review_id"],
            },
        )
        self.assertEqual(captured["provenance"]["revision"], 2)
        self.assertEqual(
            captured["provenance"]["review_id"], self.previous["review_id"]
        )
        self.assertEqual(
            captured["provenance"]["evidence_status"], "recorded_not_reproduced"
        )
        self.assertNotIn("PRIVATE", json.dumps(manifest))
        self.assertEqual(
            self.findings.get(self.finding["finding_id"])["title"], "Later title"
        )

    def test_linked_report_updates_then_completed_task_verifies_exact_snapshot(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            self.findings.ingest_report(
                db,
                self.independent,
                finding_updates=[
                    {
                        "finding_id": self.finding["finding_id"],
                        "expected_revision": 2,
                        "change": {
                            "action": "note",
                            "review_id": self.current["review_id"],
                            "reason": "Reproduced against the captured correction",
                            "evidence": ["Boundary is preserved on the new bytes"],
                        },
                    }
                ],
            )
            db.commit()
        updated = self.findings.get(self.finding["finding_id"])
        with self.assertRaises(ValueError):
            self.change(
                updated,
                "verify_fixed",
                self.current,
                verification_task_id=self.independent,
            )
        verified = self.change(
            updated, "verify_fixed", self.current, verification_task_id=self.comparison
        )
        self.assertEqual(verified["resolution"], "verified_fixed")
        self.assertEqual(verified["verified_review_id"], self.current["review_id"])
        self.assertEqual(verified["location"]["review_id"], self.previous["review_id"])
        self.assertEqual(verified["history"][:2], self.finding["history"])
        later = self.capture()
        advanced = self.change(verified, "note", later)
        self.assertFalse(advanced["verified_for_review"])
        self.assertEqual(advanced["verified_review_id"], self.current["review_id"])

    def test_low_level_corrective_stages_update_and_verify_prior_finding(self):
        independent = self.add_task(
            self.current, stage="independent", managed=False, status="running"
        )
        comparison = self.add_task(self.current, stage="comparison", managed=False)
        updated = self.change(self.finding, "note", self.current, task_id=independent)
        verified = self.change(
            updated, "verify_fixed", self.current, verification_task_id=comparison
        )
        self.assertEqual(verified["resolution"], "verified_fixed")
        self.assertEqual(verified["verified_review_id"], self.current["review_id"])
        self.assertEqual(verified["history"][-1]["verification_task_id"], comparison)

    def test_unlinked_undeclared_wrong_snapshot_and_failed_tasks_cannot_verify(self):
        unlinked = self.add_task(self.current)
        wrong_snapshot = self.add_task(self.previous)
        for task in (unlinked, wrong_snapshot):
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.change(
                    self.finding,
                    "verify_fixed",
                    self.current,
                    verification_task_id=task,
                )
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE tasks SET review_stage='comparison', review_run_id=? WHERE task_id=?",
                (self.run_id, unlinked),
            )
        with self.assertRaises(ValueError):
            self.change(
                self.finding,
                "verify_fixed",
                self.current,
                verification_task_id=unlinked,
            )
        undeclared = self.findings.create(
            self.original_conversation, self.previous["review_id"], self.draft
        )
        undeclared = self.change(undeclared, "claim_fixed", self.previous)
        with self.assertRaises(ValueError):
            self.change(
                undeclared,
                "verify_fixed",
                self.current,
                verification_task_id=self.comparison,
            )
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE tasks SET report_json=? WHERE task_id=?",
                (json.dumps({"outcome": "partial"}), self.comparison),
            )
        with self.assertRaises(ValueError):
            self.change(
                self.finding,
                "verify_fixed",
                self.current,
                verification_task_id=self.comparison,
            )
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE tasks SET report_json=? WHERE task_id=?",
                (json.dumps({"outcome": "success"}), self.comparison),
            )
        verified = self.change(
            self.finding,
            "verify_fixed",
            self.current,
            verification_task_id=self.comparison,
        )
        self.assertEqual(verified["resolution"], "verified_fixed")
        self.assertEqual(
            self.findings.get(undeclared["finding_id"])["resolution"], "claimed_fixed"
        )

    def test_live_revision_change_invalidates_corrective_authorization(self):
        changed = self.change(self.finding, "note", self.previous)
        with self.assertRaises(FindingConflict):
            self.change(
                changed,
                "verify_fixed",
                self.current,
                verification_task_id=self.comparison,
            )
        with self.assertRaises(FindingConflict):
            self.change(self.finding, "note", self.current, task_id=self.independent)
        self.assertEqual(self.findings.get(changed["finding_id"]), changed)


class VerificationHistoryIntegrityTests(unittest.TestCase):
    def plan_report(self, count=1):
        checks, claims, runs = [], [], []
        for index in range(count):
            identifier = f"check-{index}"
            criterion = f"Boundary {index} holds"
            run_id = str(uuid4())
            checks.append(
                {"id": identifier, "criterion": criterion, "command": "verify"}
            )
            claims.append(
                {
                    "name": criterion,
                    "check_id": identifier,
                    "run_id": run_id,
                    "result": "passed",
                }
            )
            runs.append(
                {
                    "check_id": identifier,
                    "run_id": run_id,
                    "criterion": criterion,
                    "role": "author",
                    "command": "verify",
                    "ended_at": 20,
                    "scope": {"kind": "content", "digest": "a" * 64},
                    "result": "passed",
                }
            )
        return VerificationPlan(checks=checks), {
            "outcome": "success",
            "answer": "Boundaries hold",
            "summary": "Verified",
            "checks": claims,
            "check_runs": runs,
        }

    def test_reverse_ordered_current_pass_is_accepted_with_failure_retained(self):
        plan, payload = self.plan_report()
        failed = {
            **payload["check_runs"][0],
            "run_id": str(uuid4()),
            "result": "failed",
            "ended_at": 10,
        }
        payload["check_runs"].append(failed)
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        assessed = assess_checks(report.check_runs)
        self.assertEqual(assessed["status"], "passed")
        self.assertEqual(assessed["known_issues"][0]["failed_run_id"], failed["run_id"])

    def test_older_pass_cannot_replace_newer_failure_by_array_position(self):
        plan, payload = self.plan_report()
        failed = {
            **payload["check_runs"][0],
            "run_id": str(uuid4()),
            "result": "failed",
            "ended_at": 30,
        }
        payload["check_runs"].insert(0, failed)
        report = decode_outcome(payload)
        with self.assertRaises(ValueError):
            enforce_verification(plan, report)
        self.assertEqual(assess_checks(report.check_runs)["status"], "failed")

    def test_fifty_required_checks_can_retain_failed_attempt_and_complete(self):
        plan, payload = self.plan_report(50)
        failed = {
            **payload["check_runs"][0],
            "run_id": str(uuid4()),
            "result": "failed",
            "ended_at": 10,
        }
        payload["check_runs"].insert(0, failed)
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        assessed = assess_checks(report.check_runs)
        self.assertEqual(assessed["status"], "passed")
        self.assertEqual(assessed["known_issues"][0]["failed_run_id"], failed["run_id"])
        self.assertEqual(assessed["run_count"], 51)

    def test_different_environment_or_bytes_does_not_discharge_failure(self):
        for changed in (
            {"environment": {"os": "different"}},
            {"scope": {"kind": "content", "digest": "b" * 64}},
        ):
            plan, payload = self.plan_report()
            failed = {
                **payload["check_runs"][0],
                **changed,
                "run_id": str(uuid4()),
                "result": "failed",
                "ended_at": 10,
            }
            payload["check_runs"].insert(0, failed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                enforce_verification(plan, decode_outcome(payload))

    def test_explicit_current_scope_keeps_other_bytes_as_history_not_current_failure(
        self,
    ):
        plan, payload = self.plan_report()
        current = payload["check_runs"][0]
        failed = {
            **current,
            "scope": {"kind": "content", "digest": "b" * 64},
            "run_id": str(uuid4()),
            "result": "failed",
            "ended_at": 10,
        }
        payload["check_runs"].insert(0, failed)
        plan = VerificationPlan.model_validate(
            {
                **plan.model_dump(),
                "checks": [{**plan.checks[0].model_dump(), "scope": current["scope"]}],
            }
        )
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        self.assertEqual(
            assess_verification(plan, report.check_runs)["status"], "passed"
        )
        self.assertEqual(assess_checks(report.check_runs)["status"], "failed")
        self.assertEqual(report.check_runs[0].result, "failed")
        payload["checks"][0]["run_id"] = failed["run_id"]
        with self.assertRaises(ValueError):
            enforce_verification(plan, decode_outcome(payload))

    def test_current_failure_in_another_role_cannot_be_hidden_by_claimed_pass(self):
        plan, payload = self.plan_report()
        failed = {
            **payload["check_runs"][0],
            "run_id": str(uuid4()),
            "role": "reviewer",
            "result": "failed",
            "ended_at": 10,
        }
        payload["check_runs"].insert(0, failed)
        report = decode_outcome(payload)
        self.assertEqual(
            assess_verification(plan, report.check_runs)["status"], "failed"
        )
        with self.assertRaises(ValueError):
            enforce_verification(plan, report)
        payload["check_runs"].append(
            {
                **failed,
                "run_id": str(uuid4()),
                "result": "passed",
                "ended_at": 30,
            }
        )
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        self.assertEqual(
            assess_verification(plan, report.check_runs)["status"], "passed"
        )

    def test_explicit_scope_excludes_historical_only_role_without_erasing_it(self):
        plan, payload = self.plan_report()
        current = payload["check_runs"][0]
        payload["check_runs"].insert(
            0,
            {
                **current,
                "run_id": str(uuid4()),
                "role": "reviewer",
                "result": "failed",
                "ended_at": 10,
                "scope": {"kind": "content", "digest": "b" * 64},
            },
        )
        plan = VerificationPlan.model_validate(
            {
                **plan.model_dump(),
                "checks": [{**plan.checks[0].model_dump(), "scope": current["scope"]}],
            }
        )
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        self.assertEqual(
            assess_verification(plan, report.check_runs)["status"], "passed"
        )
        self.assertEqual(assess_checks(report.check_runs)["status"], "failed")

    def test_history_capacity_is_bounded_at_two_hundred(self):
        plan, payload = self.plan_report()
        current = payload["check_runs"][0]
        payload["check_runs"] = [
            {**current, "run_id": str(uuid4()), "result": "failed", "ended_at": 10}
            for _ in range(199)
        ] + [current]
        report = decode_outcome(payload)
        enforce_verification(plan, report)
        self.assertEqual(assess_checks(report.check_runs)["run_count"], 200)
        payload["check_runs"].insert(0, {**current, "run_id": str(uuid4())})
        with self.assertRaises(ValueError):
            decode_outcome(payload)


if __name__ == "__main__":
    unittest.main()
