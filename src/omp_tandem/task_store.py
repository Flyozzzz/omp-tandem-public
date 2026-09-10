"""Scoped task database, conversation leases, recovery, and lifecycle commits."""

import fcntl
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

from .channel import ChannelDelivery
from .runtime_models import ACTIVE_SQL
from .task_contracts import owned_paths, work_policy
from .workspace import ProjectScope


def _connect(path):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def initialize_database(scope: ProjectScope) -> Path:
    """Validate identity before any auxiliary store can modify this database."""
    path = scope.directory / "tasks.sqlite3"
    if path.is_symlink():
        raise ValueError("Project database must not alias another workspace")
    with closing(_connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        identity = (
            db.execute(
                "SELECT scope_id, project_root FROM bridge_scope WHERE singleton=1"
            ).fetchone()
            if "bridge_scope" in tables
            else None
        )
        expected = (scope.key, str(scope.root))
        if identity is not None and tuple(identity) != expected:
            raise ValueError("Database belongs to a different launch project")
        if identity is None:
            public_tables = tables & {
                "tasks",
                "questions",
                "artifacts",
                "project_contexts",
                "channel_events",
                "context_imports",
                "reviews",
                "review_contents",
                "review_runs",
                "review_authors",
                "findings",
                "finding_history",
                "finding_reports",
                "result_receipts",
                "diagnostic_probes",
            }
            if any(
                db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in public_tables
            ):
                raise ValueError(
                    "Unscoped data must be migrated, not attached to a project directly"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS bridge_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "scope_id TEXT NOT NULL, project_root TEXT NOT NULL)"
            )
            db.execute("INSERT INTO bridge_scope VALUES (1, ?, ?)", expected)
        db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL,
            cwd TEXT NOT NULL, mode TEXT NOT NULL, model TEXT NOT NULL,
            prompt TEXT NOT NULL, session_file TEXT,
            status TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
            error TEXT, activity TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        for name, definition in {
            "contract_json": "TEXT",
            "report_json": "TEXT",
            "deadline": "REAL",
            "result_artifacts": "TEXT NOT NULL DEFAULT '[]'",
            "policy_json": "TEXT",
            "project_context_id": "TEXT",
            "previous_project_context_id": "TEXT",
            "question_timeout_seconds": "INTEGER",
            "event_history_limit": "INTEGER",
            "workspace_roots": "TEXT",
            "review_id": "TEXT",
            "review_stage": "TEXT",
            "review_run_id": "TEXT",
            "execution_json": "TEXT",
            "actual_model": "TEXT",
            "actual_thinking": "TEXT",
            "started_at": "REAL",
            "ended_at": "REAL",
            "duration_seconds": "REAL",
            "accounting_json": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        db.execute(
            "CREATE INDEX IF NOT EXISTS conversation_tasks ON tasks(conversation_id, created)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS review_run_stage ON tasks(review_run_id, review_stage) "
            "WHERE review_run_id IS NOT NULL"
        )
        db.execute("DROP INDEX IF EXISTS one_active_turn")
        db.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn_v2 ON tasks(conversation_id) WHERE status IN {ACTIVE_SQL}"
        )
        db.execute("""CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            question TEXT NOT NULL, context TEXT NOT NULL, options_json TEXT NOT NULL,
            created REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
            answer TEXT, answered REAL
        )""")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_pending_question ON questions(task_id) WHERE state='pending'"
        )
        db.commit()
    path.chmod(0o600)
    return path


