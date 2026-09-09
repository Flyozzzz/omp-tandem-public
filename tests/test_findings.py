"""Consumer-visible finding lifecycle, isolation, and transaction regressions."""

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.findings import FindingChange, FindingConflict, FindingStore


def draft(**values):
    return {
        "title": "Retry duplicates payment",
        "description": "A retried request charges the same order twice.",
        "location": {"path": "src/payments.py", "start_line": 12, "end_line": 16},
        "reproduction_conditions": [
            "Repeat a timed-out payment request with the same order ID."
        ],
        "evidence": ["The reproduction created two charges for order 42."],
        "reason": "The payment contract requires idempotency.",
        **values,
    }


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.conversation = str(uuid4())
        self.other_conversation = str(uuid4())
        self.old_review, self.review, self.new_review = [str(uuid4()) for _ in range(3)]
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE reviews (review_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, created REAL NOT NULL, manifest TEXT NOT NULL);
                CREATE TABLE tasks (task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, review_id TEXT, status TEXT NOT NULL, report_json TEXT);
            """)
            manifest = json.dumps({"files": [{"path": "src/payments.py"}]})
            db.executemany(
                "INSERT INTO reviews VALUES (?, 'project', ?, ?)",
                [
                    (review, created, manifest)
                    for review, created in (
                        (self.old_review, 1),
                        (self.review, 2),
                        (self.new_review, 3),
                    )
                ],
            )
        self.task = self.add_task()
        self.other_task = self.add_task(conversation=self.other_conversation)
        self.store = FindingStore(self.path)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def add_task(self, review=None, conversation=None, status="completed"):
        identifier = str(uuid4())
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?)",
                (
                    identifier,
                    conversation or self.conversation,
                    review or self.review,
                    status,
                    json.dumps({"outcome": "success"}),
                ),
            )
        return identifier

    def create(self, **values):
        return self.store.create(
            self.conversation, self.review, draft(**values), task_id=self.task
        )

    def change(self, record, action, review=None, **values):
        return self.store.update(
            record["finding_id"],
            {
                "action": action,
                "review_id": review or self.review,
                "reason": "Reproduction result was reviewed.",
                "evidence": ["Order 42 was checked against the payment ledger."],
                **values,
            },
            record["revision"],
        )

    def test_uncaptured_location_and_blocked_verification_are_not_certified(self):
        with self.assertRaises(ValueError):
            self.create(location={"path": "outside.py"})
        self.assertEqual(self.store.list(), [])
        finding = self.change(self.create(validity="confirmed"), "claim_fixed")
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE tasks SET report_json=? WHERE task_id=?",
                (json.dumps({"outcome": "blocked"}), self.task),
            )
        with self.assertRaises(ValueError):
            self.change(finding, "verify_fixed", verification_task_id=self.task)
        self.assertEqual(
            self.store.get(finding["finding_id"])["resolution"], "claimed_fixed"
        )

    def test_confirmed_fix_and_reopen_preserve_verified_version(self):
        initial = self.create()
        confirmed = self.change(initial, "confirm")
        claimed = self.change(confirmed, "claim_fixed", review=self.new_review)
        self.assertEqual(claimed["validity"], "confirmed")
        self.assertEqual(claimed["resolution"], "claimed_fixed")
        self.assertEqual(claimed["verified_versions"], [])
        verification = self.add_task(review=self.new_review)
        fixed = self.change(
            claimed,
            "verify_fixed",
            review=self.new_review,
            verification_task_id=verification,
        )
        self.assertEqual(
            (fixed["validity"], fixed["resolution"]), ("confirmed", "verified_fixed")
        )
        self.assertTrue(fixed["verified_for_review"])
        self.assertEqual(
            fixed["verified_versions"][0]["verification_task_id"], verification
        )
        reopened = self.change(fixed, "reopen", review=self.new_review)
        self.assertEqual(
            (reopened["validity"], reopened["resolution"]), ("confirmed", "open")
        )
        self.assertFalse(reopened["verified_for_review"])
        self.assertEqual(reopened["verified_versions"], fixed["verified_versions"])
        self.assertEqual(reopened["history"][: claimed["revision"]], claimed["history"])
        self.assertEqual(reopened["location"]["review_id"], self.review)
        self.assertEqual(
            self.store.list(review_id=self.review)[0]["finding_id"],
            initial["finding_id"],
        )
        self.assertEqual(
            self.store.list(review_id=self.new_review)[0]["finding_id"],
            initial["finding_id"],
        )
        self.assertEqual(
            self.store.for_task(verification)[0]["finding_id"], initial["finding_id"]
        )

    def test_later_snapshot_note_does_not_extend_verification(self):
        claimed = self.change(self.create(validity="confirmed"), "claim_fixed")
        fixed = self.change(claimed, "verify_fixed", verification_task_id=self.task)
        later = self.change(fixed, "note", review=self.new_review)
        self.assertEqual(later["verified_review_id"], self.review)
        self.assertFalse(later["verified_for_review"])
        self.assertEqual(later["verified_versions"], fixed["verified_versions"])

    def test_verification_requires_completed_same_conversation_snapshot_task(self):
        claimed = self.change(
            self.create(validity="confirmed"), "claim_fixed", review=self.new_review
        )
        invalid_tasks = [
            self.add_task(review=self.new_review, status="running"),
            self.add_task(review=self.new_review, status="failed"),
            self.add_task(review=self.new_review, conversation=self.other_conversation),
            self.task,
            str(uuid4()),
        ]
        for task_id in invalid_tasks:
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                self.change(
                    claimed,
                    "verify_fixed",
                    review=self.new_review,
                    verification_task_id=task_id,
                )
        with self.assertRaises(ValueError):
            self.change(claimed, "verify_fixed", verification_task_id=self.task)
        self.assertEqual(self.store.get(claimed["finding_id"]), claimed)

    def test_invalid_transitions_and_empty_evidence_leave_history_untouched(self):
        initial = self.create()
        for action in ("claim_fixed", "reopen", "verify_fixed"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.change(
                    initial,
                    action,
                    **(
                        {"verification_task_id": self.task}
                        if action == "verify_fixed"
                        else {}
                    ),
                )
        rejected = self.change(initial, "reject")
        with self.assertRaises(ValueError):
            self.change(rejected, "confirm")
        reopened = self.change(rejected, "reopen")
        self.assertEqual(reopened["validity"], "hypothesis")
        self.assertEqual(
            [item["action"] for item in reopened["history"]],
            ["create", "reject", "reopen"],
        )
        for values in (
            {"reason": " "},
            {"evidence": []},
            {"verification_task_id": self.task},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.change(reopened, "confirm", **values)
        self.assertEqual(self.store.history(initial["finding_id"]), reopened["history"])

    def test_foreign_ids_cannot_create_or_mutate_findings(self):
        initial = self.create()
        for conversation, review, task in (
            (str(uuid4()), self.review, None),
            (self.conversation, str(uuid4()), None),
            (self.conversation, self.review, str(uuid4())),
            (self.conversation, self.review, self.other_task),
            (self.conversation, self.new_review, self.task),
        ):
            with (
                self.subTest(conversation=conversation, review=review, task=task),
                self.assertRaises(ValueError),
            ):
                self.store.create(conversation, review, draft(), task_id=task)
        with self.assertRaises(ValueError):
            self.store.update(
                initial["finding_id"],
                {
                    "action": "confirm",
                    "review_id": self.review,
                    "reason": "Confirmed",
                    "evidence": ["Reproduced"],
                },
                1,
                task_id=self.other_task,
            )
        self.assertEqual(
            [item["finding_id"] for item in self.store.list(self.conversation)],
            [initial["finding_id"]],
        )
        self.assertEqual(self.store.get(initial["finding_id"]), initial)
        self.assertEqual(self.store.list(self.other_conversation), [])

    def test_concurrent_creates_allocate_stable_conversation_numbers(self):
        barrier = threading.Barrier(4)

        def create_one(_):
            barrier.wait()
            return self.create()

        with ThreadPoolExecutor(max_workers=4) as executor:
            records = list(executor.map(create_one, range(4)))
        self.assertEqual(sorted(record["number"] for record in records), [1, 2, 3, 4])
        for record in records:
            self.assertEqual(
                self.store.get_by_number(self.conversation, record["number"])[
                    "finding_id"
                ],
                record["finding_id"],
            )
        other = self.store.create(
            self.other_conversation, self.review, draft(), task_id=self.other_task
        )
        self.assertEqual(other["number"], 1)
        self.assertNotEqual(
            other["finding_id"],
            self.store.get_by_number(self.conversation, 1)["finding_id"],
        )

    def test_optimistic_updates_prevent_lost_revisions(self):
        initial = self.create()
        barrier = threading.Barrier(2)

        def change_one(action):
            barrier.wait()
            try:
                return self.change(initial, action)
            except FindingConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change_one, ["confirm", "reject"]))
        winner = next(result for result in results if result is not None)
        self.assertEqual(results.count(None), 1)
        self.assertEqual(self.store.get(initial["finding_id"]), winner)
        self.assertEqual([item["revision"] for item in winner["history"]], [1, 2])

    def test_report_batch_is_atomic_idempotent_and_owned_by_caller_transaction(self):
        initial = self.create()
        valid_update = {
            "finding_id": initial["finding_id"],
            "expected_revision": 1,
            "change": {
                "action": "confirm",
                "review_id": self.review,
                "reason": "Reproduced",
                "evidence": ["Duplicate ledger entries"],
            },
        }
        invalid_update = {**valid_update, "finding_id": str(uuid4())}
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                self.store.ingest_report(
                    db, self.task, [draft()], [valid_update, invalid_update]
                )
            db.commit()
        self.assertEqual(
            [item["finding_id"] for item in self.store.list(self.conversation)],
            [initial["finding_id"]],
        )
        self.assertEqual(self.store.get(initial["finding_id"]), initial)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self.store.ingest_report(
                db, self.task, [draft()], [valid_update], report_id="report-digest"
            )
            self.assertEqual(
                self.store.ingest_report(
                    db, self.task, [draft()], [valid_update], report_id="report-digest"
                ),
                result,
            )
            db.rollback()
        self.assertEqual(
            [item["finding_id"] for item in self.store.list(self.conversation)],
            [initial["finding_id"]],
        )
        self.assertEqual(self.store.get(initial["finding_id"]), initial)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self.store.ingest_report(
                db, self.task, [draft()], [valid_update], report_id="report-digest"
            )
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(
                self.store.ingest_report(
                    db, self.task, [draft()], [valid_update], report_id="report-digest"
                ),
                result,
            )
            with self.assertRaises(FindingConflict):
                self.store.ingest_report(
                    db,
                    self.task,
                    [draft(title="Changed finding")],
                    [valid_update],
                    report_id="report-digest",
                )
            db.commit()
        self.assertEqual(
            [item["number"] for item in self.store.list(self.conversation)], [1, 2]
        )
        self.assertEqual(self.store.get(initial["finding_id"])["revision"], 2)

    def test_report_cannot_update_another_conversations_finding(self):
        other = self.store.create(
            self.other_conversation, self.review, draft(), task_id=self.other_task
        )
        update = {
            "finding_id": other["finding_id"],
            "expected_revision": 1,
            "change": {
                "action": "confirm",
                "review_id": self.review,
                "reason": "Reproduced",
                "evidence": ["Duplicate ledger entries"],
            },
        }
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                self.store.ingest_report(db, self.task, [draft()], [update])
            db.commit()
        self.assertEqual(self.store.list(self.conversation), [])
        self.assertEqual(self.store.get(other["finding_id"]), other)

    def test_prior_history_cannot_be_rewritten_or_deleted(self):
        initial = self.create()
        with self.assertRaises(ValidationError):
            FindingChange.model_validate(
                {
                    "action": "note",
                    "review_id": self.review,
                    "reason": "Rewrite",
                    "history": [],
                }
            )
        with closing(self.connect()) as db:
            for statement in (
                "UPDATE finding_history SET reason='Rewritten'",
                "DELETE FROM finding_history",
            ):
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(sqlite3.IntegrityError),
                ):
                    db.execute(statement)
        self.assertEqual(self.store.history(initial["finding_id"]), initial["history"])

    def test_legacy_report_without_findings_does_not_require_snapshot(self):
        with closing(self.connect()) as db:
            db.execute("UPDATE tasks SET review_id=NULL WHERE task_id=?", (self.task,))
            db.execute("BEGIN IMMEDIATE")
            result = self.store.ingest_report(db, self.task)
            db.commit()
        self.assertEqual(result, {"created": [], "updated": []})
        self.assertEqual(self.store.list(self.conversation), [])

    def test_lookup_pages_keep_full_history_reachable_without_bulk_payloads(self):
        record = self.create()
        for number in range(3):
            record = self.change(
                record, "note", reason=f"Additional reproduction {number}"
            )
        first = self.store.get(record["finding_id"], history_limit=2)
        second = self.store.get_by_number(
            self.conversation, record["number"], history_offset=2, history_limit=2
        )
        self.assertEqual(first["history"] + second["history"], record["history"])
        self.assertEqual(second["history_total"], 4)
        additional = self.create()
        summaries = self.store.list(self.conversation, limit=1, offset=1)
        self.assertEqual(
            [item["finding_id"] for item in summaries], [additional["finding_id"]]
        )
        self.assertNotIn("history", summaries[0])
        self.assertNotIn("evidence", summaries[0])
        self.assertNotIn("history", self.store.for_task(self.task, limit=1)[0])
