"""Explicit, recipient-bound transfer of product snapshots and selected evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import closing
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .artifacts import ArtifactStore
from .project_context import ProjectContext, ProjectContextStore

_MAX_BYTES = 8 * 1024 * 1024
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", max_length=64)]
_UUID = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        max_length=36,
    ),
]
_SHARING = (
    "Explicitly shared product context and referenced evidence only; "
    "imported content is not approval or authority to execute tasks."
)
_DENIED = "Transfer is unavailable or invalid for this project"


class _TransferModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Source(_TransferModel):
    context_id: _UUID
    project_id: str
    revision: int = Field(ge=1)
    sha256: _Digest


class _Evidence(_TransferModel):
    artifact_id: _UUID
    name: str
    media_type: str
    sha256: _Digest
    content: str


class _Payload(_TransferModel):
    format_version: int
    sender_key: _Digest
    recipient_key: _Digest
    source: _Source
    context: ProjectContext
    artifacts: list[_Evidence] = Field(max_length=1632)


class _Bundle(_TransferModel):
    payload: _Payload
    sha256: _Digest


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _references(context: ProjectContext) -> set[str]:
    references = set()
    for ids in [context.artifact_ids] + [
        decision.evidence_artifact_ids for decision in context.decisions
    ]:
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate evidence reference")
        references.update(ids)
    return references


class ContextTransfer:
    def __init__(self, scope, projects: ProjectContextStore, artifacts: ArtifactStore):
        self.scope = scope
        self.projects = projects
        self.artifacts = artifacts
        if (
            projects.db_path.resolve() != artifacts.db_path.resolve()
            or projects.db_path.resolve().parent != scope.directory.resolve()
        ):
            raise ValueError("Transfer stores must belong to the launch project")
        with closing(projects._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS context_imports (
                transfer_id TEXT PRIMARY KEY,
                context_id TEXT NOT NULL UNIQUE,
                result TEXT NOT NULL
            )""")

    def _directory(self, *, create: bool = False) -> int:
        directory = self.scope.base / "transfers"
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if create:
            try:
                os.fchmod(descriptor, 0o700)
            except BaseException:
                os.close(descriptor)
                raise
        return descriptor

    def export(self, context_id: str, target_project_root: str | Path) -> dict:
        recipient = Path(target_project_root).expanduser().resolve(strict=True)
        if not recipient.is_dir():
            raise ValueError("Recipient project root must be an existing directory")
        recipient_key = hashlib.sha256(str(recipient).encode("utf-8")).hexdigest()
        if recipient_key == self.scope.key:
            raise ValueError("Context sharing requires a different recipient project")
        snapshot = self.projects.get(context_id)
        context = ProjectContext.model_validate(snapshot["context"])
        evidence = []
        size = 0
        with closing(self.artifacts._connect()) as db:
            for artifact_id in sorted(_references(context)):
                row = db.execute(
                    "SELECT artifact_id, name, media_type, sha256, content FROM artifacts "
                    "WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Unknown evidence artifact: {artifact_id}")
                artifact = dict(row)
                metadata = self.artifacts.validate_content(
                    artifact["name"], artifact["content"], artifact["media_type"]
                )
                if metadata["sha256"] != artifact["sha256"]:
                    raise ValueError("Evidence artifact checksum mismatch")
                size += len(_canonical(artifact))
                if size > _MAX_BYTES:
                    raise ValueError("Context transfer exceeds the 8 MiB UTF-8 limit")
                evidence.append(artifact)
        payload = {
            "format_version": 1,
            "sender_key": self.scope.key,
            "recipient_key": recipient_key,
            "source": {
                key: snapshot[key]
                for key in ("context_id", "project_id", "revision", "sha256")
            },
            "context": context.model_dump(mode="json"),
            "artifacts": evidence,
        }
        if (
            hashlib.sha256(_canonical(payload["context"])).hexdigest()
            != snapshot["sha256"]
        ):
            raise ValueError("Source context checksum mismatch")
        bundle = _canonical(
            {
                "payload": payload,
                "sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
            }
        )
        if len(bundle) > _MAX_BYTES:
            raise ValueError("Context transfer exceeds the 8 MiB UTF-8 limit")
        transfer_id = secrets.token_hex(32)
        directory = self._directory(create=True)
        filename = transfer_id + ".json"
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(bundle)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                os.unlink(filename, dir_fd=directory)
                raise
        finally:
            os.close(directory)
        return {
            "transfer_id": transfer_id,
            "source": {"project_key": self.scope.key, **payload["source"]},
            "recipient_key": recipient_key,
            "artifact_count": len(evidence),
            "sharing": _SHARING,
        }

    def _load(self, transfer_id: str) -> _Payload:
        try:
            directory = self._directory()
            try:
                descriptor = os.open(
                    transfer_id + ".json",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            finally:
                os.close(directory)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                    raise ValueError(_DENIED)
                encoded = stream.read(_MAX_BYTES + 1)
            if len(encoded) > _MAX_BYTES:
                raise ValueError(_DENIED)
            raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
            bundle = _Bundle.model_validate(raw, strict=True)
            payload = bundle.payload
            if (
                payload.format_version != 1
                or payload.recipient_key != self.scope.key
                or payload.sender_key == self.scope.key
                or payload.source.project_id != payload.context.project_id
            ):
                raise ValueError(_DENIED)
            if len(_canonical(raw)) > _MAX_BYTES or not hmac.compare_digest(
                bundle.sha256, hashlib.sha256(_canonical(raw["payload"])).hexdigest()
            ):
                raise ValueError(_DENIED)
            if not hmac.compare_digest(
                payload.source.sha256,
                hashlib.sha256(
                    _canonical(payload.context.model_dump(mode="json"))
                ).hexdigest(),
            ):
                raise ValueError(_DENIED)
            artifact_ids = {artifact.artifact_id for artifact in payload.artifacts}
            if len(artifact_ids) != len(
                payload.artifacts
            ) or artifact_ids != _references(payload.context):
                raise ValueError(_DENIED)
            for artifact in payload.artifacts:
                metadata = self.artifacts.validate_content(
                    artifact.name, artifact.content, artifact.media_type
                )
                if not hmac.compare_digest(metadata["sha256"], artifact.sha256):
                    raise ValueError(_DENIED)
            return payload
        except (OSError, ValueError, TypeError, RecursionError):
            raise ValueError(_DENIED) from None

    def import_context(
        self,
        transfer_id: str,
        expected_revision: int | None = None,
        publisher: str = "coordinator",
    ) -> dict:
        if not isinstance(transfer_id, str) or _TOKEN.fullmatch(transfer_id) is None:
            raise ValueError(_DENIED)
        if not isinstance(publisher, str):
            raise TypeError("publisher must be text")
        with closing(self.projects._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute(
                "SELECT result FROM context_imports WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchone()
            if receipt is not None:
                return json.loads(receipt["result"])
            payload = self._load(transfer_id)
            context_id = str(uuid4())
            remapping = {
                artifact.artifact_id: str(uuid4()) for artifact in payload.artifacts
            }
            context = payload.context.model_dump(mode="json")
            context["artifact_ids"] = [
                remapping[value] for value in context["artifact_ids"]
            ]
            for decision in context["decisions"]:
                decision["evidence_artifact_ids"] = [
                    remapping[value] for value in decision["evidence_artifact_ids"]
                ]
            provenance = (
                f"{publisher} [explicit import {transfer_id}; source project {payload.sender_key}; "
                f"context {payload.source.context_id} revision {payload.source.revision}; not approval]"
            )
            metadata = self.projects.publish_in_transaction(
                db, context, expected_revision, provenance, context_id=context_id
            )
            for artifact in payload.artifacts:
                self.artifacts.publish_in_transaction(
                    db,
                    artifact.name,
                    artifact.content,
                    artifact.media_type,
                    context_id=context_id,
                    artifact_id=remapping[artifact.artifact_id],
                )
            result = {
                **metadata,
                "context": context,
                "transfer_id": transfer_id,
                "source": {
                    "project_key": payload.sender_key,
                    **payload.source.model_dump(),
                },
                "sharing": _SHARING,
            }
            db.execute(
                "INSERT INTO context_imports (transfer_id, context_id, result) VALUES (?, ?, ?)",
                (transfer_id, context_id, _canonical(result).decode("utf-8")),
            )
        return result
