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


def _ready_python(directory: Path, key: str) -> Path | None:
    try:
        marker = json.loads((directory / "ready.json").read_text())
        generation = marker["generation"]
        if marker["key"] != key or not isinstance(generation, str):
            return None
        # Marker values are never accepted as arbitrary executable paths.
        if not generation.startswith("env-") or Path(generation).name != generation:
            return None
        python = directory / generation / "bin" / "python"
        return python if _usable(python) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


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
        with (directory / "prepare.lock").open("a") as lock:
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
                    json.dump({"key": key, "generation": generation.name}, marker)
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
        python = prepare_runtime(root)
        if arguments == ["--prepare"]:
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "python": str(python),
                        "authentication": "not_checked",
                    }
                )
            )
            return 0
        # execv retains cwd and the complete provider environment. Isolated mode
        # prevents a caller's local omp_tandem.py/PYTHONPATH from replacing us.
        os.execv(str(python), [str(python), "-I", "-m", "omp_tandem", *arguments])
    except (BootstrapError, OSError) as exc:
        print(f"omp-tandem: {exc}", file=sys.stderr)
        return 1
    return 0
