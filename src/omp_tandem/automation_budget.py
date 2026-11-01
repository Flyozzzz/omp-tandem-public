"""An operator-granted ceiling on how much new execution this project admits.

The plugin already caps a managed work grant with `max_launches`. Nothing capped
the other ways execution starts, so a new task ID, a recaptured review without a
declared `corrective` link, or an MCP restart each began again from zero. This
ledger spends one admission per newly admitted unit of execution and refuses the
next one when the operator's grant is used up.

What it is not: proof that a human agreed. The grant is created through the
operator CLI, and code running as the same OS user can call that CLI too. This
bounds the plugin's own APIs, not arbitrary processes.

Enforcement is off until an operator turns it on, and turning it on requires an
explicit finite `max_admissions`. There is no default ceiling, because the right
number depends on the work: one measured project spent 81 review runs in a week,
so any number chosen here would refuse honest work somewhere else.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .errors import ProtocolError

# Every unit of execution that costs the user money and time, named by the kind
# of owner that holds it. A nested native turn belongs to the owner that was
# already admitted and never spends again.
OWNER_KINDS = ("review_run", "task", "work_attempt", "team_step")


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


class AutomationBudget:
    """Admissions ledger for one launch project's database."""

    def __init__(self, database: Path):
        self.database = Path(database)
        with closing(_connect(self.database)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS automation_budget (
                authorization_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                max_admissions INTEGER NOT NULL,
                used_admissions INTEGER NOT NULL,
                state TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS automation_admissions (
                admission_id TEXT PRIMARY KEY,
                authorization_id TEXT NOT NULL,
                owner_kind TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                open INTEGER NOT NULL,
                created REAL NOT NULL,
                UNIQUE (owner_kind, owner_id)
            )""")

    # -- state -----------------------------------------------------------

    def _active(self, db):
        return db.execute(
            "SELECT * FROM automation_budget WHERE state='active'"
        ).fetchone()

    def status(self) -> dict:
        """Report the real state; never present a disabled ceiling as protection."""
        with closing(_connect(self.database)) as db:
            row = self._active(db)
            if row is None:
                return {
                    "enforcement": "off",
                    "authorization_id": None,
                    "max_admissions": None,
                    "used_admissions": None,
                    "remaining_admissions": None,
                    "revision": None,
                    "note": "No active grant: new execution is not limited by this ledger.",
                }
            open_admissions = db.execute(
                "SELECT count(*) FROM automation_admissions "
                "WHERE authorization_id=? AND open=1",
                (row["authorization_id"],),
            ).fetchone()[0]
            return {
                "enforcement": "on",
                "authorization_id": row["authorization_id"],
                "max_admissions": row["max_admissions"],
                "used_admissions": row["used_admissions"],
                "remaining_admissions": max(
                    0, row["max_admissions"] - row["used_admissions"]
                ),
                "open_admissions": open_admissions,
                "revision": row["revision"],
            }

    # -- operator lifecycle ----------------------------------------------

    def authorize(self, max_admissions: int) -> dict:
        """Open a grant. A previous grant must have no unfinished admissions."""
        if type(max_admissions) is not int or not 1 <= max_admissions <= 100000:
            raise ValueError("max_admissions must be an integer from 1 to 100000")
        now = time.time()
        with closing(_connect(self.database)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = self._active(db)
                if current is not None:
                    unfinished = db.execute(
                        "SELECT count(*) FROM automation_admissions "
                        "WHERE authorization_id=? AND open=1",
                        (current["authorization_id"],),
                    ).fetchone()[0]
                    if unfinished:
                        raise ValueError(
                            "The active grant still has unfinished admissions; "
                            "complete or reconcile them before replacing it"
                        )
                    db.execute(
                        "UPDATE automation_budget SET state='closed', updated=? "
                        "WHERE authorization_id=?",
                        (now, current["authorization_id"]),
                    )
                identifier = str(uuid4())
                db.execute(
                    "INSERT INTO automation_budget (authorization_id, revision, "
                    "max_admissions, used_admissions, state, created, updated) "
                    "VALUES (?,1,?,0,'active',?,?)",
                    (identifier, max_admissions, now, now),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.status()

    def extend(self, expected_revision: int, additional_admissions: int) -> dict:
        """Raise the ceiling of the current grant, keeping the spend intact."""
        if type(additional_admissions) is not int or not (
            1 <= additional_admissions <= 100000
        ):
            raise ValueError(
                "additional_admissions must be an integer from 1 to 100000"
            )
        with closing(_connect(self.database)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                current = self._active(db)
                if current is None:
                    raise ValueError("No active grant to extend; authorize one first")
                if current["revision"] != expected_revision:
                    raise ValueError(
                        f"Grant revision is {current['revision']}, not "
                        f"{expected_revision}; reread the current state"
                    )
                db.execute(
                    "UPDATE automation_budget SET max_admissions=?, revision=?, "
                    "updated=? WHERE authorization_id=?",
                    (
                        current["max_admissions"] + additional_admissions,
                        current["revision"] + 1,
                        time.time(),
                        current["authorization_id"],
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.status()

    def disable(self) -> dict:
        """Turn enforcement off explicitly; never a side effect of a missing grant."""
        with closing(_connect(self.database)) as db:
            db.execute(
                "UPDATE automation_budget SET state='closed', updated=? "
                "WHERE state='active'",
                (time.time(),),
            )
        return self.status()

    # -- admission --------------------------------------------------------

    def _spend(self, db, owner_kind: str, owner_id: str) -> dict:
        current = self._active(db)
        if current is None:
            return {"enforcement": "off", "admission_id": None, "spent": False}
        existing = db.execute(
            "SELECT * FROM automation_admissions WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        if existing is not None:
            return {
                "enforcement": "on",
                "admission_id": existing["admission_id"],
                "spent": False,
            }
        if current["max_admissions"] - current["used_admissions"] <= 0:
            raise ProtocolError(
                "automation_budget_exhausted",
                "The operator's automation grant has no admissions left: "
                f"{current['used_admissions']} of {current['max_admissions']} "
                "used. Work already admitted may finish; nothing new starts.",
                fields=("automation_budget",),
                allowed_fix=(
                    "Ask the operator to run the extend command, or to "
                    "authorize a new grant once open admissions are done. "
                    "A new task, review or work ID does not restore it."
                ),
            )
        identifier = str(uuid4())
        db.execute(
            "INSERT INTO automation_admissions (admission_id, authorization_id, "
            "owner_kind, owner_id, open, created) VALUES (?,?,?,?,1,?)",
            (
                identifier,
                current["authorization_id"],
                owner_kind,
                owner_id,
                time.time(),
            ),
        )
        db.execute(
            "UPDATE automation_budget SET used_admissions=used_admissions+1, "
            "updated=? WHERE authorization_id=?",
            (time.time(), current["authorization_id"]),
        )
        return {"enforcement": "on", "admission_id": identifier, "spent": True}

    def admit(self, owner_kind: str, owner_id: str, *, db=None) -> dict:
        """Spend one admission for a new unit of execution, or refuse.

        Admission is identified by its owner, so an exact replay of the same
        review run or attempt returns the recorded admission instead of
        spending again. With no active grant this is a no-op: enforcement is
        opt-in, and an absent grant must not silently stop ordinary work.

        Callers that already hold a write transaction on this database pass
        their own `db`, so the spend commits or rolls back with the reservation
        it belongs to. Opening a second connection there would deadlock.
        """
        if owner_kind not in OWNER_KINDS:
            raise ValueError(f"Unknown admission owner kind: {owner_kind}")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("Admission owner_id must be a nonblank string")
        if db is not None:
            return self._spend(db, owner_kind, owner_id)
        with closing(_connect(self.database)) as own:
            own.execute("BEGIN IMMEDIATE")
            try:
                result = self._spend(own, owner_kind, owner_id)
                own.execute("COMMIT")
            except BaseException:
                own.execute("ROLLBACK")
                raise
        return result

    def close(self, owner_kind: str, owner_id: str) -> None:
        """Mark an admitted unit finished. Spend is never returned."""
        with closing(_connect(self.database)) as db:
            db.execute(
                "UPDATE automation_admissions SET open=0 "
                "WHERE owner_kind=? AND owner_id=?",
                (owner_kind, owner_id),
            )
