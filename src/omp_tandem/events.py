"""Durable, owner-routed channel outbox independent of task state."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

_COLUMNS = "event_id, owner, kind, task_id, payload, dedupe_key, created, sent_at, acknowledged_at"
_FIELDS = tuple(_COLUMNS.split(", "))
_MAX_PAYLOAD_BYTES = 32768
_MAX_WEBHOOK_PENDING = 256


class EventConflict(ValueError):
    """A deduplication key already identifies a different event."""


class QueueFull(RuntimeError):
    """An owner's unacknowledged webhook queue has reached its limit."""


def _canonical_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError(f"{label} must be a UUID string") from None


def _key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("dedupe_key must be nonblank and at most 256 characters")
    return value


def _event(row) -> dict:
    result = dict(zip(_FIELDS, row, strict=True))
    result["payload"] = json.loads(result["payload"])
    return result


class EventStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS channel_events (
                event_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                kind TEXT NOT NULL,
                task_id TEXT,
                payload TEXT NOT NULL,
                dedupe_key TEXT,
                created REAL NOT NULL,
                sent_at REAL,
                acknowledged_at REAL,
                UNIQUE (owner, dedupe_key)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS channel_events_pending_owner
                ON channel_events(owner, created) WHERE acknowledged_at IS NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS channel_events_pending_task
                ON channel_events(task_id) WHERE acknowledged_at IS NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS channel_events_unsent_owner
                ON channel_events(owner, created)
                WHERE acknowledged_at IS NULL AND sent_at IS NULL""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    def enqueue(
        self,
        owner: str,
        kind: str,
        payload: dict,
        task_id: str | None = None,
        dedupe_key: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict:
        owner = _canonical_id(owner, "owner")
        if task_id is not None:
            task_id = _canonical_id(task_id, "task_id")
        if not isinstance(kind, str) or not kind or len(kind) > 64:
            raise ValueError("kind must be nonempty and at most 64 characters")
        if dedupe_key is not None:
            dedupe_key = _key(dedupe_key)
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            size = len(encoded.encode("utf-8"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError(f"payload must be valid JSON: {exc}") from None
        if size > _MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the 32768-byte UTF-8 limit")
        if connection is not None:
            if not connection.in_transaction:
                raise ValueError("connection must have an active caller transaction")
            return self._enqueue(connection, owner, kind, encoded, task_id, dedupe_key)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._enqueue(db, owner, kind, encoded, task_id, dedupe_key)

    def _enqueue(self, db, owner, kind, payload, task_id, dedupe_key) -> dict:
        if dedupe_key is not None:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM channel_events WHERE owner=? AND dedupe_key=?",
                (owner, dedupe_key),
            ).fetchone()
            if row is not None:
                if (row[2], row[3], row[4]) != (kind, task_id, payload):
                    raise EventConflict(
                        "dedupe_key already identifies a different event"
                    )
                return _event(row)
        if kind == "webhook":
            self._check_capacity(db, owner, 1)
        row = (
            str(uuid4()),
            owner,
            kind,
            task_id,
            payload,
            dedupe_key,
            time.time(),
            None,
            None,
        )
        db.execute(
            f"INSERT INTO channel_events ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        return _event(row)

    def _check_capacity(self, db, owner: str, incoming: int) -> None:
        if (
            incoming
            and db.execute(
                "SELECT COUNT(*) FROM channel_events "
                "WHERE owner=? AND kind='webhook' AND acknowledged_at IS NULL",
                (owner,),
            ).fetchone()[0]
            + incoming
            > _MAX_WEBHOOK_PENDING
        ):
            raise QueueFull("owner has 256 pending webhook events")

    def get(self, event_id: str) -> dict:
        event_id = _canonical_id(event_id, "event_id")
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_COLUMNS} FROM channel_events WHERE event_id=?", (event_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown event")
        return _event(row)

    def pending(
        self, owner: str | None, limit: int = 50, *, unsent_only: bool = False
    ) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("limit must be an integer from 1 to 256")
        parameters = []
        condition = "acknowledged_at IS NULL"
        if unsent_only:
            condition += " AND sent_at IS NULL"
        if owner is not None:
            parameters.append(_canonical_id(owner, "owner"))
            condition += " AND owner=?"
        parameters.append(limit)
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {_COLUMNS} FROM channel_events WHERE {condition} "
                "ORDER BY created, rowid LIMIT ?",
                parameters,
            ).fetchall()
        return [_event(row) for row in rows]

    def acknowledge(self, owner: str, event_id: str) -> bool:
        owner = _canonical_id(owner, "owner")
        event_id = _canonical_id(event_id, "event_id")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT acknowledged_at FROM channel_events WHERE event_id=? AND owner=?",
                (event_id, owner),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown event for owner")
            if row[0] is not None:
                return False
            db.execute(
                "UPDATE channel_events SET acknowledged_at=? WHERE event_id=? AND owner=?",
                (time.time(), event_id, owner),
            )
            return True

    def acknowledge_key(self, owner: str, dedupe_key: str) -> int:
        owner = _canonical_id(owner, "owner")
        dedupe_key = _key(dedupe_key)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute(
                "UPDATE channel_events SET acknowledged_at=? "
                "WHERE owner=? AND dedupe_key=? AND acknowledged_at IS NULL",
                (time.time(), owner, dedupe_key),
            ).rowcount

    def acknowledge_work(self, owner: str, work_id: str, revision: int) -> int:
        """Best-effort acknowledgement of the exact shared state already observed."""
        owner = _canonical_id(owner, "owner")
        work_id = _canonical_id(work_id, "work_id")
        with closing(self._connect()) as db:
            db.execute("PRAGMA busy_timeout=0")
            try:
                return db.execute(
                    "UPDATE channel_events SET acknowledged_at=? WHERE owner=? "
                    "AND kind='work_changed' AND acknowledged_at IS NULL "
                    "AND json_extract(payload,'$.work_id')=? "
                    "AND json_extract(payload,'$.revision')<=?",
                    (time.time(), owner, work_id, revision),
                ).rowcount
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                ):
                    raise
                # Optional delivery bookkeeping cannot block authoritative reads.
                # A later observation will acknowledge the retained event.
                return 0

    def mark_sent(self, owner: str, event_id: str) -> None:
        owner = _canonical_id(owner, "owner")
        event_id = _canonical_id(event_id, "event_id")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE channel_events SET sent_at=? WHERE event_id=? AND owner=?",
                (time.time(), event_id, owner),
            ).rowcount
            if not changed:
                raise ValueError("Unknown event for owner")

    def adopt(self, task_id: str, new_owner: str) -> int:
        task_id = _canonical_id(task_id, "task_id")
        new_owner = _canonical_id(new_owner, "new_owner")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT kind, owner FROM channel_events "
                "WHERE task_id=? AND acknowledged_at IS NULL",
                (task_id,),
            ).fetchall()
            self._check_capacity(
                db,
                new_owner,
                sum(kind == "webhook" and owner != new_owner for kind, owner in rows),
            )
            try:
                return db.execute(
                    "UPDATE channel_events SET owner=?, sent_at=NULL "
                    "WHERE task_id=? AND acknowledged_at IS NULL",
                    (new_owner, task_id),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise EventConflict(
                    "Target owner already has an event with this dedupe_key"
                ) from exc

    def adopt_event(self, event_id: str, new_owner: str) -> dict:
        event_id = _canonical_id(event_id, "event_id")
        new_owner = _canonical_id(new_owner, "new_owner")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT {_COLUMNS} FROM channel_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown event")
            if row[8] is not None:
                raise ValueError("Acknowledged events cannot be adopted")
            self._check_capacity(
                db, new_owner, int(row[2] == "webhook" and row[1] != new_owner)
            )
            try:
                db.execute(
                    "UPDATE channel_events SET owner=?, sent_at=NULL WHERE event_id=?",
                    (new_owner, event_id),
                )
            except sqlite3.IntegrityError as exc:
                raise EventConflict(
                    "Target owner already has an event with this dedupe_key"
                ) from exc
            return _event(
                db.execute(
                    f"SELECT {_COLUMNS} FROM channel_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
            )
