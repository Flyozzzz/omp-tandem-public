"""Conservative, copy-only import of settled legacy conversations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import UUID

_SETTLED = {"completed", "failed", "cancelled", "interrupted"}
_WARNING = (
    "Legacy stores and agents are unchanged. Old sessions must finish, then restart "
    "with project isolation; deferred conversations can be imported on a later startup. "
    "Already imported snapshots are never merged with later legacy changes."
)


class _Deferred(Exception):
    pass


def _rows(db, table, where="", values=()):
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} {where}", values)]


def _task_metadata(db):
    available = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    columns = [
        name
        for name in (
            "task_id",
            "conversation_id",
            "cwd",
            "status",
            "session_file",
            "project_context_id",
            "previous_project_context_id",
        )
        if name in available
    ]
    return {
        row["task_id"]: dict(row)
        for row in db.execute(f"SELECT {', '.join(columns)} FROM tasks")
    }


def _canonical_cwd(value):
    try:
        path = Path(value).expanduser()
        return path.resolve(strict=False) if path.is_absolute() else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _safe_path(path, root, *, missing=False):
    """Reject every symlink component, including links that stay inside the root."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise _Deferred("unsafe_history_path")
    current = root
    try:
        if stat.S_ISLNK(current.lstat().st_mode):
            raise _Deferred("unsafe_history_path")
        for part in path.relative_to(root).parts:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise _Deferred("unsafe_history_path")
    except FileNotFoundError:
        if missing:
            return None
        raise _Deferred("history_changed_during_copy") from None
    return path


def _open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _Deferred("unsafe_history_path")
    return os.fdopen(fd, "rb")


def _artifact_refs(value):
    if not isinstance(value, dict):
        raise _Deferred("invalid_legacy_references")
    refs = value.get("artifact_ids", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise _Deferred("invalid_legacy_references")
    return set(refs)


def _context_refs(row):
    value = json.loads(row["context"])
    refs = _artifact_refs(value)
    for decision in value.get("decisions", []):
        evidence = decision.get("evidence_artifact_ids", [])
        if not isinstance(evidence, list) or any(
            not isinstance(ref, str) for ref in evidence
        ):
            raise _Deferred("invalid_legacy_references")
        refs.update(evidence)
    return refs


def _insert(db, table, row):
    columns = {entry[1] for entry in db.execute(f"PRAGMA table_info({table})")}
    if not row.keys() <= columns:
        raise _Deferred("destination_schema_incompatible")
    names = list(row)
    db.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
        [row[name] for name in names],
    )


def _same_or_insert(db, table, key, row):
    existing = db.execute(
        f"SELECT * FROM {table} WHERE {key}=?", (row[key],)
    ).fetchone()
    if existing is not None:
        if any(existing[name] != value for name, value in row.items()):
            raise _Deferred("destination_immutable_collision")
        return False
    _insert(db, table, row)
    return True


def _history_plan(tasks, base, allowed):
    plans, missing = {}, 0
    root = base / "sessions"
    for task in tasks:
        value = task.get("session_file")
        if not value:
            missing += 1
            continue
        path = Path(value)
        if not path.is_absolute():
            raise _Deferred("unsafe_history_path")
        path = _safe_path(path, root, missing=True)
        if path is None:
            missing += 1
            continue
        if value in plans:
            continue
        with _open_regular(path) as stream:
            remaining = 1024 * 1024
            while True:
                line = stream.readline(remaining + 1)
                if not line or len(line) > remaining:
                    raise _Deferred("invalid_history_header")
                remaining -= len(line)
                header = json.loads(line)
                if not isinstance(header, dict):
                    raise _Deferred("invalid_history_header")
                if header.get("type") == "session":
                    break
                # Native OMP puts mutable, padded title metadata before the session header.
                # Never skip message/tool records when establishing history ownership.
                if header.get("type") != "title":
                    raise _Deferred("invalid_history_header")
        if _canonical_cwd(header.get("cwd")) not in allowed:
            raise _Deferred("foreign_history_header")
        files = [(path, Path(path.name))]
        directories = []
        sidecar = path.with_suffix("")
        if _safe_path(sidecar, root, missing=True) is not None:
            if not sidecar.is_dir():
                raise _Deferred("unsafe_history_path")
            directories.append(Path(sidecar.name))
            pending = [sidecar]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        item = _safe_path(Path(entry.path), root)
                        relative = Path(sidecar.name) / item.relative_to(sidecar)
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(relative)
                            pending.append(item)
                        elif entry.is_file(follow_symlinks=False):
                            files.append((item, relative))
                        else:
                            raise _Deferred("unsafe_history_path")
        plans[value] = (files, directories)
    return plans, missing


