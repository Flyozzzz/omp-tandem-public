"""Stdlib-only preparation shared by the public launcher and manual installer."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


class BootstrapError(RuntimeError):
    """A prerequisite or runtime preparation failed; safe to show on stderr."""


def doctor(root: Path) -> dict:
    """Inspect local executable availability, never provider credentials or login."""
    tools = {name: shutil.which(name) for name in ("uv", "omp")}
    missing = [name for name, executable in tools.items() if executable is None]
    if sys.version_info < (3, 12):
        missing.append("python>=3.12")
    if os.name != "posix":
        missing.append("macOS/Linux or WSL")
    for name in ("pyproject.toml", "uv.lock", "src/omp_tandem/__init__.py"):
        if not (root / name).is_file():
            missing.append(name)
    return {
        "status": "blocked" if missing else "ready",
        "tools": tools,
        "python": sys.executable,
        "python_version": platform.python_version(),
        "missing": missing,
        "authentication": "not_checked",
        "message": "Provider authentication is required and was not checked.",
        "live_check": {
            "tool": "tandem_diagnose",
            "arguments": {"live": True},
            "requires_user_request": True,
            "scope": "Run in the actual connected MCP client session; may incur provider cost.",
        },
        "requirements": {
            "uv": "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
            "omp": "Install OMP once: https://omp.sh/; configure your own provider login separately.",
        },
    }


def _content_key(root: Path) -> str:
    # Hash bytes and relative names: relocation does not invalidate an identical
    # installed package, but resources and metadata changes always do.
    paths = {root / "pyproject.toml", root / "uv.lock"}
    paths.update(
        path for path in (root / ".gitignore", root / ".hgignore") if path.is_file()
    )
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    project = metadata.get("project", {})
    for field in ("readme", "license"):
        value = project.get(field)
        filename = (
            value
            if field == "readme" and isinstance(value, str)
            else (value.get("file") if isinstance(value, dict) else None)
        )
        if filename:
            candidate = (root / filename).resolve()
            if not candidate.is_relative_to(root):
                raise BootstrapError(f"Build input must be inside the package: {field}")
            paths.add(candidate)
    for pattern in project.get("license-files", []):
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise BootstrapError("License files must remain inside the package")
        for path in root.glob(pattern):
            candidate = path.resolve()
            if not candidate.is_relative_to(root):
                raise BootstrapError("License files must remain inside the package")
            if candidate.is_file():
                paths.add(candidate)
    paths.update(
        path
        for path in (root / "src").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in (".pyc", ".pyo")
    )
    digest = hashlib.sha256()
    interpreter = Path(sys.executable).resolve()
    stat = interpreter.stat()
    identity = (
        "omp-tandem-runtime-v1",
        sys.version,
        sys.implementation.name,
        sys.implementation.cache_tag,
        platform.machine(),
        sys.platform,
        str(interpreter),
        stat.st_size,
        stat.st_mtime_ns,
    )
    digest.update(json.dumps(identity).encode())
    for path in sorted(paths):
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _cache_root(root: Path) -> Path:
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    base = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".cache" / "omp-tandem"
    )
    cache = (base / "runtimes").resolve()
    if cache.is_relative_to(root):
        raise BootstrapError(
            "Runtime cache must be outside the plugin checkout; set PLUGIN_DATA to a writable private directory."
        )
    return cache


def _run(command: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    # Dependency processes must not consume MCP stdin or write protocol stdout.
    result = subprocess.run(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    for output in (result.stdout, result.stderr):
        if output:
            print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
    return result


def _usable(python: Path) -> bool:
    if not python.is_file():
        return False
    try:
        return (
            _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import sys; assert sys.version_info >= (3, 12); import omp_tandem.cli",
                ]
            ).returncode
            == 0
        )
    except OSError:
        return False


def _digest_generation(generation: Path) -> str:
    """Seal installed bytes, not mutable bytecode caches created on import."""
    digest = hashlib.sha256()
    for path in sorted(generation.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        name = path.relative_to(generation).as_posix().encode()
        if path.is_symlink():
            target = path.resolve()
            interpreter_link = (
                path.parent == generation / "bin"
                and path.name.startswith("python")
                and target.is_file()
            )
            if not target.is_relative_to(generation) and not interpreter_link:
                raise ValueError(
                    "Prepared runtime links must not escape the generation"
                )
            content = os.readlink(path).encode()
            values = (b"link", name, content)
        elif path.is_file():
            values = (b"file", name)
        else:
            continue
        for value in values:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        if not path.is_symlink() or not target.is_relative_to(generation):
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
    return digest.hexdigest()


def _valid_key(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _verified_python(cache: Path, marker: dict) -> Path | None:
    try:
        key, name = marker["key"], marker["generation"]
        if not _valid_key(key) or not isinstance(name, str):
            return None
        if not name.startswith("env-") or Path(name).name != name:
            return None
        directory = cache / key
        generation = directory / name
        if directory.is_symlink() or generation.is_symlink():
            return None
        if not generation.is_dir() or not _valid_key(marker.get("content_digest")):
            return None
        if _digest_generation(generation) != marker["content_digest"]:
            return None
        python = generation / "bin" / "python"
        return python if _usable(python) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _ready_python(directory: Path, key: str) -> Path | None:
    try:
        path = directory / "ready.json"
        if path.is_symlink():
            return None
        marker = json.loads(path.read_text())
        if not isinstance(marker, dict) or marker.get("key") != key:
            return None
        return _verified_python(directory.parent, marker)
    except (OSError, ValueError, TypeError):
        return None


def _atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".selection-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _selection_path(root: Path) -> Path:
    installation = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    return _cache_root(root.resolve()) / f"selection-{installation}.json"


def _read_selection(path: Path) -> dict | None:
    if path.is_symlink():
        raise BootstrapError(
            "Unsafe runtime pin; inspect --runtime-info before explicitly refreshing."
        )
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text())
        if (
            not isinstance(marker, dict)
            or _verified_python(path.parent, marker) is None
        ):
            raise ValueError("unverified generation")
        return marker
    except (OSError, ValueError, TypeError) as exc:
        raise BootstrapError(
            "Runtime pin is stale or unsafe; stop and inspect --runtime-info. "
            "Use --runtime-refresh only to explicitly select the current package."
        ) from exc


def _selected_runtime(
    root: Path, *, candidate: bool = False
) -> tuple[Path, dict | None]:
    if not candidate:
        path = _selection_path(root)
        marker = _read_selection(path)
        if marker is not None:
            return path.parent / marker["key"] / marker[
                "generation"
            ] / "bin" / "python", marker
    return prepare_runtime(root), None


def select_runtime(root: Path, *, candidate: bool = False) -> Path:
    """Unpinned installs follow updates; explicit pins never silently fall forward."""
    return _selected_runtime(root, candidate=candidate)[0]


def pin_runtime(root: Path, key: str | None = None) -> dict:
    """Atomically select a verified prepared identity for future launches only."""
    import fcntl

    path = _selection_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if key is None:
            python = prepare_runtime(root)
            key = python.parents[2].name
        elif not _valid_key(key):
            raise BootstrapError(
                "A runtime pin must be a prepared 64-character content key, not an executable path."
            )
        directory = path.parent / key
        if _ready_python(directory, key) is None:
            raise BootstrapError(
                "Prepared identity is missing, stale or unsafe; no pin was changed."
            )
        marker = json.loads((directory / "ready.json").read_text())
        _atomic_json(path, marker)
        return marker


def unpin_runtime(root: Path) -> None:
    import fcntl

    path = _selection_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path.unlink(missing_ok=True)


def runtime_selection(root: Path) -> dict:
    """Inspect selection without preparing, changing state, or contacting providers."""
    path = _selection_path(root)
    try:
        pinned = _read_selection(path)
        status = "pinned" if pinned else "following_install"
        error = None
    except BootstrapError as exc:
        pinned, status, error = None, "blocked", str(exc)
    key = _content_key(root.resolve())
    ready = _ready_python(path.parent / key, key)
    return {
        "status": status,
        "selection_file": str(path),
        "pinned": pinned,
        "candidate": {
            "key": key,
            "prepared": ready is not None,
            "python": str(ready) if ready else None,
        },
        "error": error,
        "running_processes": "unchanged; selection applies only to future launches",
        "authentication": "not_checked",
    }


def prepare_runtime(root: Path) -> Path:
    """Return a validated frozen non-editable runtime, without changing cwd/auth.

    A per-content advisory lock serializes cold starts. Each attempt builds a new
    generation; neither failed preparation nor repair overwrites a live runtime.
    """
    root = root.resolve()
    if os.name != "posix":
        raise BootstrapError("Use macOS/Linux, or WSL with Linux-installed tools.")
    if sys.version_info < (3, 12):
        raise BootstrapError(
            "Python >=3.12 is required; use the documented isolated uv launcher."
        )
    uv = shutil.which("uv")
    if uv is None:
        raise BootstrapError(
            "uv is missing from PATH. Install it once: https://docs.astral.sh/uv/getting-started/installation/"
        )
    import fcntl

    try:
        key = _content_key(root)
        cache = _cache_root(root)
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = cache / key
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise BootstrapError(
                "Prepared runtime directory must not be a symbolic link."
            )
        descriptor = os.open(
            directory / "prepare.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ready = _ready_python(directory, key)
            if ready is not None:
                return ready
            generation = Path(tempfile.mkdtemp(prefix="env-", dir=directory))
            marker_path = None
            try:
                result = _run(
                    [
                        uv,
                        "sync",
                        "--project",
                        str(root),
                        "--frozen",
                        "--no-editable",
                        "--no-dev",
                        "--python",
                        sys.executable,
                        # uv's default local-wheel key does not include all source
                        # and resource bytes. Rebuild this package on each cold key.
                        "--reinstall-package",
                        "omp-tandem",
                    ],
                    env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(generation)},
                )
                if result.returncode:
                    raise BootstrapError(
                        f"uv preparation failed (exit {result.returncode}); inspect stderr and retry --prepare."
                    )
                python = generation / "bin" / "python"
                if not _usable(python):
                    raise BootstrapError(
                        "Prepared runtime cannot import omp_tandem.cli; no ready marker was published."
                    )
                # Do not publish a runtime assembled while its inputs changed.
                if _content_key(root) != key:
                    raise BootstrapError(
                        "Package changed during preparation; retry with a stable checkout."
                    )
                fd, marker_name = tempfile.mkstemp(prefix=".ready-", dir=directory)
                marker_path = Path(marker_name)
                with os.fdopen(fd, "w") as marker:
                    json.dump(
                        {
                            "key": key,
                            "generation": generation.name,
                            "content_digest": _digest_generation(generation),
                        },
                        marker,
                    )
                    marker.flush()
                    os.fsync(marker.fileno())
                marker_path.replace(directory / "ready.json")
                return python
            except BaseException:
                if marker_path is not None:
                    marker_path.unlink(missing_ok=True)
                shutil.rmtree(generation, ignore_errors=True)
                raise
    except BootstrapError:
        raise
    except (OSError, ValueError) as exc:
        raise BootstrapError(f"Could not prepare runtime: {exc}") from exc


def main(root: Path, argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--doctor"]:
        report = doctor(root)
        print(json.dumps(report))
        return 2 if report["missing"] else 0
    try:
        if arguments == ["--runtime-info"]:
            report = runtime_selection(root)
            print(json.dumps(report))
            return 1 if report["status"] == "blocked" else 0
        if arguments == ["--runtime-unpin"]:
            unpin_runtime(root)
            print(json.dumps(runtime_selection(root)))
            return 0
        if arguments == ["--runtime-refresh"] or arguments[:1] == ["--runtime-pin"]:
            if arguments[0] == "--runtime-pin" and len(arguments) != 2:
                raise BootstrapError("Usage: --runtime-pin PREPARED_CONTENT_KEY")
            marker = pin_runtime(root, arguments[1] if len(arguments) == 2 else None)
            print(
                json.dumps(
                    {"status": "pinned", **marker, "running_processes": "unchanged"}
                )
            )
            return 0
        candidate = "--candidate" in arguments
        python, pinned = _selected_runtime(
            root, candidate=candidate or arguments == ["--prepare"]
        )
        if arguments == ["--prepare"]:
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "python": str(python),
                        "key": python.parents[2].name,
                        "authentication": "not_checked",
                    }
                )
            )
            return 0
        # Selection provenance is a startup observation, never a refresh signal.
        os.environ["OMP_TANDEM_RUNTIME_SELECTION"] = json.dumps(
            {
                "mode": "candidate" if candidate else "installed",
                "key": python.parents[2].name,
                "generation": python.parents[1].name,
                "selection_file": str(_selection_path(root)),
                "pinned": pinned,
                "candidate_key": _content_key(root.resolve()),
            }
        )
        # execv retains cwd, stdio and the provider environment. Candidate state is
        # selected inside the installed CLI, before any database can be opened.
        os.execv(str(python), [str(python), "-I", "-m", "omp_tandem", *arguments])
    except (BootstrapError, OSError) as exc:
        print(f"omp-tandem: {exc}", file=sys.stderr)
        return 1
    return 0
