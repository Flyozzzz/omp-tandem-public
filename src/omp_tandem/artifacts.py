"""Immutable, versioned text artifacts stored alongside conversation state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

_METADATA = "artifact_id, conversation_id, task_id, context_id, name, version, sha256, media_type, characters, created"
_MAX_BYTES = 4 * 1024 * 1024
_MEDIA_TYPES = {"text/plain", "text/markdown", "application/json"}


def _canonical_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError(f"{label} must be a UUID string") from None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


class ArtifactStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(artifacts)")
            }
            indexes = []
            if columns and "context_id" not in columns:
                indexes = [
                    row[0]
                    for row in db.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='artifacts' AND sql IS NOT NULL"
                    )
                ]
                db.execute("ALTER TABLE artifacts RENAME TO artifacts_legacy")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                task_id TEXT,
                context_id TEXT,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                media_type TEXT NOT NULL,
                characters INTEGER NOT NULL,
                created REAL NOT NULL,
                content TEXT NOT NULL,
                CHECK (
                    (conversation_id IS NOT NULL AND task_id IS NOT NULL AND context_id IS NULL)
                    OR (conversation_id IS NULL AND task_id IS NULL AND context_id IS NOT NULL)
                ),
                UNIQUE (conversation_id, name, version),
                UNIQUE (context_id, name, version)
            )""")
            if columns and "context_id" not in columns:
                old_columns = _METADATA.replace(", context_id", "")
                db.execute(
                    f"INSERT INTO artifacts ({old_columns}, content) "
                    f"SELECT {old_columns}, content FROM artifacts_legacy"
                )
                db.execute("DROP TABLE artifacts_legacy")
                for statement in indexes:
                    db.execute(statement)
            db.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_task_created ON artifacts(task_id, created)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS artifacts_context_created ON artifacts(context_id, created)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def publish(
        self,
        conversation_id: str,
        task_id: str,
        name: str,
        content: str,
        media_type: str = "text/plain",
    ) -> dict:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self.publish_in_transaction(
                db,
                name,
                content,
                media_type,
                conversation_id=conversation_id,
                task_id=task_id,
            )

    @staticmethod
    def validate_content(name: str, content: str, media_type: str) -> dict:
        """One validator for ordinary publication and portable evidence."""
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError(
                "Artifact name must be nonblank and at most 120 characters"
            )
        if not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
            raise ValueError("Unsupported artifact media_type")
        if not isinstance(content, str):
            raise TypeError("Artifact content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("Artifact content exceeds the 4 MiB UTF-8 limit")
        if media_type == "application/json":
            try:
                json.loads(content, parse_constant=_reject_json_constant)
            except (ValueError, RecursionError) as exc:
                raise ValueError(
                    f"Artifact content must be valid JSON: {exc}"
                ) from None
        return {
            "name": name,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "media_type": media_type,
            "characters": len(content),
        }

    def publish_in_transaction(
        self,
        db: sqlite3.Connection,
        name: str,
        content: str,
        media_type: str = "text/plain",
        *,
        conversation_id: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        artifact_id: str | None = None,
    ) -> dict:
        """Publish without committing an existing caller-owned transaction."""
        if not db.in_transaction:
            raise ValueError("Artifact publication requires an active transaction")
        if context_id is None:
            conversation_id = _canonical_id(conversation_id, "conversation_id")
            task_id = _canonical_id(task_id, "task_id")
            owner_column, owner_id = "conversation_id", conversation_id
        else:
            if conversation_id is not None or task_id is not None:
                raise ValueError("Artifacts require either task or context ownership")
            context_id = _canonical_id(context_id, "context_id")
            owner_column, owner_id = "context_id", context_id
        metadata = {
            **self.validate_content(name, content, media_type),
            "artifact_id": _canonical_id(artifact_id, "artifact_id")
            if artifact_id is not None
            else str(uuid4()),
            "conversation_id": conversation_id,
            "task_id": task_id,
            "context_id": context_id,
            "created": time.time(),
        }
        metadata["version"] = db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM artifacts "
            f"WHERE {owner_column} = ? AND name = ?",
            (owner_id, name),
        ).fetchone()[0]
        db.execute(
            f"INSERT INTO artifacts ({_METADATA}, content) "
            "VALUES (:artifact_id, :conversation_id, :task_id, :context_id, :name, :version, "
            ":sha256, :media_type, :characters, :created, :content)",
            {**metadata, "content": content},
        )
        return metadata

    def info(self, artifact_id: str) -> dict:
        artifact_id = _canonical_id(artifact_id, "artifact_id")
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_METADATA} FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown artifact: {artifact_id}")
        return dict(row)

    def for_task(self, task_id: str) -> list[dict]:
        """Discover already-published work even without a final task report."""
        task_id = _canonical_id(task_id, "task_id")
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT {_METADATA} FROM artifacts WHERE task_id=? ORDER BY created DESC, rowid DESC",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read(self, artifact_id: str, offset: int = 0, limit: int = 16000) -> dict:
        artifact_id = _canonical_id(artifact_id, "artifact_id")
        if type(offset) is not int or offset < 0:
            raise ValueError("Artifact offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 50000:
            raise ValueError("Artifact limit must be an integer from 1 to 50000")
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_METADATA}, content FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown artifact: {artifact_id}")
        result = dict(row)
        # Python slicing counts Unicode code points even after embedded NULs,
        # unlike SQLite's text substr()/length() functions.
        result["content"] = result["content"][offset : offset + limit]
        end = offset + len(result["content"])
        result["next_offset"] = end if end < result["characters"] else None
        return result
