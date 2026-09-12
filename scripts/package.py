"""Build an explicit source/plugin ZIP; never sweep runtime or client state into it."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".gitignore",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "README.md",
    "README.ru.md",
    "README.zh-CN.md",
    "pyproject.toml",
    "uv.lock",
    "server.py",
    "plugin.json",
    "mcp.json",
)
TREES = {
    "src/omp_tandem": {".py", ".yml"},
    "tests": {".py"},
    "scripts": {".py", ".sh"},
    "skills": {".md"},
    "config": {".json"},
    "docs": {".md", ".json"},
    ".claude-plugin": {".json"},
    ".agents/plugins": {".json"},
    ".github": {".yml", ".md"},
}


def source_payloads(root: Path) -> dict[str, bytes]:
    selected = [root / name for name in ROOT_FILES]
    for name, suffixes in TREES.items():
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"Missing or unsafe source directory: {name}")
        for path in directory.rglob("*"):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError("Package source must not contain symbolic links")
            if path.is_file() and path.suffix in suffixes:
                selected.append(path)
    payloads = {}
    for path in sorted(selected):
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            raise ValueError(
                f"Missing or unsafe package input: {path.relative_to(root)}"
            )
        payloads[path.relative_to(root).as_posix()] = path.read_bytes()
    return payloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the existing manifest without writing files",
    )
    args = parser.parse_args(argv)
    root = ROOT.resolve()
    payloads = source_payloads(root)
    version = tomllib.loads(payloads["pyproject.toml"].decode())["project"]["version"]
    for name in ("plugin.json", ".claude-plugin/plugin.json"):
        metadata = json.loads(payloads[name])
        if metadata["name"] != "omp-tandem" or metadata["version"] != version:
            raise ValueError(f"Plugin identity/version mismatch: {name}")
    marketplace = json.loads(payloads[".claude-plugin/marketplace.json"])
    if (
        marketplace["version"] != version
        or marketplace["plugins"][0]["version"] != version
    ):
        raise ValueError("Marketplace plugin version does not match the package")
    portable = json.loads(payloads[".agents/plugins/marketplace.json"])
    if portable["plugins"][0]["name"] != "omp-tandem":
        raise ValueError("Portable marketplace plugin identity does not match")
    manifest = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in payloads.items()
    )
    if args.check:
        if (root / "SHA256SUMS").read_text() != manifest:
            raise ValueError(
                "SHA256SUMS does not match the current distribution inputs"
            )
        print(
            json.dumps(
                {"status": "verified", "files": len(payloads), "version": version}
            )
        )
        return
    (root / "SHA256SUMS").write_text(manifest)
    payloads["SHA256SUMS"] = manifest.encode()
    args.output.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to build the wheel")
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(args.output.resolve())],
        cwd=root,
        check=True,
        timeout=120,
    )
    wheel_path = args.output / f"omp_tandem-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path) as wheel:
        if wheel.testzip() is not None:
            raise ValueError("Wheel integrity check failed")
        for name, content in payloads.items():
            if (
                name.startswith("src/omp_tandem/")
                and wheel.read(name.removeprefix("src/")) != content
            ):
                raise ValueError(f"Wheel source content check failed: {name}")
    archive_path = args.output / f"omp-tandem-{version}.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, content in payloads.items():
            entry = zipfile.ZipInfo(
                "omp-tandem/" + name, date_time=(1980, 1, 1, 0, 0, 0)
            )
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = (
                stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)
            ) << 16
            archive.writestr(entry, content)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        for name, content in payloads.items():
            if archive.read("omp-tandem/" + name) != content:
                raise ValueError("Archive content check failed")
    artifact_manifest = args.output / "SHA256SUMS"
    artifact_manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (wheel_path, archive_path)
        )
    )
    print(
        json.dumps(
            {
                "status": "packaged",
                "version": version,
                "files": len(payloads),
                "archive": str(archive_path),
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "wheel": str(wheel_path),
                "wheel_sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
                "artifact_manifest": str(artifact_manifest),
            }
        )
    )


if __name__ == "__main__":
    main()
