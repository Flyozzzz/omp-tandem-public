"""Owned detached Git snapshots; edit isolation, not an operating-system sandbox."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .work_items import shell_permission
from .workspace import ProjectScope, _directory, _private_lock

_MAX_OUTPUT = 16 * 1024 * 1024
_MAX_FILE = 16 * 1024 * 1024
_MAX_SNAPSHOT = 256 * 1024 * 1024
_MAX_FILES = 20000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Ownership requires exact relative POSIX file paths")
    parts = value.split("/")
    if (
        PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} or part.lower() == ".git" for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"Unsafe workspace path: {value!r}")
    return value


class WorkWorkspace:
    """Preserve attempt directories and never implicitly update the user's checkout."""

    def __init__(self, scope: ProjectScope):
        self.scope = scope
        self.directory = scope.directory / "worktrees"

    def _identity(self) -> None:
        scope = self.scope
        expected = {
            "version": 1,
            "project_root": str(scope.root),
            "scope_id": scope.key,
        }
        descriptor = scope.directory / "scope.json"
        if (
            scope.root.resolve() != scope.root
            or hashlib.sha256(str(scope.root).encode()).hexdigest() != scope.key
            or scope.directory != scope.base / "projects" / scope.key
            or scope.directory.resolve() != scope.directory
            or descriptor.is_symlink()
            or json.loads(descriptor.read_text(encoding="utf-8")) != expected
        ):
            raise ValueError(
                "Workspace state identity does not match the launch project"
            )
        if scope.directory.is_relative_to(scope.root):
            raise ValueError(
                "Shared workspace state must be outside the user's active checkout"
            )

    @contextmanager
    def _lock(self):
        # Identity must be checked before creating a directory, lock, or Git object.
        self._identity()
        _directory(self.directory)
        os.chmod(self.directory, 0o700)
        with _private_lock(self.directory / ".lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _git(
        self,
        cwd: Path,
        *args: str,
        data: bytes | None = None,
        allow_failure: bool = False,
        index: Path | None = None,
        output_limit: int = _MAX_OUTPUT,
    ) -> bytes | None:
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
            GIT_ATTR_NOSYSTEM="1",
            GIT_AUTHOR_NAME="OMP Tandem",
            GIT_AUTHOR_EMAIL="tandem@example.invalid",
            GIT_COMMITTER_NAME="OMP Tandem",
            GIT_COMMITTER_EMAIL="tandem@example.invalid",
            GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
            GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
        )
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
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
                        "core.untrackedCache=false",
                        "-c",
                        "diff.external=",
                        "-c",
                        "core.attributesFile=/dev/null",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "tag.gpgsign=false",
                        "-c",
                        "core.autocrlf=false",
                        "-c",
                        "core.safecrlf=false",
                        "-c",
                        "core.eol=lf",
                        "-c",
                        "merge.verifySignatures=false",
                        "-c",
                        "gc.auto=0",
                        "-c",
                        "maintenance.auto=false",
                        *args,
                    ],
                    cwd=cwd,
                    env=environment,
                    input=data,
                    stdin=subprocess.DEVNULL if data is None else None,
                    stdout=output,
                    stderr=errors,
                    timeout=30,
                    check=False,
                )
            except FileNotFoundError:
                raise ValueError("Git is required for shared workspaces") from None
            except subprocess.TimeoutExpired:
                raise ValueError(
                    f"Workspace Git operation exceeded 30 seconds: {args[0]}"
                ) from None
            if result.returncode:
                if allow_failure:
                    return None
                errors.seek(0)
                raise ValueError(
                    "Workspace Git operation failed: "
                    + errors.read(2000).decode("utf-8", "replace")
                )
            if output.tell() > output_limit:
                raise ValueError(
                    "Workspace Git output exceeds its bounded safety limit"
                )
            output.seek(0)
            return output.read()

    def _repository(self) -> None:
        root = self.scope.root
        top = self._git(root, "rev-parse", "--show-toplevel").decode().strip()
        if Path(top).resolve() != root:
            raise ValueError("The project root must be the Git repository root")
        # Checkout/status may execute configured clean/smudge filters. Refuse them,
        # rather than treating worktree isolation as a sandbox for repository code.
        filters = self._git(
            root, "config", "--get-regexp", r"^filter\.", allow_failure=True
        )
        if filters:
            raise ValueError(
                "Git clean/smudge filters are not allowed in shared workspaces"
            )

    def source_commit(self) -> str:
        """Read the exact launch checkout HEAD for the store's authorization pin."""
        self._identity()
        self._repository()
        return (
            self._git(self.scope.root, "rev-parse", "--verify", "HEAD^{commit}")
            .decode()
            .strip()
        )

    def _commit(self, value: str) -> str:
        if not isinstance(value, str) or not _COMMIT.fullmatch(value):
            raise ValueError("An immutable full Git commit ID is required")
        actual = (
            self._git(self.scope.root, "rev-parse", "--verify", f"{value}^{{commit}}")
            .decode()
            .strip()
        )
        if actual != value:
            raise ValueError("The submitted object must itself be a commit")
        return actual

    def _tree(self, commit: str) -> dict[str, tuple[str, str]]:
        result = {}
        for entry in self._git(self.scope.root, "ls-tree", "-rz", commit).split(b"\0"):
            if not entry:
                continue
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split()
            path = _path(raw_path.decode("utf-8"))
            if mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError(
                    f"Symlinks, submodules and nonregular files are unsupported: {path}"
                )
            result[path] = (mode, object_id)
            if len(result) > _MAX_FILES:
                raise ValueError("Workspace exceeds the file-count safety limit")
        return result

    def _attempt_path(self, attempt: dict) -> Path:
        identifier = attempt.get("attempt_id", "")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError("Unsafe workspace attempt identifier")
        path = self.directory / identifier
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("Attempt workspace must not follow symbolic links")
        return path

    @staticmethod
    def _owned(attempt: dict, plan: dict) -> list[str]:
        steps = [step for step in plan["steps"] if step["id"] == attempt["step_id"]]
        if len(steps) != 1:
            raise ValueError("Attempt step is absent or ambiguous in the agreed plan")
        owned = [_path(path) for path in steps[0]["owned_files"]]
        if len(owned) != len(set(owned)):
            raise ValueError("Duplicate owned file paths")
        return sorted(owned)

    def _new_commit(self, cwd: Path, tree: str, parent: str, message: str) -> str:
        return (
            self._git(
                cwd, "commit-tree", tree, "-p", parent, data=(message + "\n").encode()
            )
            .decode()
            .strip()
        )

    def _apply_delta(self, path: Path, parent: str, commit: str) -> None:
        patch = self._git(
            self.scope.root,
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            parent,
            commit,
            "--",
        )
        if patch:
            self._git(
                path,
                "apply",
                "--cached",
                "--3way",
                "--whitespace=nowarn",
                data=patch,
            )

    def _materialize(self, path: Path, tree: str) -> None:
        """Write exact saved blobs, not smudged/eol/ident checkout variants."""
        entries = self._tree(tree)
        identifiers = list(dict.fromkeys(oid for _mode, oid in entries.values()))
        if not identifiers:
            return
        request = ("\n".join(identifiers) + "\n").encode("ascii")
        sizes = {}
        for row in self._git(
            path, "cat-file", "--batch-check", data=request
        ).splitlines():
            identifier, kind, size = row.decode("ascii").split()
            if kind != "blob" or identifier not in identifiers:
                raise ValueError("Snapshot contains an unexpected Git object")
            sizes[identifier] = int(size)
        if set(sizes) != set(identifiers) or any(
            size > _MAX_FILE for size in sizes.values()
        ):
            raise ValueError("Workspace blob exceeds the per-file safety limit")
        if sum(sizes[oid] for _mode, oid in entries.values()) > _MAX_SNAPSHOT:
            raise ValueError("Workspace snapshot exceeds the 256 MiB safety limit")
        payload = self._git(
            path,
            "cat-file",
            "--batch",
            data=request,
            output_limit=sum(sizes.values()) + 128 * len(identifiers),
        )
        offsets = {}
        position = 0
        for identifier in identifiers:
            end = payload.index(b"\n", position)
            header = payload[position:end].decode("ascii").split()
            size = sizes[identifier]
            if header != [identifier, "blob", str(size)]:
                raise ValueError("Git blob response does not match the snapshot")
            position = end + 1
            offsets[identifier] = (position, position + size)
            position += size + 1
        if position != len(payload):
            raise ValueError("Unexpected trailing snapshot data")
        view = memoryview(payload)
        for relative, (mode, identifier) in entries.items():
            destination = path / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o755 if mode == "100755" else 0o644,
            )
            with os.fdopen(descriptor, "wb") as target:
                start, end = offsets[identifier]
                target.write(view[start:end])

    def _checkpoint(
        self, attempt: dict, source: str, owned: list[str]
    ) -> tuple[str, str] | None:
        checkpoint = attempt.get("checkpoint")
        if checkpoint is None:
            return None
        if (
            checkpoint.get("step_id") != attempt["step_id"]
            or checkpoint.get("plan_revision") != attempt.get("plan_revision")
            or checkpoint.get("plan_revision") is None
        ):
            raise ValueError(
                "Checkpoint does not belong to this step and current plan revision"
            )
        parent = self._commit(checkpoint.get("base_commit"))
        commit = self._commit(checkpoint.get("commit"))
        for ancestor, descendant in ((source, parent), (parent, commit)):
            if (
                self._git(
                    self.scope.root,
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                    allow_failure=True,
                )
                is None
            ):
                raise ValueError(
                    "Checkpoint does not descend from the pinned source and base"
                )
        baseline, saved = self._tree(parent), self._tree(commit)
        changed = sorted(
            name
            for name in baseline.keys() | saved.keys()
            if baseline.get(name) != saved.get(name)
        )
        if set(changed) - set(owned):
            raise ValueError("Checkpoint changes files outside declared step ownership")
        if checkpoint.get("changed_files") != changed:
            raise ValueError(
                "Checkpoint changed-file manifest does not match its commit"
            )
        tree = (
            self._git(self.scope.root, "rev-parse", f"{commit}^{{tree}}")
            .decode()
            .strip()
        )
        if checkpoint.get("tree_hash") != tree:
            raise ValueError("Checkpoint tree hash does not match its immutable commit")
        return parent, commit

    def prepare(self, attempt: dict, plan: dict, dependencies: list[dict]) -> dict:
        """Compose accepted ancestor deltas in supplied canonical DAG order."""
        with self._lock():
            self._repository()
            path = self._attempt_path(attempt)
            manifest_path = self.directory / f"{attempt['attempt_id']}.json"
            if path.exists() or manifest_path.exists() or manifest_path.is_symlink():
                raise ValueError(
                    f"Attempt workspace already exists; preserve and reconcile it: {path}"
                )
            owned = self._owned(attempt, plan)
            source = self._commit(attempt.get("source_commit"))
            kind = attempt.get("kind")
            if kind not in {"implement", "review"}:
                raise ValueError("Unknown workspace attempt kind")
            base = (
                self._commit(attempt["submission"]["commit"])
                if kind == "review"
                else source
            )
            self._tree(base)
            verified = []
            checkpoint = (
                self._checkpoint(attempt, source, owned)
                if kind == "implement"
                else None
            )
            if kind == "implement":
                seen = set()
                for dependency in dependencies:
                    commit = self._commit(dependency["commit"])
                    parent = self._commit(dependency["base_commit"])
                    if commit in seen:
                        continue
                    seen.add(commit)
                    self._tree(parent)
                    self._tree(commit)
                    if (
                        self._git(
                            self.scope.root,
                            "merge-base",
                            "--is-ancestor",
                            source,
                            parent,
                            allow_failure=True,
                        )
                        is None
                    ):
                        raise ValueError(
                            "Accepted dependency belongs to a different pinned source"
                        )
                    verified.append((parent, commit))
            self._git(
                self.scope.root,
                "worktree",
                "add",
                "--no-checkout",
                "--detach",
                str(path),
                base,
            )
            os.chmod(path, 0o700)
            try:
                self._git(path, "read-tree", base)
                for parent, commit in verified:
                    self._apply_delta(path, parent, commit)
                if verified:
                    tree = self._git(path, "write-tree").decode().strip()
                    base = self._new_commit(
                        path, tree, source, "Compose accepted shared-work dependencies"
                    )
                    self._git(path, "update-ref", "--no-deref", "HEAD", base, source)
                if checkpoint is not None:
                    # Keep the dependency-only base: finish must include all saved
                    # partial edits, not merely edits made by this continuation.
                    self._apply_delta(path, *checkpoint)
                self._materialize(path, self._git(path, "write-tree").decode().strip())
                metadata = {
                    "path": str(path),
                    "base_commit": base,
                    "source_commit": source,
                    "step_id": attempt["step_id"],
                    "kind": kind,
                    "owned_files": owned,
                }
                with manifest_path.open("x", encoding="utf-8") as stream:
                    os.chmod(manifest_path, 0o600)
                    json.dump(metadata, stream, sort_keys=True)
                return {"path": str(path), "base_commit": base}
            except (ValueError, OSError) as error:
                raise ValueError(
                    f"Workspace preparation failed; conflict/recovery evidence preserved at {path}: {error}"
                ) from error

    def _metadata(
        self, attempt: dict, plan: dict, workspace: dict
    ) -> tuple[Path, dict]:
        path = self._attempt_path(attempt)
        manifest = self.directory / f"{attempt['attempt_id']}.json"
        if manifest.is_symlink():
            raise ValueError("Workspace metadata must not be a symbolic link")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            metadata["path"] != str(path)
            or workspace.get("path") != str(path)
            or workspace.get("base_commit") != metadata["base_commit"]
            or metadata["source_commit"] != attempt.get("source_commit")
            or metadata["kind"] != attempt.get("kind")
            or metadata["step_id"] != attempt.get("step_id")
            or metadata["owned_files"] != self._owned(attempt, plan)
        ):
            raise ValueError(
                "Workspace does not match the reserved attempt and agreed ownership"
            )
        marker = path / ".git"
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("Attempt Git metadata has been replaced")
        common = (
            self._git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .decode()
            .strip()
        )
        expected = (
            self._git(
                self.scope.root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
            .decode()
            .strip()
        )
        gitdir = Path(
            self._git(path, "rev-parse", "--absolute-git-dir").decode().strip()
        )
        if common != expected or gitdir.parent != Path(expected) / "worktrees":
            raise ValueError(
                "Attempt no longer belongs to the project's detached worktrees"
            )
        backlink = gitdir / "gitdir"
        if backlink.is_symlink() or backlink.read_text().strip() != str(marker):
            raise ValueError("Attempt Git metadata points at another checkout")
        if (
            self._git(path, "symbolic-ref", "-q", "HEAD", allow_failure=True)
            is not None
        ):
            raise ValueError("Attempt must remain detached, not on a user branch")
        if (
            self._git(path, "rev-parse", "HEAD").decode().strip()
            != metadata["base_commit"]
        ):
            raise ValueError(
                "Attempt HEAD changed; preserve model-created commits for reconciliation"
            )
        return path, metadata

    @staticmethod
    def _read_file(root: Path, path: str) -> tuple[bytes, str]:
        descriptors = []
        try:
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(parent)
            parts = _path(path).split("/")
            for part in parts[:-1]:
                parent = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
                descriptors.append(parent)
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE:
                raise ValueError(
                    f"Workspace file is nonregular or exceeds 16 MiB: {path}"
                )
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                content = stream.read(_MAX_FILE + 1)
            after = os.fstat(descriptor)

            def signature(item):
                return (
                    item.st_dev,
                    item.st_ino,
                    item.st_mode,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ctime_ns,
                )

            if len(content) > _MAX_FILE or signature(before) != signature(after):
                raise ValueError(f"Workspace file changed during snapshot: {path}")
            return content, "100755" if before.st_mode & 0o111 else "100644"
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _snapshot(
        self,
        path: Path,
        baseline: dict,
        owned: set[str],
        *,
        capture: bool = True,
        allow_ignored: bool = False,
    ) -> tuple[dict, dict]:
        def inaccessible(error):
            raise ValueError(
                f"Workspace directory cannot be inspected: {error.filename}"
            ) from error

        files = set()
        ignored = set()
        if allow_ignored:
            ignored = {
                _path(name.decode())
                for name in self._git(
                    path,
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "-z",
                ).split(b"\0")
                if name
            }
            ignored.difference_update(owned)
            ignored.difference_update(baseline)
        for directory, directories, names in os.walk(
            path, followlinks=False, onerror=inaccessible
        ):
            if Path(directory) == path:
                names = [name for name in names if name != ".git"]
            for name in directories[:]:
                item = Path(directory) / name
                if item.relative_to(path).as_posix() in ignored:
                    directories.remove(name)
                    continue
                if item.is_symlink() or name.lower() == ".git":
                    raise ValueError(
                        f"Unsafe workspace directory: {item.relative_to(path)}"
                    )
            for name in names:
                relative = _path((Path(directory) / name).relative_to(path).as_posix())
                if relative in ignored:
                    continue
                if relative not in baseline and relative not in owned:
                    raise ValueError(
                        f"Unexpected untracked or ignored file outside ownership: {relative}"
                    )
                files.add(relative)
                if len(files) > _MAX_FILES:
                    raise ValueError("Workspace exceeds the file-count safety limit")
        algorithm = (
            self._git(path, "rev-parse", "--show-object-format").decode().strip()
        )
        if algorithm not in {"sha1", "sha256"}:
            raise ValueError("Unsupported Git object format")
        snapshot, contents, total = {}, {}, 0
        for relative in sorted(files):
            try:
                content, mode = self._read_file(path, relative)
            except OSError as error:
                raise ValueError(
                    f"Unsafe or concurrently modified workspace file: {relative}"
                ) from error
            total += len(content)
            if total > _MAX_SNAPSHOT:
                raise ValueError("Workspace snapshot exceeds the 256 MiB safety limit")
            hasher = hashlib.new(algorithm, f"blob {len(content)}\0".encode())
            hasher.update(content)
            digest = hasher.hexdigest()
            snapshot[relative] = (mode, digest)
            if baseline.get(relative) != snapshot[relative]:
                if relative not in owned:
                    raise ValueError(
                        f"Unexpected modification outside ownership: {relative}"
                    )
                if capture:
                    contents[relative] = content
        removed = baseline.keys() - snapshot.keys()
        if removed - owned:
            raise ValueError(
                f"Unexpected deletion outside ownership: {sorted(removed - owned)[0]}"
            )
        return snapshot, contents

    def _retain(self, attempt: dict, commit: str) -> None:
        reference = f"refs/omp-tandem/work/{attempt['attempt_id']}"
        existing = self._git(
            self.scope.root, "rev-parse", "--verify", reference, allow_failure=True
        )
        if existing is not None and existing.decode().strip() != commit:
            raise ValueError("Attempt already published a different immutable snapshot")
        if existing is None:
            self._git(
                self.scope.root, "update-ref", reference, commit, "0" * len(commit)
            )

    def adopt_submission(self, attempt: dict, plan: dict, commit: str) -> dict:
        """Validate an already committed manual claim without checking out files."""
        with self._lock():
            self._repository()
            self._attempt_path(attempt)
            if (
                attempt.get("autonomous") is not False
                or attempt.get("kind") != "implement"
            ):
                raise ValueError(
                    "Only a manual implementation claim can adopt a submitted commit"
                )
            source = self._commit(attempt.get("source_commit"))
            commit = self._commit(commit)
            if (
                self._git(
                    self.scope.root,
                    "merge-base",
                    "--is-ancestor",
                    source,
                    commit,
                    allow_failure=True,
                )
                is None
            ):
                raise ValueError(
                    "Manual submission must descend from the claim's pinned source commit"
                )
            baseline, submitted = self._tree(source), self._tree(commit)
            owned = set(self._owned(attempt, plan))
            changed = sorted(
                name
                for name in baseline.keys() | submitted.keys()
                if baseline.get(name) != submitted.get(name)
            )
            if set(changed) - owned:
                raise ValueError(
                    f"Manual submission changes paths outside ownership: {sorted(set(changed) - owned)[0]}"
                )
            tree = (
                self._git(self.scope.root, "rev-parse", f"{commit}^{{tree}}")
                .decode()
                .strip()
            )
            self._retain(attempt, commit)
            return {
                "commit": commit,
                "base_commit": source,
                "workspace": str(self.scope.root),
                "tree_hash": tree,
                "changed_files": changed,
            }

    def verify_review(self, attempt: dict, plan: dict, workspace: dict) -> None:
        """Reject acceptance if the reviewer changed the submitted checkout."""
        with self._lock():
            self._repository()
            path, metadata = self._metadata(attempt, plan, workspace)
            if metadata["kind"] != "review":
                raise ValueError("Only review attempts can verify review snapshots")
            commit = self._commit(attempt["submission"]["commit"])
            if metadata["base_commit"] != commit:
                raise ValueError("Reviewer workspace is not the exact submitted commit")
            if self._git(path, "ls-files", "-u", "-z"):
                raise ValueError("Reviewer left unresolved index conflicts")
            if self._git(
                path,
                "diff",
                "--cached",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                commit,
                "--",
            ):
                raise ValueError("Reviewer modified the submitted index")
            baseline = self._tree(commit)
            # Explicitly granted tests may create ignored caches; they are neither
            # reviewed input nor submission output. This is not a shell sandbox.
            self._snapshot(
                path,
                baseline,
                set(),
                capture=False,
                allow_ignored=shell_permission(attempt),
            )

    def finish(self, attempt: dict, plan: dict, workspace: dict) -> dict:
        """Commit a verified raw-byte snapshot, including owned additions/deletions."""
        with self._lock():
            self._repository()
            path, metadata = self._metadata(attempt, plan, workspace)
            if metadata["kind"] != "implement":
                raise ValueError(
                    "Review attempts cannot publish implementation snapshots"
                )
            base = self._commit(metadata["base_commit"])
            baseline = self._tree(base)
            owned = set(metadata["owned_files"])
            if self._git(path, "ls-files", "-u", "-z"):
                raise ValueError("Unresolved index conflicts require reconciliation")
            staged = self._git(
                path,
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "-z",
                base,
                "--",
            )
            if any(
                _path(name.decode()) not in owned
                for name in staged.split(b"\0")
                if name
            ):
                raise ValueError("Unexpected staged modification outside ownership")
            allow_ignored = shell_permission(attempt)
            snapshot, contents = self._snapshot(
                path, baseline, owned, allow_ignored=allow_ignored
            )
            changed = sorted(
                name
                for name in baseline.keys() | snapshot.keys()
                if baseline.get(name) != snapshot.get(name)
            )
            # Build a separate index from the trusted base; the model's index is not
            # an authority, and Git clean filters never transform submitted bytes.
            with tempfile.TemporaryDirectory(
                prefix=".index-", dir=self.directory
            ) as temporary:
                index = Path(temporary) / "index"
                self._git(path, "read-tree", base, index=index)
                records = []
                for name in changed:
                    if name not in snapshot:
                        records.append(
                            b"0 " + b"0" * len(base) + b"\t" + name.encode() + b"\0"
                        )
                    else:
                        mode, digest = snapshot[name]
                        actual = (
                            self._git(
                                path,
                                "hash-object",
                                "-w",
                                "--stdin",
                                "--no-filters",
                                data=contents[name],
                            )
                            .decode()
                            .strip()
                        )
                        if actual != digest:
                            raise ValueError(
                                "Git blob hash differs from captured workspace bytes"
                            )
                        records.append(
                            f"{mode} {digest}\t".encode() + name.encode() + b"\0"
                        )
                if records:
                    self._git(
                        path,
                        "update-index",
                        "-z",
                        "--index-info",
                        data=b"".join(records),
                        index=index,
                    )
                tree = self._git(path, "write-tree", index=index).decode().strip()
            # Detect concurrent edits before promotion; leave the real index/HEAD
            # untouched, so retries and recovery retain the original evidence.
            observed, _ = self._snapshot(
                path, baseline, owned, capture=False, allow_ignored=allow_ignored
            )
            if observed != snapshot:
                raise ValueError(
                    "Workspace changed during snapshot; no submission was promoted"
                )
            self._metadata(attempt, plan, workspace)
            commit = self._new_commit(
                path, tree, base, f"Submit shared-work step {attempt['step_id']}"
            )
            if self._tree(commit) != snapshot:
                raise ValueError(
                    "Submitted commit does not match the verified workspace snapshot"
                )
            # Detached objects must survive Git GC while their submissions are stored.
            self._retain(attempt, commit)
            return {
                "commit": commit,
                "base_commit": base,
                "workspace": str(path),
                "tree_hash": tree,
                "changed_files": changed,
            }

    def apply(self, output: dict, expected_head: str) -> dict:
        """Explicit operator-only fast-forward; caller must enforce CLI authority."""
        with self._lock():
            self._repository()
            expected = self._commit(expected_head)
            commit = self._commit(output["commit"])
            incoming = self._tree(commit)
            tree = (
                self._git(self.scope.root, "rev-parse", f"{commit}^{{tree}}")
                .decode()
                .strip()
            )
            if output.get("tree_hash") != tree:
                raise ValueError("Output tree hash does not match its immutable commit")
            root = self.scope.root
            if self._git(root, "rev-parse", "HEAD").decode().strip() != expected:
                raise ValueError(
                    "Project HEAD changed; inspect and integrate the saved output explicitly"
                )
            if self._git(root, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError(
                    "Project tracked and nonignored files must be clean before explicit application"
                )
            if (
                self._git(
                    root,
                    "merge-base",
                    "--is-ancestor",
                    expected,
                    commit,
                    allow_failure=True,
                )
                is None
            ):
                raise ValueError(
                    "Output cannot fast-forward the expected HEAD; no checkout changes were made"
                )
            current = self._tree(expected)
            for name in incoming.keys() - current.keys():
                target = root / name
                if target.exists() or target.is_symlink():
                    raise ValueError(
                        f"Application would overwrite an ignored path: {name}"
                    )
                for parent in target.parents:
                    if parent == root:
                        break
                    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                        raise ValueError(
                            f"Application would overwrite an ignored parent: {parent.relative_to(root)}"
                        )
            self._git(
                root,
                "merge",
                "--ff-only",
                "--no-edit",
                "--no-autostash",
                "--no-overwrite-ignore",
                commit,
            )
            return {
                "commit": commit,
                "tree_hash": tree,
                "project_root": str(root),
                "applied": True,
            }
