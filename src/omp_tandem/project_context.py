"""Validated product snapshots with immutable, optimistic-concurrency storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

__all__ = [
    "ContextConflict",
    "ProductRule",
    "ProjectContext",
    "ProjectContextStore",
    "ProjectDecision",
]

_NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]
_Identifier = Annotated[_NonBlank, StringConstraints(max_length=100)]
_NonBlank2000 = Annotated[_NonBlank, StringConstraints(max_length=2000)]
_ProjectId = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$", max_length=100)
]
_PROJECT_ID_ADAPTER = TypeAdapter(_ProjectId)
_METADATA = "context_id, project_id, revision, sha256, created, publisher"
_MAX_BYTES = 64000


def _canonical_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("context_id must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("context_id must be a UUID string") from None


def _validate_artifact_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("artifact IDs must be canonical lowercase hyphenated UUIDs")
    return value


_ArtifactId = Annotated[str, AfterValidator(_validate_artifact_id)]


class _ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class ProductRule(_ContextModel):
    id: _Identifier
    text: _NonBlank = Field(max_length=4000)
    requirement: Literal["required", "advisory"] = "required"
    applies_to: list[_NonBlank] = Field(default_factory=list, max_length=32)
    source: _NonBlank2000
    positive_examples: list[_NonBlank2000] = Field(default_factory=list, max_length=10)
    negative_examples: list[_NonBlank2000] = Field(default_factory=list, max_length=10)


class ProjectDecision(_ContextModel):
    id: _Identifier
    text: _NonBlank = Field(max_length=4000)
    status: Literal["accepted", "rejected", "deferred", "superseded"]
    source: _NonBlank2000
    evidence_artifact_ids: list[_ArtifactId] = Field(
        default_factory=list, max_length=16
    )
    supersedes: _Identifier | None = None


class ProjectContext(_ContextModel):
    project_id: _ProjectId
    product_summary: _NonBlank = Field(max_length=12000)
    components: list[_NonBlank] = Field(default_factory=list, max_length=32)
    rules: list[ProductRule] = Field(default_factory=list, max_length=50)
    decisions: list[ProjectDecision] = Field(default_factory=list, max_length=100)
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        ids = set()
        for entry in (*self.rules, *self.decisions):
            if entry.id in ids:
                raise ValueError(
                    "rule and decision IDs must be unique across the snapshot"
                )
            ids.add(entry.id)
        decisions = {decision.id: decision for decision in self.decisions}
        for decision in self.decisions:
            if decision.supersedes is not None and (
                decision.supersedes == decision.id
                or decision.supersedes not in decisions
            ):
                raise ValueError(
                    "supersedes must reference another decision in this snapshot"
                )
        visited = set()
        for decision_id in decisions:
            path = set()
            current = decision_id
            while current is not None and current not in visited:
                if current in path:
                    raise ValueError(
                        "decision supersedes references must not form a cycle"
                    )
                path.add(current)
                current = decisions[current].supersedes
            visited.update(path)
        return self


class ContextConflict(ValueError):
    """A publication's expected revision does not match the latest snapshot."""


class ProjectContextStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project_contexts (
                context_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created REAL NOT NULL,
                publisher TEXT NOT NULL,
                context TEXT NOT NULL,
                UNIQUE (project_id, revision)
            )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def publish(
        self,
        context: ProjectContext | dict,
        expected_revision: int | None = None,
        publisher: str = "coordinator",
    ) -> dict:
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self.publish_in_transaction(
                db, context, expected_revision, publisher
            )

    def publish_in_transaction(
        self,
        db: sqlite3.Connection,
        context: ProjectContext | dict,
        expected_revision: int | None = None,
        publisher: str = "coordinator",
        *,
        context_id: str | None = None,
    ) -> dict:
        """Publish without committing an existing caller-owned transaction."""
        if not db.in_transaction:
            raise ValueError("Context publication requires an active transaction")
        context = ProjectContext.model_validate(context)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ContextConflict(
                "expected_revision must be a nonnegative integer or None"
            )
        if not isinstance(publisher, str):
            raise TypeError("publisher must be text")
        content = json.dumps(
            context.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("Project context exceeds the 64000-byte UTF-8 limit")
        metadata = {
            "context_id": _canonical_id(context_id)
            if context_id is not None
            else str(uuid4()),
            "project_id": context.project_id,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "publisher": publisher,
        }
        latest = db.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM project_contexts WHERE project_id = ?",
            (context.project_id,),
        ).fetchone()[0]
        if (latest == 0 and expected_revision not in (None, 0)) or (
            latest > 0 and expected_revision != latest
        ):
            raise ContextConflict(
                f"Project {context.project_id!r} is at revision {latest}; "
                f"expected {expected_revision!r}"
            )
        metadata["revision"] = latest + 1
        metadata["created"] = time.time()
        db.execute(
            f"INSERT INTO project_contexts ({_METADATA}, context) "
            "VALUES (:context_id, :project_id, :revision, :sha256, :created, "
            ":publisher, :context)",
            {**metadata, "context": content},
        )
        return metadata

    def get(self, context_id: str) -> dict:
        context_id = _canonical_id(context_id)
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_METADATA}, context FROM project_contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown project context: {context_id}")
        result = dict(row)
        result["context"] = json.loads(result["context"])
        return result

    def info(self, context_id: str) -> dict:
        context_id = _canonical_id(context_id)
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT {_METADATA} FROM project_contexts WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown project context: {context_id}")
        return dict(row)

    def list(self, project_id: str | None = None, limit: int = 20) -> list[dict]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if project_id is not None:
            project_id = _PROJECT_ID_ADAPTER.validate_python(project_id)
        with closing(self._connect()) as db:
            if project_id is None:
                rows = db.execute(
                    f"SELECT {_METADATA} FROM project_contexts "
                    "ORDER BY created DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT {_METADATA} FROM project_contexts WHERE project_id = ? "
                    "ORDER BY revision DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
