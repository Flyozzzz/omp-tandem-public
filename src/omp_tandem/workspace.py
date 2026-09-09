"""Launch-bound MCP data namespaces and process-wide worker capacity."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _private_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "a")


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("State directories must not alias another workspace")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass(frozen=True)
class ProjectScope:
    root: Path
    base: Path
    key: str
    directory: Path
    root_source: str

    def info(self) -> dict:
        return {
            "project_root": str(self.root),
            "scope_id": self.key,
            "root_source": self.root_source,
            "isolation": "launch_project",
            "filesystem_sandbox": False,
        }

    def validate_cwd(self, value, *, allowed_roots=()) -> Path:
        candidate = self.root if value is None else Path(value).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                "cwd must be an absolute directory in this project's granted roots"
            )
        candidate = candidate.resolve()
        if not any(
            candidate.is_relative_to(root) for root in (self.root, *allowed_roots)
        ):
            raise ValueError(
                "cwd is outside this launch project's client-granted roots; "
                "use that project's coordinator session or explicitly grant the directory in your MCP client"
            )
        if not candidate.is_dir():
            raise ValueError("cwd must be an existing directory")
        return candidate


def resolve_scope(
    state_base: Path, project_root: Path | None = None, *, source: str | None = None
) -> ProjectScope:
    # Only operator/client launch state selects a namespace. Never accept a task's cwd here.
    selected = (
        project_root
        if project_root is not None
        else os.environ.get("CLAUDE_PROJECT_DIR")
    )
    root_source = source or (
        "operator_override"
        if project_root is not None
        else "claude_project_dir"
        if selected is not None
        else "launch_cwd"
    )
    if (
        project_root is None
        and selected is not None
        and (not selected or not Path(selected).is_absolute())
    ):
        raise ValueError("CLAUDE_PROJECT_DIR must be an absolute existing directory")
    if selected is None and any(
        os.environ.get(name)
        for name in (
            "PLUGIN_ROOT",
            "PLUGIN_DATA",
            "CLAUDE_PLUGIN_ROOT",
            "CLAUDE_PLUGIN_DATA",
        )
    ):
        raise ValueError(
            "Plugin launch requires client workspace metadata or explicit --project-root"
        )
    root = (
        Path(selected).expanduser() if selected is not None else Path.cwd()
    ).resolve()
    if selected == "" or not root.is_dir():
        raise ValueError("The trusted launch project must be an existing directory")
    base = state_base.expanduser().resolve()
    key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    directory = base / "projects" / key
    _directory(base)
    with _private_lock(base / ".scopes.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _directory(base / "projects")
        _directory(directory)
        descriptor = directory / "scope.json"
        expected = {"version": 1, "project_root": str(root), "scope_id": key}
        if descriptor.is_symlink():
            raise ValueError("Workspace identity must not be a symbolic link")
        if descriptor.exists():
            if json.loads(descriptor.read_text(encoding="utf-8")) != expected:
                raise ValueError(
                    "Workspace state identity does not match the launch project"
                )
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".scope-",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                try:
                    json.dump(expected, file, ensure_ascii=False, sort_keys=True)
                    file.flush()
                    os.fsync(file.fileno())
                    os.replace(temporary, descriptor)
                finally:
                    temporary.unlink(missing_ok=True)
    return ProjectScope(root, base, key, directory, root_source)


def client_root_paths(roots) -> tuple[Path, ...]:
    """Decode trusted MCP roots/list responses, never user-supplied tool arguments."""
    paths = set()
    for root in roots:
        uri = urlsplit(str(root.uri))
        if (
            uri.scheme != "file"
            or uri.netloc not in ("", "localhost")
            or uri.query
            or uri.fragment
        ):
            continue
        path = Path(unquote(uri.path))
        if path.is_absolute() and path.is_dir():
            paths.add(path.resolve())
    return tuple(sorted(paths))


class WorkerSlots:
    """Four kernel-held leases shared across projects, without exposing their tasks."""

    def __init__(self, base: Path):
        self.directory = base / "worker-slots"
        _directory(self.directory)

    def acquire(self):
        for index in range(4):
            handle = _private_lock(self.directory / f"{index}.lock")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
            except BaseException:
                handle.close()
                raise
            else:
                return handle
        raise ValueError(
            "All four shared OMP worker slots are busy; try again after work finishes"
        )
