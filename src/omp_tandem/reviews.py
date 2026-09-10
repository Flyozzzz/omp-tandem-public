"""Project-owned immutable review bytes; live observations are explicitly separate."""

from __future__ import annotations

import base64
import difflib
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .artifacts import ArtifactStore, _canonical_id
from .workspace import ProjectScope

_MAX_FILE = 4 * 1024 * 1024
_MAX_TOTAL = 16 * 1024 * 1024
_MAX_FILES = 256
_GIT_OUTPUT = 8 * 1024 * 1024

_PUBLICATION_LOCKS = threading.local()


class PublicationBusy(RuntimeError):
    """A controller operation must yield to an atomic snapshot publication."""


@contextmanager
def publication_lock(scope: ProjectScope, *, blocking=True):
    """Serialize publication with controller writes, without waiting in its guard."""
    path = scope.directory / "review-publication.lock"
    held = getattr(_PUBLICATION_LOCKS, "held", None)
    if held is None:
        held = _PUBLICATION_LOCKS.held = {}
    if path in held:
        yield
        return
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise PublicationBusy(
                "Snapshot publication is busy; retry shortly"
            ) from None
        held[path] = handle
        try:
            yield
        finally:
            del held[path]


class ReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    output: str = Field(max_length=1024 * 1024)
    command: str | None = Field(default=None, max_length=4000)
    source: str = Field(default="coordinator supplied", max_length=1000)
    code_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: str = Field(min_length=1, max_length=200000)
    criteria: list[str] = Field(default_factory=list, max_length=100)
    base: str = Field(default="HEAD", min_length=1, max_length=200)
    source: Literal["worktree", "staged"] = Field(
        default="worktree",
        description="Select worktree bytes or Git index bytes compared with base. Staged capture never reads live files.",
    )
    paths: list[str] | None = Field(default=None, min_length=1, max_length=_MAX_FILES)
    context_paths: list[str] = Field(
        default_factory=list,
        max_length=_MAX_FILES,
        description="Explicit additional saved context; staged reviews use index bytes, never live files.",
    )
    checks: list[ReviewCheck] = Field(default_factory=list, max_length=32)
    author_proposal: str = Field(default="", max_length=200000)
    author_rationale: str = Field(default="", max_length=200000)
    external_boundaries: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("criteria", "external_boundaries")
    @classmethod
    def bounded_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 10000 for value in values):
            raise ValueError("Items must be nonblank and at most 10000 characters")
        return values

    @field_validator("requirements")
    @classmethod
    def nonblank_requirements(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("requirements must be nonblank")
        return value


@dataclass(frozen=True)
class CaptureReservation:
    run_id: str
    owner: str
    review_id: str

    def __post_init__(self):
        for name in ("run_id", "owner", "review_id"):
            value = getattr(self, name)
            if _canonical_id(value, name) != value:
                raise ValueError(f"{name} must be a canonical UUID string")


class _Mutation(ValueError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Review paths must be nonempty relative file paths")
    parts = value.split("/")
    if (
        any(part.casefold() in ("", ".", "..", ".git") for part in parts)
        or "\\" in value
    ):
        raise ValueError("Unsafe review path")
    if PurePosixPath(value).is_absolute() or any(ord(char) < 32 for char in value):
        raise ValueError("Unsafe review path")
    value.encode("utf-8")
    return value


def _signature(value: os.stat_result) -> tuple:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _descriptor(data: bytes | None, mode: str | None) -> dict:
    return {
        "exists": data is not None,
        "sha256": _sha(data) if data is not None else None,
        "bytes": len(data) if data is not None else 0,
        "mode": mode,
    }


class ReviewStore:
    def __init__(self, db_path: Path, scope: ProjectScope, artifacts: ArtifactStore):
        self.db_path = db_path
        self.scope = scope
        # ArtifactStore requires task/context ownership. Reviews intentionally own bytes
        # separately rather than pretending a review is a product context.
        self.artifacts = artifacts
        with publication_lock(scope), closing(self._connect()) as db, db:
            # Readers must remain available even while large snapshot inserts spill
            # SQLite's page cache. All snapshot tables and the run mapping still
            # commit in the same transaction.
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL,
                created REAL NOT NULL, manifest TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_contents (
                review_id TEXT NOT NULL, section TEXT NOT NULL, path TEXT NOT NULL,
                content BLOB NOT NULL, PRIMARY KEY(review_id, section, path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_authors (
                review_id TEXT PRIMARY KEY, content TEXT NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _git(
        self, *args: str, allow_failure: bool = False, maximum: int = _GIT_OUTPUT
    ) -> bytes | None:
        # Temporary output avoids allocating unbounded subprocess output in memory.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
            GIT_LITERAL_PATHSPECS="1",
            GIT_NO_REPLACE_OBJECTS="1",
            LC_ALL="C",
        )
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "--no-pager",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "diff.external=",
                        "-c",
                        "core.attributesFile=/dev/null",
                        *args,
                    ],
                    cwd=self.scope.root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                    timeout=15,
                    check=False,
                )
            except FileNotFoundError:
                if allow_failure:
                    return None
                raise ValueError("Git is unavailable") from None
            except subprocess.TimeoutExpired:
                raise ValueError("Review Git operation exceeded 15 seconds") from None
            if result.returncode:
                if allow_failure:
                    return None
                errors.seek(0)
                raise ValueError(
                    "Review Git operation failed: "
                    + errors.read(2000).decode("utf-8", "replace")
                )
            if output.tell() > maximum:
                raise ValueError(
                    "Review Git output exceeds capture limit; select fewer files"
                )
            output.seek(0)
            return output.read()

    def _working(self, path: str) -> tuple[bytes | None, str | None, tuple | None]:
        """Open every component without following links, including concurrent swaps."""
        descriptors = []
        try:
            parent = os.open(
                self.scope.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            descriptors.append(parent)
            parts = _path(path).split("/")
            for part in parts[:-1]:
                parent = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                descriptors.append(parent)
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            descriptors.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"Only regular review files are supported: {path}")
            if before.st_size > _MAX_FILE:
                raise ValueError(f"Review file exceeds 4 MiB: {path}")
            with os.fdopen(os.dup(fd), "rb") as stream:
                content = stream.read(_MAX_FILE + 1)
            after = os.fstat(fd)
            if len(content) > _MAX_FILE:
                raise ValueError(f"Review file exceeds 4 MiB: {path}")
            if _signature(before) != _signature(after):
                raise _Mutation(f"File changed while capturing: {path}")
            mode = "100755" if before.st_mode & 0o111 else "100644"
            return content, mode, _signature(after)
        except FileNotFoundError:
            return None, None, None
        except OSError as exc:
            raise ValueError(
                f"Unsafe or unreadable review path {path}: {exc.strerror}"
            ) from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _identity(self, base: str) -> dict:
        root = self._git("rev-parse", "--show-toplevel", allow_failure=True)
        if root is None:
            return {"kind": "files", "head": None, "base_commit": None, "prefix": ""}
        prefix = self._git("rev-parse", "--show-prefix").decode("utf-8").strip("\n")
        head = self._git("rev-parse", "--verify", "HEAD^{commit}", allow_failure=True)
        resolved = self._git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            base + "^{commit}",
            allow_failure=True,
        )
        if resolved is None and not (head is None and base == "HEAD"):
            raise ValueError("Review base must resolve to an existing Git commit")
        return {
            "kind": "git",
            "head": head.decode().strip() if head else None,
            "base_commit": resolved.decode().strip() if resolved else None,
            "prefix": prefix,
        }

    def _index(self) -> dict:
        entries = {}
        for row in self._git("ls-files", "--stage", "-z").split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            mode, oid, stage = meta.decode("ascii").split()
            path = name.decode("utf-8")
            entries.setdefault(path, []).append((mode, oid, stage))
        return entries

    def _tree(self, identity: dict) -> dict:
        if identity["base_commit"] is None:
            return {}
        prefix = identity["prefix"]
        rows = self._git(
            "ls-tree",
            "--full-tree",
            "-r",
            "-z",
            identity["base_commit"],
            "--",
            prefix or ".",
        )
        entries = {}
        for row in rows.split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            path = name.decode("utf-8")
            if prefix and not path.startswith(prefix):
                continue
            entries[path[len(prefix) :]] = (mode, oid, kind)
        return entries

    def _selection(
        self, request: ReviewRequest, identity: dict, index: dict, tree: dict
    ) -> list[str]:
        if request.source == "staged" and identity["kind"] != "git":
            raise ValueError("Staged reviews require a Git repository")
        if request.paths is not None:
            return sorted({_path(path) for path in request.paths})
        if identity["kind"] != "git":
            raise ValueError("Non-Git reviews require explicit file paths")
        # Compare object IDs and modes without invoking diff or content filters.
        paths = {
            path
            for path in index.keys() | tree.keys()
            if [(mode, oid) for mode, oid, _ in index.get(path, [])]
            != ([(tree[path][0], tree[path][1])] if path in tree else [])
            or any(stage != "0" for _, _, stage in index.get(path, []))
        }
        if request.source == "worktree":
            for args in (
                ("--modified", "--deleted"),
                ("--others", "--exclude-standard"),
            ):
                paths.update(
                    path.decode("utf-8")
                    for path in self._git("ls-files", *args, "-z").split(b"\0")
                    if path
                )
        selected = sorted({_path(path) for path in paths})
        if len(selected) > _MAX_FILES:
            raise ValueError("Review selects more than 256 files; specify paths")
        return selected

    def _blob(self, entry) -> tuple[bytes | None, str | None]:
        if entry is None:
            return None, None
        mode, oid, kind = entry
        if mode == "160000" or kind not in ("blob", "0"):
            raise ValueError(
                "Selected submodules or unmerged index entries require a separate review"
            )
        size = int(self._git("cat-file", "-s", oid))
        if size > _MAX_FILE:
            raise ValueError("Review Git blob exceeds 4 MiB")
        return self._git("cat-file", "blob", oid, maximum=_MAX_FILE), mode

    def _capture(
        self, request: ReviewRequest
    ) -> tuple[dict, list[tuple[str, str, bytes]]]:
        identity = self._identity(request.base)
        index = self._index() if identity["kind"] == "git" else {}
        tree = self._tree(identity) if identity["kind"] == "git" else {}
        selected_paths = self._selection(request, identity, index, tree)
        context_paths = {_path(path) for path in request.context_paths}
        paths = sorted(set(selected_paths) | context_paths)
        if len(paths) > _MAX_FILES:
            raise ValueError("Review selects more than 256 files including context")
        files, contents, observed = [], [], {}
        total = 0
        diff = []
        for path in paths:
            if request.source == "worktree":
                selected, mode, signature = self._working(path)
                observed[path] = (selected, mode, signature)
            base, base_mode = self._blob(tree.get(path))
            stages = index.get(path, [])
            if len(stages) > 1 or (stages and stages[0][2] != "0"):
                raise ValueError(f"Unmerged index entry cannot be certified: {path}")
            staged, staged_mode = self._blob(stages[0] if stages else None)
            if request.source == "staged":
                selected, mode = staged, staged_mode
            if path in context_paths and selected is None:
                raise ValueError(
                    f"Required context is missing from the {request.source} source: {path}; "
                    "provide an explicit expanded capture before review"
                )
            if selected is None and base is None and staged is None:
                raise ValueError(
                    f"Selected file does not exist in the {request.source} source or base: {path}"
                )
            row = {
                "path": path,
                "selected": _descriptor(selected, mode),
                "base": _descriptor(base, base_mode),
                "staged": _descriptor(staged, staged_mode),
            }
            row["change"] = (
                "added"
                if base is None
                else "deleted"
                if selected is None
                else "unchanged"
                if (selected, mode) == (base, base_mode)
                else "modified"
            )
            if (
                identity["kind"] == "git"
                and path not in selected_paths
                and row["change"] != "unchanged"
            ):
                raise ValueError(
                    f"Context path has changes: {path}; include it in paths to review those changes"
                )
            row["role"] = (
                "change"
                if path in selected_paths and row["change"] != "unchanged"
                else "context"
            )
            files.append(row)
            for section, data in (
                ("selected", selected),
                ("base", base),
                ("staged", staged),
            ):
                if data is not None:
                    total += len(data)
                    if total > _MAX_TOTAL:
                        raise ValueError("Review content exceeds 16 MiB")
                    contents.append((section, path, data))
            if row["role"] == "change":
                diff.append(
                    f"File: {path}\nModes: {base_mode or 'absent'} -> {mode or 'absent'}\n"
                )
                try:
                    old, new = (
                        (base or b"").decode("utf-8"),
                        (selected or b"").decode("utf-8"),
                    )
                    if "\0" in old or "\0" in new:
                        raise UnicodeError
                    for line in difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile=f"base/{path}",
                        tofile=f"selected/{path}",
                    ):
                        diff.append(
                            line
                            if line.endswith("\n")
                            else line + "\n\\ No newline at end of file\n"
                        )
                    diff.append("\n")
                except UnicodeError:
                    diff.append(
                        f"Binary content; base sha256={row['base']['sha256']}; selected sha256={row['selected']['sha256']}\n"
                    )
        # Both complete scans must agree, including identity and selection. No live
        # reads are needed later to reconstruct any section of this bundle.
        current_index = self._index() if identity["kind"] == "git" else {}
        if (
            self._identity(request.base) != identity
            or self._selection(request, identity, current_index, tree) != selected_paths
        ):
            raise _Mutation("Git identity or selected paths changed during capture")
        if any(current_index.get(path) != index.get(path) for path in paths):
            raise _Mutation("Selected Git index changed during capture")
        if request.source == "worktree":
            for path in paths:
                if self._working(path) != observed[path]:
                    raise _Mutation(f"Selected file changed during capture: {path}")
        fingerprint = _sha(
            _json({"files": files, "git": identity, "source": request.source}).encode()
        )
        checks = []
        for check in request.checks:
            item = check.model_dump()
            item.update(
                verified=False,
                provenance="supplied_not_executed",
                version_association=(
                    "matches_supplied_fingerprint"
                    if check.code_fingerprint == fingerprint
                    else "different_fingerprint"
                    if check.code_fingerprint
                    else "unknown"
                ),
            )
            checks.append(item)
        manifest = {
            "schema_version": 1,
            "source": request.source,
            "requirements": request.requirements,
            "criteria": request.criteria,
            "selection": "explicit_paths"
            if request.paths is not None
            else "staged_changes"
            if request.source == "staged"
            else "current_changes",
            "requested_base": request.base,
            "git": identity,
            "files": files,
            "context_paths": sorted(context_paths),
            "change_count": sum(row["role"] == "change" for row in files),
            "context_count": sum(row["role"] == "context" for row in files),
            "code_fingerprint": fingerprint,
            "checks": [
                {key: value for key, value in item.items() if key != "output"}
                for item in checks
            ],
            "boundaries": {
                "saved": [
                    "requirements",
                    "criteria",
                    "selected/base/staged file bytes and modes",
                    "derived diff",
                    "supplied check reports",
                ],
                "observed": [
                    "Git HEAD/base identity",
                    "selected index entries",
                    "selected index entries stable across two reads"
                    if request.source == "staged"
                    else "selected files stable across two reads",
                ],
                "external": [
                    "unselected files",
                    *(
                        ["working tree and untracked files"]
                        if request.source == "staged"
                        else []
                    ),
                    "runtime/environment/dependencies",
                    "external services",
                    "test execution",
                    *request.external_boundaries,
                ],
                "whole_environment_immutable": False,
                "capture_consistency": "bounded double-read detection; not a filesystem transaction",
                "renames": "represented as deletion/addition paths; no similarity inference",
            },
        }
        for section, data in (
            ("requirements", request.requirements),
            ("criteria", _json(request.criteria)),
            ("diff", "".join(diff)),
            ("checks", _json(checks)),
        ):
            encoded = data.encode("utf-8")
            total += len(encoded)
            if total > _MAX_TOTAL:
                raise ValueError("Review content exceeds 16 MiB")
            contents.append((section, "", encoded))
        return manifest, contents

    def _check_reservation(
        self,
        db: sqlite3.Connection,
        reservation: CaptureReservation,
        request: dict,
        *,
        published: bool = False,
    ) -> None:
        identity = db.execute(
            "SELECT scope_id, project_root FROM bridge_scope WHERE singleton=1"
        ).fetchone()
        if identity is None or tuple(identity) != (
            self.scope.key,
            str(self.scope.root),
        ):
            raise ValueError("Capture database belongs to a different launch project")
        row = db.execute(
            "SELECT owner, capture_id, capture_started, phase, status, deadline, "
            "review_id, payload_json FROM review_runs WHERE run_id=?",
            (reservation.run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner"] != reservation.owner
            or row["capture_id"] != reservation.review_id
            or row["capture_started"] != 1
            or row["phase"] != "capture"
            or row["status"] not in ("starting", "running", "waiting_input")
            or not row["deadline"] > time.time()
            or row["review_id"] != (reservation.review_id if published else None)
            or json.loads(row["payload_json"])["request"] != request
        ):
            raise ValueError("Capture reservation is no longer publishable")
        if (
            not published
            and db.execute(
                "SELECT 1 FROM reviews WHERE review_id=?", (reservation.review_id,)
            ).fetchone()
        ):
            raise ValueError("Capture reservation has already been published")
        # An exclusive lifetime flock is held by the controller. Never create a
        # missing lease: an absent or acquirable lock proves no live owner.
        path = self.scope.directory / f"review-owner-{reservation.owner}.lock"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise ValueError("Capture owner lease was lost") from None
        with os.fdopen(descriptor, "rb") as lease:
            if not stat.S_ISREG(os.fstat(lease.fileno()).st_mode):
                raise ValueError("Capture owner lease is not a regular file")
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            raise ValueError("Capture owner lease was lost")

    def create(
        self, request: ReviewRequest, *, reservation: CaptureReservation | None = None
    ) -> dict:
        if not isinstance(request, ReviewRequest):
            request = ReviewRequest.model_validate(request)
        if len(request.model_dump_json().encode("utf-8")) > _MAX_TOTAL:
            raise ValueError("Review request exceeds 16 MiB")
        normalized = None
        if reservation is not None:
            if not isinstance(reservation, CaptureReservation):
                raise TypeError("reservation must be a CaptureReservation")
            normalized = request.model_dump()
            for key in ("paths", "context_paths"):
                if normalized[key] is not None:
                    normalized[key] = sorted(set(normalized[key]))
            with closing(self._connect()) as db:
                self._check_reservation(db, reservation, normalized)
        for attempt in range(3):
            try:
                manifest, contents = self._capture(request)
                break
            except _Mutation:
                if attempt == 2:
                    raise ValueError(
                        "Project changed during all three capture attempts; retry when selected files are stable"
                    ) from None
        review_id = reservation.review_id if reservation else str(uuid4())
        created = time.time()
        manifest.update(review_id=review_id, created=created, scope_id=self.scope.key)
        with publication_lock(self.scope), closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if reservation is not None:
                self._check_reservation(db, reservation, normalized)
            db.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?)",
                (review_id, self.scope.key, created, _json(manifest)),
            )
            db.executemany(
                "INSERT INTO review_contents VALUES (?, ?, ?, ?)",
                [
                    (review_id, section, path, content)
                    for section, path, content in contents
                ],
            )
            db.execute(
                "INSERT INTO review_authors VALUES (?, ?)",
                (
                    review_id,
                    _json(
                        {
                            "proposal": request.author_proposal,
                            "rationale": request.author_rationale,
                        }
                    ),
                ),
            )
            if reservation is not None:
                db.execute(
                    "UPDATE review_runs SET review_id=?, updated=? WHERE run_id=?",
                    (review_id, time.time(), reservation.run_id),
                )
                # Recheck after inserting potentially large contents, immediately
                # before commit. Any lost lease/deadline rolls back both tables.
                self._check_reservation(db, reservation, normalized, published=True)
        return self._summary(manifest)

    def _manifest(self, review_id: str) -> dict:
        review_id = _canonical_id(review_id, "review_id")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT manifest FROM reviews WHERE review_id=? AND scope_id=?",
                (review_id, self.scope.key),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown review: {review_id}")
        return json.loads(row["manifest"])

    @staticmethod
    def _summary(manifest: dict) -> dict:
        return {
            "review_id": manifest["review_id"],
            "created": manifest["created"],
            "scope_id": manifest["scope_id"],
            "source": manifest.get("source", "worktree"),
            "code_fingerprint": manifest["code_fingerprint"],
            "summary": f"Immutable {manifest.get('source', 'worktree')} review bundle of {len(manifest['files'])} selected paths",
            "file_count": len(manifest["files"]),
            "change_count": manifest.get(
                "change_count",
                sum(row["change"] != "unchanged" for row in manifest["files"]),
            ),
            "context_count": manifest.get(
                "context_count",
                sum(row["change"] == "unchanged" for row in manifest["files"]),
            ),
            "check_count": len(manifest["checks"]),
            "selection": manifest["selection"],
            "git": manifest["git"],
            "whole_environment_immutable": False,
            "reader_sections": [
                "manifest",
                "requirements",
                "criteria",
                "diff",
                "selected",
                "base",
                "staged",
                "checks",
            ],
        }

    def info(self, review_id: str) -> dict:
        return self._summary(self._manifest(review_id))

    def read(
        self,
        review_id: str,
        section: str = "manifest",
        path: str | None = None,
        offset: int = 0,
        limit: int = 16000,
        reveal_author: bool = False,
    ) -> dict:
        manifest = self._manifest(review_id)
        review_id = manifest["review_id"]
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50000
        ):
            raise ValueError(
                "Review paging requires offset >= 0 and limit from 1 to 50000"
            )
        if section == "author" and not reveal_author:
            raise ValueError("Author material is available only in comparison stage")
        if section in ("selected", "base", "staged"):
            path = _path(path)
            file = next((row for row in manifest["files"] if row["path"] == path), None)
            if file is None:
                raise ValueError("Path is not part of this review")
            if not file[section]["exists"]:
                return {
                    "review_id": review_id,
                    "source": manifest.get("source", "worktree"),
                    "section": section,
                    "path": path,
                    "exists": False,
                    "content": None,
                    "next_offset": None,
                }
        elif path is not None:
            raise ValueError("Only selected/base/staged sections accept a path")
        if section == "manifest":
            data = _json(manifest).encode()
        elif section == "author":
            with closing(self._connect()) as db:
                data = (
                    db.execute(
                        "SELECT content FROM review_authors WHERE review_id=?",
                        (review_id,),
                    )
                    .fetchone()[0]
                    .encode()
                )
        elif section in (
            "requirements",
            "criteria",
            "diff",
            "checks",
            "selected",
            "base",
            "staged",
        ):
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT content FROM review_contents WHERE review_id=? AND section=? AND path=?",
                    (review_id, section, path or ""),
                ).fetchone()
            if row is None:
                raise ValueError("Missing review section")
            data = row[0]
        else:
            raise ValueError("Unknown review section")
        try:
            text, encoding = data.decode("utf-8"), "utf-8"
            if "\0" in text:
                raise UnicodeError
        except UnicodeError:
            text, encoding = base64.b64encode(data).decode("ascii"), "base64"
        content = text[offset : offset + limit]
        end = offset + len(content)
        return {
            "review_id": review_id,
            "source": manifest.get("source", "worktree"),
            "section": section,
            "path": path,
            "exists": True,
            "encoding": encoding,
            "sha256": _sha(data),
            "bytes": len(data),
            "characters": len(text),
            "offset": offset,
            "content": content,
            "next_offset": end if end < len(text) else None,
        }

    def assess(self, review_id: str) -> dict:
        manifest = self._manifest(review_id)
        source = manifest.get("source", "worktree")
        changed, unknown, index_changed = [], [], []
        identity, index = None, None
        try:
            identity = self._identity(manifest["requested_base"])
            if identity["kind"] != manifest["git"]["kind"]:
                unknown.append("Project Git availability changed")
            else:
                index = self._index() if identity["kind"] == "git" else {}
        except (ValueError, OSError) as exc:
            unknown.append(str(exc))
        for row in manifest["files"]:
            path = row["path"]
            try:
                if source == "worktree":
                    data, mode, _ = self._working(path)
                    if _descriptor(data, mode) != row["selected"]:
                        changed.append(path)
                if index is not None:
                    stages = index.get(path, [])
                    if len(stages) > 1 or (stages and stages[0][2] != "0"):
                        index_changed.append(path)
                        if source == "staged":
                            changed.append(path)
                    else:
                        staged, staged_mode = self._blob(stages[0] if stages else None)
                        if (
                            source == "staged"
                            and _descriptor(staged, staged_mode) != row["selected"]
                        ):
                            changed.append(path)
                        if _descriptor(staged, staged_mode) != row["staged"]:
                            index_changed.append(path)
            except ValueError as exc:
                unknown.append(f"{path}: {exc}")
        previous = identity is not None and identity["head"] != manifest["git"]["head"]
        base_changed = (
            identity is not None
            and identity["base_commit"] != manifest["git"]["base_commit"]
        )
        status = (
            "stale"
            if changed or index_changed
            else "unknown"
            if unknown
            else "previous_version"
            if previous or base_changed
            else "current_selected_state"
        )
        return {
            "review_id": manifest["review_id"],
            "source": source,
            "assessed_at": time.time(),
            "status": status,
            "changed_paths": changed,
            "changed_index_paths": index_changed,
            "unknown": unknown,
            "previous_version": previous,
            "base_reference_changed": base_changed,
            "snapshot_head": manifest["git"]["head"],
            "observed_head": identity["head"] if identity else None,
            "whole_environment_immutable": False,
            "scope": (
                "selected index paths and captured Git identity only; working tree, "
                "unselected staged paths and external boundaries remain outside this review"
                if source == "staged"
                else "selected files and captured Git identity only; external boundaries remain unknown"
            ),
        }