def _copy_history(plans, staging, remaining):
    rewritten, copied = {}, 0
    for index, (original, (files, directories)) in enumerate(plans.items()):
        folder = staging / str(index)
        folder.mkdir(mode=0o700)
        for directory in directories:
            (folder / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        for source, relative in files:
            with _open_regular(source) as incoming:
                before = os.fstat(incoming.fileno())
                if remaining is not None and copied + before.st_size > remaining:
                    raise _Deferred(
                        "history_budget_exceeded_use_explicit_unlimited_migration"
                    )
                destination = folder / relative
                with destination.open("xb") as outgoing:
                    os.chmod(destination, 0o600)
                    while chunk := incoming.read(1024 * 1024):
                        copied += len(chunk)
                        if remaining is not None and copied > remaining:
                            raise _Deferred(
                                "history_budget_exceeded_use_explicit_unlimited_migration"
                            )
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                after = os.fstat(incoming.fileno())
                if (before.st_size, before.st_mtime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise _Deferred("history_changed_during_copy")
        rewritten[original] = str(folder / Path(original).name)
    # Persist directory entries before publishing their paths in SQLite.
    for directory, _, _ in os.walk(staging, topdown=False):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return rewritten, copied


def migrate_legacy(
    scope, db_path, *, legacy_cwds=None, context_ids=(), max_bytes=64 * 1024 * 1024
):
    """Import only proven project-owned snapshots; never write the legacy store.

    ``legacy_cwds`` and ``context_ids`` are trusted operator inputs, not MCP inputs.
    Exact-root matching deliberately excludes nested projects unless mapped explicitly.
    Missing native histories retain answers but clear session_file (no resume).
    """
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer or None")
    allowed = {scope.root}
    for value in legacy_cwds or ():
        cwd = _canonical_cwd(value)
        if cwd is None:
            raise ValueError("Explicit legacy cwd must be an absolute path")
        allowed.add(cwd)
    explicit_contexts = set(context_ids)
    if any(not isinstance(value, str) for value in explicit_contexts):
        raise ValueError("context_ids must contain strings")
    source_path = scope.base / "tasks.sqlite3"
    result = {
        "status": "no_legacy_store",
        "imported_conversations": 0,
        "imported_tasks": 0,
        "imported_artifacts": 0,
        "imported_contexts": 0,
        "imported_questions": 0,
        "already_imported": 0,
        "deferred_conversations": 0,
        "deferred_contexts": 0,
        "history_unavailable_tasks": 0,
        "copied_bytes": 0,
        "reasons": {},
        "warning": _WARNING,
    }
    if not source_path.exists():
        return result
    if source_path.is_symlink() or Path(db_path).resolve() == source_path.resolve():
        raise ValueError(
            "Legacy source must be separate from the destination and not a symlink"
        )
    reasons = Counter()
    with ExitStack() as stack:
        migration_lock = stack.enter_context(
            (scope.directory / ".legacy-migration.lock").open("a")
        )
        try:
            fcntl.flock(migration_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.update(status="migration_busy", reasons={"migration_in_progress": 1})
            return result
        source = stack.enter_context(
            closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True))
        )
        source.row_factory = sqlite3.Row
        target = stack.enter_context(
            closing(sqlite3.connect(db_path, timeout=10, isolation_level=None))
        )
        target.row_factory = sqlite3.Row
        target.execute("""CREATE TABLE IF NOT EXISTS legacy_migrations (
            conversation_id TEXT PRIMARY KEY, source_identity TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL, imported REAL NOT NULL DEFAULT (unixepoch())
        )""")
        info = source_path.stat()
        identity = hashlib.sha256(
            f"{source_path}:{info.st_dev}:{info.st_ino}".encode()
        ).hexdigest()
        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "tasks" not in tables:
            result.update(status="deferred", reasons={"legacy_schema_unavailable": 1})
            return result
        discovered = defaultdict(list)
        for row in _task_metadata(source).values():
            discovered[row["conversation_id"]].append(row)
        locked = set()
        for conversation, tasks in discovered.items():
            cwds = {_canonical_cwd(task["cwd"]) for task in tasks}
            if not cwds & allowed:
                continue
            reason = None
            if not cwds <= allowed:
                reason = "mixed_project_conversation"
            elif any(task["status"] not in _SETTLED for task in tasks):
                reason = "legacy_conversation_active"
            else:
                try:
                    if str(UUID(conversation)) != conversation:
                        raise ValueError
                    path = _safe_path(scope.base / f"{conversation}.lock", scope.base)
                    handle = stack.enter_context(_open_regular(path))
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked.add(conversation)
                except (OSError, ValueError, _Deferred):
                    reason = "legacy_conversation_lock_unavailable"
            if reason:
                reasons[reason] += 1
                result["deferred_conversations"] += 1
        # Establish the source snapshot only after acquiring legacy conversation locks.
        source.execute("BEGIN")
        all_tasks = _task_metadata(source)
        conversations = defaultdict(list)
        for row in all_tasks.values():
            conversations[row["conversation_id"]].append(row)
        eligible = {}
        for conversation in locked:
            tasks = conversations.get(conversation, [])
            if not tasks or any(
                _canonical_cwd(task["cwd"]) not in allowed
                or task["status"] not in _SETTLED
                for task in tasks
            ):
                reasons["legacy_conversation_changed"] += 1
                result["deferred_conversations"] += 1
            else:
                full_tasks = _rows(
                    source, "tasks", "WHERE conversation_id=?", (conversation,)
                )
                eligible[conversation] = full_tasks
                all_tasks.update((task["task_id"], task) for task in full_tasks)
        artifacts, questions = {}, []
        for conversation in eligible:
            if "artifacts" in tables:
                artifacts.update(
                    (row["artifact_id"], row)
                    for row in _rows(
                        source,
                        "artifacts",
                        "WHERE conversation_id=? OR task_id IN "
                        "(SELECT task_id FROM tasks WHERE conversation_id=?)",
                        (conversation, conversation),
                    )
                )
            if "questions" in tables:
                questions.extend(
                    _rows(
                        source,
                        "questions",
                        "WHERE task_id IN (SELECT task_id FROM tasks WHERE conversation_id=?)",
                        (conversation,),
                    )
                )
        context_selection = explicit_contexts | {
            task[field]
            for tasks in eligible.values()
            for task in tasks
            for field in ("project_context_id", "previous_project_context_id")
            if task.get(field)
        }
        contexts = {}
        if "project_contexts" in tables:
            for context_id in context_selection:
                if any(
                    context_id
                    in (
                        task.get("project_context_id"),
                        task.get("previous_project_context_id"),
                    )
                    and task["conversation_id"] not in eligible
                    for task in all_tasks.values()
                ):
                    continue
                rows = _rows(
                    source, "project_contexts", "WHERE context_id=?", (context_id,)
                )
                if rows:
                    contexts[context_id] = rows[0]
        # Retain conversation flocks, not SQLite read locks, while copying histories.
        # Other legacy projects must remain able to commit their running tasks.
        source.rollback()
        imported = {
            row[0]
            for row in target.execute("SELECT conversation_id FROM legacy_migrations")
        }
        result["already_imported"] = len(imported & eligible.keys())
        pending = set(eligible) - imported
        links = {conversation: set() for conversation in pending}
        failures = {}
        selected_contexts = defaultdict(set)
        session_owners = defaultdict(set)
        for task in all_tasks.values():
            path = _canonical_cwd(task.get("session_file"))
            if path is not None:
                session_owners[path].add(task["conversation_id"])

        def owner(artifact_id):
            artifact = artifacts.get(artifact_id)
            task = all_tasks.get(artifact.get("task_id")) if artifact else None
            if (
                not task
                or task["conversation_id"] != artifact["conversation_id"]
                or task["conversation_id"] not in eligible
            ):
                raise _Deferred("foreign_or_unavailable_artifact_reference")
            if task["conversation_id"] in imported:
                existing = target.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
                ).fetchone()
                if existing is None or any(
                    existing[name] != value for name, value in artifact.items()
                ):
                    raise _Deferred("previously_imported_snapshot_changed")
            return task["conversation_id"]

        def link(conversation, other):
            if other in pending and other != conversation:
                links[conversation].add(other)
                links[other].add(conversation)

        def attach_context(conversation, context_id):
            context = contexts.get(context_id)
            if context is None:
                raise _Deferred("foreign_or_unavailable_context_reference")
            # A profile used by an ineligible legacy task is ambiguous, not project-owned.
            for task in all_tasks.values():
                if (
                    context_id
                    in (
                        task.get("project_context_id"),
                        task.get("previous_project_context_id"),
                    )
                    and task["conversation_id"] not in eligible
                ):
                    raise _Deferred("foreign_or_unavailable_context_reference")
            for artifact_id in _context_refs(context):
                link(conversation, owner(artifact_id))
            selected_contexts[conversation].add(context_id)

        for conversation in pending:
            try:
                task_ids = {task["task_id"] for task in eligible[conversation]}
                for task in eligible[conversation]:
                    for other in session_owners.get(
                        _canonical_cwd(task.get("session_file")), ()
                    ):
                        if other not in eligible:
                            raise _Deferred("foreign_or_unavailable_history_reference")
                        link(conversation, other)
                    refs = set()
                    for field in ("contract_json", "report_json"):
                        if task.get(field):
                            refs.update(_artifact_refs(json.loads(task[field])))
                    result_refs = json.loads(task.get("result_artifacts") or "[]")
                    if not isinstance(result_refs, list):
                        raise _Deferred("invalid_legacy_references")
                    for ref in result_refs:
                        refs.add(ref["artifact_id"] if isinstance(ref, dict) else ref)
                    for artifact_id in refs:
                        link(conversation, owner(artifact_id))
                    for field in ("project_context_id", "previous_project_context_id"):
                        if task.get(field):
                            attach_context(conversation, task[field])
                for artifact in artifacts.values():
                    if (
                        artifact.get("task_id") in task_ids
                        or artifact.get("conversation_id") == conversation
                    ) and owner(artifact["artifact_id"]) != conversation:
                        raise _Deferred("foreign_or_unavailable_artifact_reference")
            except (
                _Deferred,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                RecursionError,
            ) as exc:
                failures[conversation] = (
                    str(exc)
                    if isinstance(exc, _Deferred)
                    else "invalid_legacy_references"
                )
        for context_id in explicit_contexts:
            try:
                context = contexts.get(context_id)
                if context is None:
                    raise _Deferred("foreign_or_unavailable_context_reference")
                refs = _context_refs(context)
                owners = {owner(ref) for ref in refs}
                own_pending = owners & pending
                if not own_pending:
                    for task in all_tasks.values():
                        if (
                            context_id
                            in (
                                task.get("project_context_id"),
                                task.get("previous_project_context_id"),
                            )
                            and task["conversation_id"] not in eligible
                        ):
                            raise _Deferred("foreign_or_unavailable_context_reference")
                    if (
                        hashlib.sha256(context["context"].encode()).hexdigest()
                        != context["sha256"]
                    ):
                        raise _Deferred("legacy_checksum_mismatch")
                    target.execute("BEGIN IMMEDIATE")
                    added = _same_or_insert(
                        target, "project_contexts", "context_id", context
                    )
                    target.commit()
                    result["imported_contexts"] += added
                    continue
                for conversation in own_pending:
                    attach_context(conversation, context_id)
                    for other in owners:
                        link(conversation, other)
            except (
                _Deferred,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                RecursionError,
                sqlite3.Error,
            ) as exc:
                target.rollback()
                result["deferred_contexts"] += 1
                reasons[
                    str(exc)
                    if isinstance(exc, _Deferred)
                    else "invalid_legacy_references"
                ] += 1
        # Shared snapshots are one atomic component, including snapshots with no evidence.
        context_users = defaultdict(list)
        for conversation, ids in selected_contexts.items():
            for context_id in ids:
                context_users[context_id].append(conversation)
        for users in context_users.values():
            for other in users[1:]:
                link(users[0], other)
        sessions = scope.directory / "sessions"
        sessions.mkdir(mode=0o700, exist_ok=True)
        staging_root = sessions / ".legacy-imports"
        staging_root.mkdir(mode=0o700, exist_ok=True)
        # Directories are private to this importer. A crash before the SQLite commit
        # leaves an unreferenced directory; only those are safe to remove on retry.
        referenced = {
            Path(row[0])
            for row in target.execute(
                "SELECT session_file FROM tasks WHERE session_file IS NOT NULL"
            )
        }
        for directory in staging_root.iterdir():
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and directory.name.startswith("snapshot-")
                and not any(path.is_relative_to(directory) for path in referenced)
            ):
                shutil.rmtree(directory)
        while pending:
            start = min(pending)
            component, todo = set(), [start]
            while todo:
                conversation = todo.pop()
                if conversation not in component:
                    component.add(conversation)
                    todo.extend(links[conversation] - component)
            pending -= component
            staging = None
            try:
                for conversation in component:
                    if conversation in failures:
                        raise _Deferred(failures[conversation])
                tasks = [
                    dict(task)
                    for conversation in sorted(component)
                    for task in eligible[conversation]
                ]
                task_ids = {task["task_id"] for task in tasks}
                own_artifacts = [
                    row for row in artifacts.values() if row.get("task_id") in task_ids
                ]
                own_questions = [row for row in questions if row["task_id"] in task_ids]
                own_contexts = [
                    contexts[key]
                    for key in sorted(
                        set().union(
                            *(
                                selected_contexts[conversation]
                                for conversation in component
                            )
                        )
                    )
                ]
                if any(row["state"] == "pending" for row in own_questions):
                    raise _Deferred("legacy_pending_question")
                for conversation in component:
                    if target.execute(
                        "SELECT 1 FROM tasks WHERE conversation_id=? LIMIT 1",
                        (conversation,),
                    ).fetchone():
                        raise _Deferred("destination_conversation_exists")
                for row in own_artifacts:
                    if (
                        hashlib.sha256(row["content"].encode()).hexdigest()
                        != row["sha256"]
                    ):
                        raise _Deferred("legacy_checksum_mismatch")
                for row in own_contexts:
                    if (
                        hashlib.sha256(row["context"].encode()).hexdigest()
                        != row["sha256"]
                    ):
                        raise _Deferred("legacy_checksum_mismatch")
                plans, missing = _history_plan(tasks, scope.base, allowed)
                size = sum(
                    path.stat().st_size
                    for files, _ in plans.values()
                    for path, _ in files
                )
                remaining = (
                    None if max_bytes is None else max_bytes - result["copied_bytes"]
                )
                if remaining is not None and size > remaining:
                    raise _Deferred(
                        "history_budget_exceeded_use_explicit_unlimited_migration"
                    )
                rewritten, copied = {}, 0
                if plans:
                    staging = Path(
                        tempfile.mkdtemp(prefix="snapshot-", dir=staging_root)
                    )
                    rewritten, copied = _copy_history(plans, staging, remaining)
                    fd = os.open(staging_root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                target.execute("BEGIN IMMEDIATE")
                for conversation in component:
                    if target.execute(
                        "SELECT 1 FROM tasks WHERE conversation_id=? LIMIT 1",
                        (conversation,),
                    ).fetchone():
                        raise _Deferred("destination_conversation_exists")
                new_contexts = sum(
                    _same_or_insert(target, "project_contexts", "context_id", row)
                    for row in own_contexts
                )
                for row in own_artifacts:
                    _insert(target, "artifacts", row)
                for task in tasks:
                    task["session_file"] = rewritten.get(task.get("session_file"))
                    _insert(target, "tasks", task)
                for row in own_questions:
                    _insert(target, "questions", row)
                for conversation in component:
                    snapshot = json.dumps(
                        eligible[conversation], sort_keys=True, separators=(",", ":")
                    )
                    target.execute(
                        "INSERT INTO legacy_migrations (conversation_id, source_identity, snapshot_sha256) VALUES (?, ?, ?)",
                        (
                            conversation,
                            identity,
                            hashlib.sha256(snapshot.encode()).hexdigest(),
                        ),
                    )
                target.commit()
                result["imported_conversations"] += len(component)
                result["imported_tasks"] += len(tasks)
                result["imported_artifacts"] += len(own_artifacts)
                result["imported_questions"] += len(own_questions)
                result["imported_contexts"] += new_contexts
                result["history_unavailable_tasks"] += missing
                result["copied_bytes"] += copied
            except (
                _Deferred,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                RecursionError,
                sqlite3.Error,
            ) as exc:
                target.rollback()
                if staging is not None and staging.exists():
                    shutil.rmtree(staging)
                result["deferred_conversations"] += len(component)
                reason = (
                    str(exc)
                    if isinstance(exc, _Deferred)
                    else "legacy_snapshot_unavailable_or_destination_collision"
                )
                reasons[reason] += len(component)
    result["status"] = (
        "imported"
        if result["imported_conversations"] or result["imported_contexts"]
        else "unchanged"
    )
    if (result["deferred_conversations"] or result["deferred_contexts"]) and not (
        result["imported_conversations"] or result["imported_contexts"]
    ):
        result["status"] = "deferred"
    result["reasons"] = dict(sorted(reasons.items()))
    return result
