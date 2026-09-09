"""Snapshot-bound review findings with append-only, optimistic lifecycle history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

__all__ = [
    "FindingChange",
    "FindingConflict",
    "FindingDraft",
    "FindingLocation",
    "FindingStore",
    "FindingUpdate",
]

_Text = Annotated[str, StringConstraints(pattern=r"\S", max_length=8000)]


def _identifier(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("IDs must be canonical lowercase hyphenated UUIDs")
    return value


_Id = Annotated[str, AfterValidator(_identifier)]


def _relative_path(value: str) -> str:
    if (
        not value.strip()
        or PureWindowsPath(value).anchor
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.replace("\\", "/").split("/"))
    ):
        raise ValueError("Finding location must be a relative snapshot file path")
    return value


class _FindingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class FindingLocation(_FindingModel):
    path: Annotated[
        str, StringConstraints(max_length=2000), AfterValidator(_relative_path)
    ]
    start_line: int | None = Field(default=None, ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if self.end_line is not None and (
            self.start_line is None or self.end_line < self.start_line
        ):
            raise ValueError("end_line requires start_line and must not precede it")
        return self


class FindingDraft(_FindingModel):
    title: Annotated[str, StringConstraints(pattern=r"\S", max_length=500)]
    description: _Text
    location: FindingLocation
    reproduction_conditions: list[_Text] = Field(min_length=1, max_length=30)
    evidence: list[_Text] = Field(min_length=1, max_length=30)
    reason: _Text
    validity: Literal["hypothesis", "confirmed"] = "hypothesis"


class FindingChange(_FindingModel):
    action: Literal[
        "note", "confirm", "reject", "reopen", "claim_fixed", "verify_fixed"
    ]
    review_id: _Id
    reason: _Text
    evidence: list[_Text] = Field(default_factory=list, max_length=30)
    verification_task_id: _Id | None = None

    @model_validator(mode="after")
    def verification_evidence(self) -> Self:
        if (
            self.action in ("confirm", "reject", "claim_fixed", "verify_fixed")
            and not self.evidence
        ):
            raise ValueError(f"{self.action} requires evidence")
        if self.action == "verify_fixed":
            if self.verification_task_id is None:
                raise ValueError("verify_fixed requires a completed verification task")
        elif self.verification_task_id is not None:
            raise ValueError("verification_task_id is only valid for verify_fixed")
        return self


class FindingUpdate(_FindingModel):
    finding_id: _Id
    expected_revision: int = Field(ge=1, strict=True)
    change: FindingChange


class FindingConflict(ValueError):
    """The caller must reread a finding before attempting another mutation."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FindingStore:
    """Use only after project database identity validation and task initialization."""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    origin_review_id TEXT NOT NULL,
                    review_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    validity TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL,
                    UNIQUE(conversation_id, number)
                );
                CREATE TABLE IF NOT EXISTS finding_history (
                    finding_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    task_id TEXT,
                    review_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    verification_task_id TEXT,
                    validity TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    created REAL NOT NULL,
                    PRIMARY KEY(finding_id, revision)
                );
                CREATE INDEX IF NOT EXISTS finding_history_task ON finding_history(task_id);
                CREATE INDEX IF NOT EXISTS finding_history_verification ON finding_history(verification_task_id);
                CREATE TRIGGER IF NOT EXISTS finding_history_no_update
                BEFORE UPDATE ON finding_history BEGIN
                    SELECT RAISE(ABORT, 'Finding history is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS finding_history_no_delete
                BEFORE DELETE ON finding_history BEGIN
                    SELECT RAISE(ABORT, 'Finding history is append-only');
                END;
                CREATE TABLE IF NOT EXISTS finding_reports (
                    task_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _actor(actor):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 500:
            raise ValueError("actor must be nonempty and at most 500 characters")

    @staticmethod
    def _review(db, review_id):
        _identifier(review_id)
        row = db.execute(
            "SELECT created FROM reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown review snapshot in this project")
        return row["created"]

    @staticmethod
    def _conversation(db, conversation_id):
        _identifier(conversation_id)
        if (
            db.execute(
                "SELECT 1 FROM tasks WHERE conversation_id=? LIMIT 1",
                (conversation_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("Unknown conversation in this project")

    @staticmethod
    def _task(db, task_id, conversation_id=None, review_id=None):
        _identifier(task_id)
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task in this project")
        if conversation_id is not None and row["conversation_id"] != conversation_id:
            raise ValueError("Task belongs to another conversation")
        if review_id is not None and row["review_id"] != review_id:
            raise ValueError("Task is not bound to the specified review snapshot")
        return row

    @staticmethod
    def _row(db, finding_id):
        _identifier(finding_id)
        row = db.execute(
            "SELECT * FROM findings WHERE finding_id=?", (finding_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown finding in this project")
        return row

    @staticmethod
    def _page(offset, limit):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be an integer between 1 and 200")

    @staticmethod
    def _history(db, finding_id, offset=0, limit=50):
        result = []
        for row in db.execute(
            "SELECT * FROM finding_history WHERE finding_id=? ORDER BY revision LIMIT ? OFFSET ?",
            (finding_id, limit, offset),
        ):
            entry = dict(row)
            entry["evidence"] = json.loads(entry.pop("evidence_json"))
            result.append(entry)
        return result

    def _get(self, db, finding_id, history_offset=0, history_limit=50, summary=False):
        result = dict(self._row(db, finding_id))
        draft = json.loads(result.pop("draft_json"))
        # The location always describes the original immutable snapshot, not current code.
        fields = (
            ("title", "location")
            if summary
            else (
                "title",
                "description",
                "location",
                "reproduction_conditions",
                "evidence",
            )
        )
        result.update({key: draft[key] for key in fields})
        result["location"]["review_id"] = result["origin_review_id"]
        if not summary:
            result["history"] = self._history(
                db, finding_id, history_offset, history_limit
            )
            result["history_offset"] = history_offset
            result["history_total"] = result["revision"]
            # Historical verification facts on this page, never a live-code guarantee.
            result["verified_versions"] = [
                entry
                for entry in result["history"]
                if entry["action"] == "verify_fixed"
            ]
        last_verified = db.execute(
            "SELECT review_id FROM finding_history WHERE finding_id=? AND action='verify_fixed' ORDER BY revision DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
        result["verified_review_id"] = (
            last_verified["review_id"] if last_verified else None
        )
        result["verified_for_review"] = (
            result["resolution"] == "verified_fixed"
            and result["verified_review_id"] == result["review_id"]
        )
        result["verification_scope"] = "snapshot_only"
        return result

    def get(self, finding_id, history_offset=0, history_limit=50):
        self._page(history_offset, history_limit)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return self._get(db, finding_id, history_offset, history_limit)

    def get_by_number(
        self, conversation_id, number, history_offset=0, history_limit=50
    ):
        self._page(history_offset, history_limit)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("Finding number must be a positive integer")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._conversation(db, conversation_id)
            row = db.execute(
                "SELECT finding_id FROM findings WHERE conversation_id=? AND number=?",
                (conversation_id, number),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown finding number in this conversation")
            return self._get(db, row["finding_id"], history_offset, history_limit)

    def history(self, finding_id, offset=0, limit=50):
        self._page(offset, limit)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._row(db, finding_id)
            return self._history(db, finding_id, offset, limit)

    def list(self, conversation_id=None, review_id=None, limit=50, offset=0):
        self._page(offset, limit)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            filters, values = [], []
            if conversation_id is not None:
                self._conversation(db, conversation_id)
                filters.append("f.conversation_id=?")
                values.append(conversation_id)
            if review_id is not None:
                self._review(db, review_id)
                filters.append(
                    "EXISTS (SELECT 1 FROM finding_history h WHERE h.finding_id=f.finding_id AND h.review_id=?)"
                )
                values.append(review_id)
            clause = " WHERE " + " AND ".join(filters) if filters else ""
            rows = db.execute(
                "SELECT f.finding_id FROM findings f"
                + clause
                + " ORDER BY f.conversation_id, f.number LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            return [self._get(db, row["finding_id"], summary=True) for row in rows]

    def for_task(self, task_id, limit=50, offset=0):
        self._page(offset, limit)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            self._task(db, task_id)
            rows = db.execute(
                "SELECT DISTINCT f.finding_id, f.number FROM findings f JOIN finding_history h USING(finding_id) WHERE h.task_id=? OR h.verification_task_id=? ORDER BY f.number LIMIT ? OFFSET ?",
                (task_id, task_id, limit, offset),
            ).fetchall()
            return [self._get(db, row["finding_id"], summary=True) for row in rows]

    @staticmethod
    def _append(
        db,
        finding_id,
        revision,
        action,
        actor,
        task_id,
        review_id,
        reason,
        evidence,
        verification_task_id,
        validity,
        resolution,
        now,
    ):
        db.execute(
            "INSERT INTO finding_history "
            "(finding_id,revision,action,actor,task_id,review_id,reason,evidence_json,"
            "verification_task_id,validity,resolution,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                finding_id,
                revision,
                action,
                actor,
                task_id,
                review_id,
                reason,
                _json(evidence),
                verification_task_id,
                validity,
                resolution,
                now,
            ),
        )

    def _create(self, db, conversation_id, review_id, finding, task_id, actor):
        finding = FindingDraft.model_validate(finding)
        self._actor(actor)
        self._conversation(db, conversation_id)
        self._review(db, review_id)
        manifest = json.loads(
            db.execute(
                "SELECT manifest FROM reviews WHERE review_id=?", (review_id,)
            ).fetchone()["manifest"]
        )
        if finding.location.path not in {item["path"] for item in manifest["files"]}:
            raise ValueError("Finding location is outside the captured review material")
        if task_id is not None:
            self._task(db, task_id, conversation_id, review_id)
        number = db.execute(
            "SELECT COALESCE(MAX(number),0)+1 FROM findings WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        identifier, now = str(uuid4()), time.time()
        db.execute(
            "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                conversation_id,
                number,
                review_id,
                review_id,
                1,
                finding.validity,
                "open",
                finding.model_dump_json(),
                now,
                now,
            ),
        )
        self._append(
            db,
            identifier,
            1,
            "create",
            actor,
            task_id,
            review_id,
            finding.reason,
            finding.evidence,
            None,
            finding.validity,
            "open",
            now,
        )
        return self._get(db, identifier)

    def create(
        self, conversation_id, review_id, finding, task_id=None, actor="coordinator"
    ):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._create(
                db, conversation_id, review_id, finding, task_id, actor
            )
            db.commit()
            return result

    def _update(self, db, finding_id, change, expected_revision, task_id, actor):
        change = FindingChange.model_validate(change)
        self._actor(actor)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        current = self._row(db, finding_id)
        if current["revision"] != expected_revision:
            raise FindingConflict(
                f"Finding revision is {current['revision']}, not {expected_revision}"
            )
        created = self._review(db, change.review_id)
        if created < self._review(db, current["review_id"]):
            raise ValueError(
                "A finding update requires its current or a newer snapshot"
            )
        if task_id is not None:
            self._task(db, task_id, current["conversation_id"], change.review_id)
        validity, resolution = current["validity"], current["resolution"]
        action = change.action
        if action == "confirm":
            if validity != "hypothesis":
                raise ValueError(
                    "Only a hypothesis can be confirmed; reopen rejected findings first"
                )
            validity = "confirmed"
        elif action == "reject":
            if validity == "rejected":
                raise ValueError("Finding is already rejected")
            validity, resolution = "rejected", "open"
        elif action == "reopen":
            if validity != "rejected" and resolution == "open":
                raise ValueError("Finding is already open")
            if validity == "rejected":
                validity = "hypothesis"
            resolution = "open"
        elif action == "claim_fixed":
            if validity != "confirmed" or resolution != "open":
                raise ValueError("Only an open confirmed finding can be claimed fixed")
            resolution = "claimed_fixed"
        elif action == "verify_fixed":
            if validity != "confirmed" or resolution != "claimed_fixed":
                raise ValueError(
                    "Verification requires a confirmed finding with a claimed fix"
                )
            verification = self._task(
                db,
                change.verification_task_id,
                current["conversation_id"],
                change.review_id,
            )
            if verification["status"] != "completed":
                raise ValueError("Verification evidence requires a completed task")
            report = (
                json.loads(verification["report_json"])
                if verification["report_json"]
                else None
            )
            if report is None or report.get("outcome") != "success":
                raise ValueError(
                    "Verification requires a successful structured outcome, not merely a completed turn"
                )
            resolution = "verified_fixed"
        revision, now = expected_revision + 1, time.time()
        db.execute(
            "UPDATE findings SET review_id=?, revision=?, validity=?, resolution=?, updated=? WHERE finding_id=?",
            (change.review_id, revision, validity, resolution, now, finding_id),
        )
        self._append(
            db,
            finding_id,
            revision,
            action,
            actor,
            task_id,
            change.review_id,
            change.reason,
            change.evidence,
            change.verification_task_id,
            validity,
            resolution,
            now,
        )
        return self._get(db, finding_id)

    def update(
        self, finding_id, change, expected_revision, task_id=None, actor="coordinator"
    ):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._update(
                db, finding_id, change, expected_revision, task_id, actor
            )
            db.commit()
            return result

    def ingest_report(
        self, db, task_id, findings=(), finding_updates=(), report_id=None
    ):
        """Ingest once per task inside the caller's transaction; never commit it.

        A savepoint also undoes this entire batch if the caller catches an error.
        report_id may be the digest of the full report; absent it, the canonical
        finding payload is the report identity. Changed resubmissions are rejected.
        """
        if not db.in_transaction:
            raise ValueError("Finding ingestion requires an active report transaction")
        drafts = [FindingDraft.model_validate(value) for value in findings]
        updates = [FindingUpdate.model_validate(value) for value in finding_updates]
        payload_hash = hashlib.sha256(
            _json(
                {
                    "findings": [item.model_dump() for item in drafts],
                    "finding_updates": [item.model_dump() for item in updates],
                }
            ).encode("utf-8")
        ).hexdigest()
        identity = payload_hash if report_id is None else report_id
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("report_id must be nonempty")
        savepoint = "finding_batch_" + uuid4().hex
        db.execute(f"SAVEPOINT {savepoint}")
        try:
            task = self._task(db, task_id)
            previous = db.execute(
                "SELECT * FROM finding_reports WHERE task_id=?", (task_id,)
            ).fetchone()
            if previous is not None:
                if (
                    previous["report_id"] != identity
                    or previous["payload_hash"] != payload_hash
                ):
                    raise FindingConflict(
                        "Findings for this task report have already been ingested"
                    )
                result = json.loads(previous["result_json"])
            else:
                result = {"created": [], "updated": []}
                if drafts or updates:
                    if not task["review_id"]:
                        raise ValueError(
                            "Structured findings require a snapshot-bound task"
                        )
                    self._review(db, task["review_id"])
                for draft in drafts:
                    record = self._create(
                        db,
                        task["conversation_id"],
                        task["review_id"],
                        draft,
                        task_id,
                        "worker",
                    )
                    result["created"].append(record["finding_id"])
                for update in updates:
                    record = self._update(
                        db,
                        update.finding_id,
                        update.change,
                        update.expected_revision,
                        task_id,
                        "worker",
                    )
                    result["updated"].append(record["finding_id"])
                db.execute(
                    "INSERT INTO finding_reports VALUES (?,?,?,?)",
                    (task_id, identity, payload_hash, _json(result)),
                )
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except Exception:
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
