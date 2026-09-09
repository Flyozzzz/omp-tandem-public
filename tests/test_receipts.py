"""Application claims survive races and ambiguous coordinator crashes."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from omp_tandem.receipts import ReceiptStore


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tasks.sqlite3"
        self.task_id = str(uuid4())
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
            )
            db.execute("INSERT INTO tasks VALUES (?, 'completed')", (self.task_id,))
        self.store = ReceiptStore(self.path)

    def test_concurrent_claims_authorize_only_one_application(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(
                pool.map(
                    lambda _: self.store.claim(self.task_id, "coordinator"), range(8)
                )
            )
        authorized = [claim for claim in claims if claim["authorized"]]
        self.assertEqual(len(authorized), 1)
        for claim in claims:
            if not claim["authorized"]:
                self.assertNotIn("token", claim)
        token = authorized[0]["token"]
        self.assertNotIn(token, str(self.store.status(self.task_id)))
        self.assertEqual(
            self.store.complete(self.task_id, "coordinator", token)["state"],
            "completed",
        )
        self.assertFalse(self.store.claim(self.task_id, "coordinator")["authorized"])

    def test_crash_after_claim_remains_uncertain_without_reauthorizing_owner(self):
        claim = self.store.claim(self.task_id, "original")
        restarted = ReceiptStore(self.path)
        self.assertEqual(restarted.status(self.task_id)["state"], "uncertain")
        for owner in ("original", "replacement"):
            duplicate = restarted.claim(self.task_id, owner)
            self.assertFalse(duplicate["authorized"])
            self.assertNotIn("token", duplicate)
        with self.assertRaises(ValueError):
            restarted.complete(self.task_id, "replacement", claim["token"])
        with self.assertRaises(ValueError):
            restarted.complete(self.task_id, "original", "invented")
        completed = restarted.complete(self.task_id, "original", claim["token"])
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(
            restarted.complete(self.task_id, "original", claim["token"]), completed
        )
        self.assertFalse(completed["authorized"])

    def test_active_and_foreign_tasks_cannot_be_claimed(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE tasks SET status='running'")
        self.assertEqual(self.store.status(self.task_id)["state"], "not_ready")
        with self.assertRaises(ValueError):
            self.store.claim(self.task_id, "coordinator")
        with self.assertRaises(ValueError):
            self.store.claim(str(uuid4()), "coordinator")
        with self.assertRaises(ValueError):
            self.store.status(str(uuid4()))


if __name__ == "__main__":
    unittest.main()
