"""Startup identity and digests of schemas registered by this running process."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from .models import outcome_schema
from .work_items import WorkCommand


def schema_digest(schema: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _distribution_identity(distribution, module_path):
    if distribution is None:
        return "unknown", None, "unknown"
    origin = str(distribution.locate_file(""))
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            return "editable", origin, "unknown"
        record = distribution.read_text("RECORD")
        wheel = distribution.read_text("WHEEL")
        if not record or not wheel:
            return "unpinned", origin, "unknown"
        sources = {
            Path(distribution.locate_file(item)).resolve(): item
            for item in distribution.files or ()
        }
        package = module_path.parent
        actual = {
            path.resolve()
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        metadata_paths = {
            path
            for path in sources
            if path.parent.name.endswith(".dist-info")
            and path.name in {"METADATA", "WHEEL"}
        }
        if {path.name for path in metadata_paths} != {"METADATA", "WHEEL"}:
            return "unpinned", origin, "unknown"
        actual |= {
            path
            for path in sources
            if path.suffix != ".pyc"
            and not (path.name == "RECORD" and path.parent.name.endswith(".dist-info"))
        }
        if module_path not in sources or not actual.issubset(sources):
            return "unpinned", origin, "unknown"
        for path in actual:
            expected = sources[path].hash
            if not expected or expected.mode != "sha256":
                return "unpinned", origin, "unknown"
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest())
                .decode()
                .rstrip("=")
            )
            if digest != expected.value:
                return "unpinned", origin, "unknown"
        return (
            "installed_wheel",
            origin,
            "sha256:" + hashlib.sha256(record.encode()).hexdigest(),
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return "unpinned", origin, "unknown"


def _checkout_at_start(module_path):
    root = next(
        (
            parent
            for parent in module_path.parents
            if (parent / ".git").exists()
            and any(
                module_path.is_relative_to(parent / source)
                for source in ("src/omp_tandem", "omp_tandem")
            )
        ),
        None,
    )
    if root is None:
        return None
    result = {
        "root": str(root),
        "source": "startup_git_observation",
        "head": None,
        "dirty": None,
    }
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode == 0:
            result["head"] = head.stdout.strip()
        if status.returncode == 0:
            result["dirty"] = bool(status.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


@dataclass(frozen=True)
class RuntimeIdentity:
    package_version: str
    distribution_origin: str
    distribution_path: str | None
    exact_build: str
    loaded_module_path: str
    captured_at: float
    source_checkout_json: str
    work_protocol: str = "work-v2-presentation"
    review_protocol: str = "independent-first-v1"

    @classmethod
    def capture(cls, *, module_path=None, distribution=None):
        """Capture at startup, never derive loaded identity on a progress read."""
        module_path = Path(module_path or __file__).resolve()
        if distribution is None:
            try:
                distribution = metadata.distribution("omp-tandem")
            except metadata.PackageNotFoundError:
                distribution = None
        origin, path, exact = _distribution_identity(distribution, module_path)
        return cls(
            package_version=distribution.version if distribution else "unknown",
            distribution_origin=origin,
            distribution_path=path,
            exact_build=exact,
            loaded_module_path=str(module_path),
            captured_at=time.time(),
            source_checkout_json=json.dumps(_checkout_at_start(module_path)),
        )

    def view(self):
        result = asdict(self)
        result["source_checkout"] = json.loads(result.pop("source_checkout_json"))
        result["identity_source"] = "process_start_importlib.metadata"
        result["exact_build_source"] = (
            "verified_distribution_record"
            if self.exact_build != "unknown"
            else "unknown"
        )
        return result


# Import occurs at server/worker initialization, before requests. Registration may
# add observed surfaces, but never refreshes the captured package/checkout identity.
_CAPTURED = RuntimeIdentity.capture()
_CANONICAL = {
    "tandem_finish": schema_digest(outcome_schema()),
    "tandem_work": schema_digest(WorkCommand.model_json_schema()),
}
_SURFACES: dict[str, dict[str, str]] = {}
_GUARD = threading.Lock()


def register_schemas(surface: str, schemas: dict[str, dict]):
    digests = {name: schema_digest(schema) for name, schema in schemas.items()}
    with _GUARD:
        _SURFACES[surface] = {**_SURFACES.get(surface, {}), **digests}


def runtime_identity():
    with _GUARD:
        schemas = {surface: dict(values) for surface, values in _SURFACES.items()}
    return {
        **_CAPTURED.view(),
        "schema_digests": {
            **_CANONICAL,
            "surfaces": schemas,
            "source": "registered_tool_parameters",
            "compatibility": "not_assessed",
        },
    }