class TaskStore:
    def __init__(self, scope: ProjectScope, channel: ChannelDelivery):
        self.root = scope.directory
        self.path = self.root / "tasks.sqlite3"
        self.channel = channel

    def connect(self):
        return _connect(self.path)

    def session_path(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.root / "sessions"):
            raise ValueError("OMP history is outside this project's session storage")
        return path

    def update(self, task_id, **values):
        values["updated"] = time.time()
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"UPDATE tasks SET {', '.join(key + '=?' for key in values)} WHERE task_id=?",
                [*values.values(), task_id],
            )
            if values.get("status") in (
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            ):
                self._terminal_event(db, task_id, values["status"])
            db.commit()
        if values.get("status") in ("completed", "failed", "cancelled", "interrupted"):
            self.channel.signal()

    def _terminal_event(self, db, task_id, status):
        task = db.execute(
            "SELECT conversation_id, report_json, review_run_id FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        report = json.loads(task["report_json"]) if task["report_json"] else None
        self.channel.emit(
            "task_" + status,
            {
                "task_id": task_id,
                "conversation_id": task["conversation_id"],
                **(
                    {"review_run_id": task["review_run_id"]}
                    if task["review_run_id"]
                    else {}
                ),
                "status": status,
                "outcome": report["outcome"]
                if report and status == "completed"
                else None,
            },
            task_id=task_id,
            dedupe_key=f"task:{task_id}:{status}",
            connection=db,
        )

    def lock(self, conversation_id):
        conversation_id = str(UUID(conversation_id))
        handle = (self.root / f"{conversation_id}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ValueError(
                "Conversation already has an active turn; get its result or answer its question"
            ) from None
        return handle

    def recover(self):
        with closing(self.connect()) as db:
            rows = db.execute(
                f"SELECT task_id, conversation_id FROM tasks WHERE status IN {ACTIVE_SQL}"
            ).fetchall()
        for row in rows:
            try:
                handle = self.lock(row["conversation_id"])
            except ValueError:
                continue
            with handle, closing(self.connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    f"""UPDATE tasks SET status='interrupted', error=?, updated=?
                    WHERE task_id=? AND status IN {ACTIVE_SQL}""",
                    (
                        "Owning MCP server exited. Review possible partial edits before continuing.",
                        time.time(),
                        row["task_id"],
                    ),
                )
                db.execute(
                    "UPDATE questions SET state='cancelled' WHERE task_id=? AND state='pending'",
                    (row["task_id"],),
                )
                if changed.rowcount:
                    self._terminal_event(db, row["task_id"], "interrupted")
                db.commit()
            if changed.rowcount:
                self.channel.signal()

    def get(self, task_id, *, refresh=True):
        if refresh:
            self.recover()
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if refresh and row is not None and row["status"] == "waiting_input":
                # Readers must not offer an already-expired question while its
                # host-tool thread is between wakeups.
                db.execute("BEGIN IMMEDIATE")
                now = time.time()
                expired = db.execute(
                    "UPDATE questions SET state='expired' WHERE task_id=? AND state='pending' AND deadline<=?",
                    (task_id, now),
                )
                if expired.rowcount:
                    db.execute(
                        "UPDATE tasks SET status='running', activity='Clarification expired', updated=? WHERE task_id=? AND status='waiting_input' AND cancel_requested=0",
                        (now, task_id),
                    )
                row = db.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                db.commit()
        if row is None:
            raise ValueError("Unknown task_id; use tandem_list")
        return dict(row)

    def insert(self, record, owned):
        """Check overlapping work and admit the task in one transaction."""
        mode = record["mode"]
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if record.get("review_run_id"):
                self.validate_review_reservation(record, db)
            active = db.execute(
                f"SELECT cwd, mode, contract_json, policy_json FROM tasks WHERE status IN {ACTIVE_SQL}"
            ).fetchall()
            if mode == "work" and owned:
                for other in active:
                    if other["mode"] == "work":
                        overlap = owned & owned_paths(other["cwd"], work_policy(other))
                        if overlap:
                            raise ValueError(
                                f"Files already assigned to active work: {sorted(map(str, overlap))}"
                            )
            columns = ", ".join(record)
            placeholders = ", ".join("?" for _ in record)
            db.execute(
                f"INSERT INTO tasks ({columns}) VALUES ({placeholders})",
                list(record.values()),
            )
            db.commit()

    def validate_review_reservation(self, record, db=None):
        """Private dispatch capability: only the live owner may consume its reservation."""
        if db is None:
            with closing(self.connect()) as connection:
                return self.validate_review_reservation(record, connection)
        stage = record.get("review_stage")
        if stage not in ("independent", "comparison") or record.get("mode") != "think":
            raise ValueError("Review run reservations require a think review stage")
        run = db.execute(
            "SELECT * FROM review_runs WHERE run_id=?", (record["review_run_id"],)
        ).fetchone()
        if (
            run is None
            or run["owner"] != self.channel.owner
            or run["status"] != "starting"
            or run["phase"] != stage
            or run["review_id"] != record.get("review_id")
            or run[stage + "_task_id"] != record["task_id"]
            or run["deadline"] <= time.time()
        ):
            raise ValueError("Invalid, expired, or foreign review run reservation")
        if db.execute(
            "SELECT 1 FROM tasks WHERE task_id=?", (record["task_id"],)
        ).fetchone():
            raise ValueError("Review run reservation already dispatched")
        if stage == "comparison":
            first = db.execute(
                "SELECT * FROM tasks WHERE task_id=?", (run["independent_task_id"],)
            ).fetchone()
            report = (
                json.loads(first["report_json"])
                if first and first["report_json"]
                else None
            )
            if (
                first is None
                or first["review_run_id"] != run["run_id"]
                or first["review_id"] != run["review_id"]
                or first["review_stage"] != "independent"
                or first["conversation_id"] != record["conversation_id"]
                or first["status"] != "completed"
                or not report
                or report.get("outcome") != "success"
                or not run["independent_json"]
            ):
                raise ValueError(
                    "Comparison requires the saved successful independent result"
                )
        record["deadline"] = min(record["deadline"], run["deadline"])

    def finish(self, task_id, status, answer, error, artifact_ids):
        """Settle questions, task state, and its durable event atomically."""
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE questions SET state='cancelled' WHERE task_id=? AND state='pending'",
                (task_id,),
            )
            db.execute(
                "UPDATE tasks SET status=?, answer=?, error=?, activity='', result_artifacts=?, updated=? WHERE task_id=?",
                (
                    status,
                    answer,
                    error,
                    json.dumps(list(dict.fromkeys(artifact_ids))),
                    time.time(),
                    task_id,
                ),
            )
            self._terminal_event(db, task_id, status)
            db.commit()
        self.channel.signal()

    def cancel(self, task_id):
        self.get(task_id)
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"UPDATE tasks SET cancel_requested=1, status='cancelling', updated=? WHERE task_id=? AND status IN {ACTIVE_SQL}",
                (time.time(), task_id),
            )
            db.execute(
                "UPDATE questions SET state='cancelled' WHERE task_id=? AND state='pending'",
                (task_id,),
            )
            db.commit()

    def latest(self, conversation_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE conversation_id=? ORDER BY created DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def recent_ids(self, limit):
        self.recover()
        with closing(self.connect()) as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT task_id FROM tasks ORDER BY created DESC LIMIT ?", (limit,)
                )
            ]

    def wait_states(self, identifiers):
        self.recover()
        placeholders = ",".join("?" for _ in identifiers)
        with closing(self.connect()) as db:
            rows = db.execute(
                f"SELECT task_id, conversation_id, status FROM tasks WHERE task_id IN ({placeholders})",
                identifiers,
            ).fetchall()
        return {row["task_id"]: dict(row) for row in rows}
