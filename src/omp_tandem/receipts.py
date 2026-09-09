"""Durable result-application claims, not exactly-once external transactions."""

from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .events import _canonical_id

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class ReceiptStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS result_receipts (
                task_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                token TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                completed_at REAL
            )""")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _task(db, task_id):
        row = db.execute(
            "SELECT status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown task in this project")
        return row["status"]

    @staticmethod
    def _view(task_id, status, row):
        return {
            "task_id": task_id,
            "state": "completed"
            if row and row["completed_at"] is not None
            else (
                "uncertain"
                if row
                else "unclaimed"
                if status in _TERMINAL
                else "not_ready"
            ),
            "owner": row["owner"] if row else None,
            "claimed_at": row["claimed_at"] if row else None,
            "completed_at": row["completed_at"] if row else None,
            "authorized": False,
        }

    def status(self, task_id):
        task_id = _canonical_id(task_id, "task_id")
        with closing(self._connect()) as db:
            status = self._task(db, task_id)
            row = db.execute(
                "SELECT * FROM result_receipts WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._view(task_id, status, row)

    def claim(self, task_id, owner):
        task_id = _canonical_id(task_id, "task_id")
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 256:
            raise ValueError("owner must be nonblank and at most 256 characters")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            status = self._task(db, task_id)
            if status not in _TERMINAL:
                raise ValueError("Only terminal task results can be claimed")
            row = db.execute(
                "SELECT * FROM result_receipts WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is not None:
                # Even the same owner must not replay an ambiguous external effect.
                db.commit()
                return self._view(task_id, status, row)
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO result_receipts VALUES (?, ?, ?, ?, NULL)",
                (task_id, owner, token, time.time()),
            )
            row = db.execute(
                "SELECT * FROM result_receipts WHERE task_id=?", (task_id,)
            ).fetchone()
            db.commit()
            return {
                **self._view(task_id, status, row),
                "authorized": True,
                "token": token,
            }

    def complete(self, task_id, owner, token):
        task_id = _canonical_id(task_id, "task_id")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            status = self._task(db, task_id)
            row = db.execute(
                "SELECT * FROM result_receipts WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                row is None
                or owner != row["owner"]
                or not isinstance(token, str)
                or not secrets.compare_digest(token.encode(), row["token"].encode())
            ):
                raise ValueError("Receipt ownership or claim token does not match")
            if row["completed_at"] is None:
                db.execute(
                    "UPDATE result_receipts SET completed_at=? WHERE task_id=?",
                    (time.time(), task_id),
                )
                row = db.execute(
                    "SELECT * FROM result_receipts WHERE task_id=?", (task_id,)
                ).fetchone()
            db.commit()
            return self._view(task_id, status, row)
