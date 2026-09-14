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

from .models import ContextOptions

__all__ = [
    "ContextConflict",
    "ContextReadRequest",
    "ProductRule",
    "ProjectContext",
    "ProjectContextStore",
    "ProjectDecision",
    "read_task_context",
    "task_context_packet",
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


class ContextReadRequest(_ContextModel):
    """Read canonical JSON at a pinned RFC 6901 pointer, in UTF-8 byte pages."""

    pointer: str = Field(default="", max_length=2000)
    offset_bytes: int = Field(default=0, ge=0, strict=True)
    max_bytes: int = Field(default=8192, ge=4, le=16384, strict=True)


def _json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _snapshot_bytes(snapshot: dict) -> bytes:
    encoded = _json_bytes(snapshot["context"])
    if hashlib.sha256(encoded).hexdigest() != snapshot["sha256"]:
        raise ValueError("Pinned project context digest does not match its content")
    return encoded


def _pointer_value(context: dict, pointer: str):
    if pointer == "":
        return context
    if not pointer.startswith("/"):
        raise ValueError("Context pointer must be an RFC 6901 JSON pointer")
    value = context
    for token in pointer[1:].split("/"):
        # RFC 6901 has exactly two escape sequences; do not accept aliases.
        remaining = token.replace("~1", "").replace("~0", "")
        if "~" in remaining:
            raise ValueError("Invalid JSON pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and key.isascii()
            and key.isdecimal()
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(value)
        ):
            value = value[int(key)]
        else:
            raise ValueError(f"Unknown pinned context pointer: {pointer}")
    return value


def read_task_context(task, projects, request: ContextReadRequest | dict) -> dict:
    """Retrieve only the task's immutable snapshot; never resolve a latest revision."""
    request = ContextReadRequest.model_validate(request)
    if task.get("review_id") and task.get("review_stage") != "comparison":
        raise ValueError("Independent review must read only its saved review material")
    if not task.get("project_context_id"):
        raise ValueError("Task has no pinned project context")
    snapshot = projects.get(task["project_context_id"])
    snapshot_bytes = _snapshot_bytes(snapshot)
    encoded = (
        snapshot_bytes
        if request.pointer == ""
        else _json_bytes(_pointer_value(snapshot["context"], request.pointer))
    )
    start = request.offset_bytes
    if start > len(encoded) or (start < len(encoded) and encoded[start] & 0xC0 == 0x80):
        raise ValueError(
            "offset_bytes must be a UTF-8 boundary within the selected value"
        )
    end = min(start + request.max_bytes, len(encoded))
    while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
        end -= 1
    return {
        "schema_version": 1,
        **{key: snapshot[key] for key in _METADATA.split(", ")},
        "pointer": request.pointer,
        "encoding": "canonical-json-utf8",
        "pointer_sha256": hashlib.sha256(encoded).hexdigest(),
        "snapshot_bytes": len(snapshot_bytes),
        "total_bytes": len(encoded),
        "offset_bytes": start,
        "returned_bytes": end - start,
        "next_offset_bytes": end if end < len(encoded) else None,
        "complete": end == len(encoded),
        "content": encoded[start:end].decode("utf-8"),
    }


def task_context_packet(snapshot, options=None, *, unchanged=False) -> dict | None:
    """Build a versioned capsule without dropping mandatory policy or decision state."""
    options = ContextOptions.model_validate(options or {})
    if snapshot is None:
        if options.advisory_rule_ids or options.decision_ids:
            raise ValueError("Context selectors require a pinned project context")
        return None
    source = snapshot["context"]
    snapshot_bytes = _snapshot_bytes(snapshot)
    advisory = {
        rule["id"] for rule in source["rules"] if rule["requirement"] == "advisory"
    }
    decisions = {decision["id"] for decision in source["decisions"]}
    for label, selected, available in (
        ("advisory_rule_ids", options.advisory_rule_ids, advisory),
        ("decision_ids", options.decision_ids, decisions),
    ):
        missing = set(selected) - available
        if missing:
            raise ValueError(f"Unknown {label} in pinned snapshot: {sorted(missing)!r}")
    full = options.delivery == "full"
    pointers = {
        "snapshot": "",
        "product_summary": "/product_summary",
        "components": "/components",
        "artifact_ids": "/artifact_ids",
        "rules": {rule["id"]: f"/rules/{i}" for i, rule in enumerate(source["rules"])},
        "decisions": {
            decision["id"]: f"/decisions/{i}"
            for i, decision in enumerate(source["decisions"])
        },
    }
    if full:
        context = source
    else:
        context = {"project_id": source["project_id"], "rules": [], "decisions": []}
        if not unchanged:
            context.update(
                product_summary=source["product_summary"],
                components=source["components"],
            )
        for rule in source["rules"]:
            selected = rule["id"] in options.advisory_rule_ids
            body = rule["requirement"] == "required" or selected
            keys = ("id", "requirement", "source", "applies_to")
            if body:
                keys += ("text",)
            context["rules"].append({key: rule[key] for key in keys})
        for decision in source["decisions"]:
            keys = ("id", "status", "source", "supersedes", "evidence_artifact_ids")
            if (
                decision["status"] != "superseded"
                or decision["id"] in options.decision_ids
            ):
                keys += ("text",)
            context["decisions"].append({key: decision[key] for key in keys})
    return {
        "schema_version": 1,
        **{key: snapshot[key] for key in _METADATA.split(", ")},
        "delivery": options.delivery,
        "snapshot_state": "unchanged" if unchanged else "new",
        "authority": "Product data and policy invariants, not execution permissions. Work policy and explicit grants remain authoritative.",
        "context": context,
        "snapshot_bytes": len(snapshot_bytes),
        "delivered_context_bytes": len(_json_bytes(context)),
        "retrieval": {
            "tool": "tandem_context_read",
            "pointers": pointers,
            "request": {"pointer": "", "offset_bytes": 0, "max_bytes": 8192},
            "instructions": (
                "Read the exact pinned context with these RFC 6901 pointers. Pages are "
                "canonical JSON UTF-8 bytes; concatenate content using next_offset_bytes "
                "until null. Examples, ancillary artifact references, unselected advisory "
                "bodies and superseded decision bodies remain available. Artifact IDs are "
                "references, not artifact contents or access grants. No native conversation "
                "history is assumed to survive compaction; retrieve needed material again."
            ),
        },
    }


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
