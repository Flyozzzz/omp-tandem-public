"""Presentation-only focus over the native worker's already permitted callbacks."""

import hashlib
import math
import re
import subprocess

from .focus import FocusCandidate
from .jev_client import canonical_json

CORE_ESSENTIAL = frozenset(
    {
        "tandem_finish",
        "tandem_ask",
        "tandem_read_artifact",
        "tandem_context_read",
        "tandem_review_read",
    }
)
OPTIONAL_TOOLS = frozenset({"tandem_work", "tandem_publish_artifact"})
# This is a compatibility gate, not a minimum-version guess. New runtimes require
# the credential-free native transport check before joining this set.
DISCOVERY_VERSIONS = frozenset({"18.1.13"})


def discovery_capability(executable, *, native_tools, timeout_seconds=3.0):
    """Check the public runtime version before any prompt or classification.

    The pinned runtime supplies an xd-only write transport when native read is
    permitted. We never add filesystem write to the native allowlist. Failure
    keeps the ordinary essential callback registration, without retrying a turn.
    """
    fallback = {"supported": False, "reason": "runtime_unavailable", "version": None}
    if "read" not in (native_tools or ()):
        return {**fallback, "reason": "no_native_read"}
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        return {**fallback, "reason": "no_probe_time"}
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=min(timeout_seconds, 3.0),
            check=True,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return fallback
    match = re.fullmatch(r"(?:omp(?:/|\s+))?(\d+\.\d+\.\d+)", result.stdout.strip())
    version = match.group(1) if match else None
    supported = version in DISCOVERY_VERSIONS
    return {
        "supported": supported,
        "reason": None if supported else "unsupported_runtime",
        "version": version,
    }


def _candidate(tool):
    descriptor = canonical_json(
        {"description": tool.description, "parameters": tool.parameters}
    )
    return FocusCandidate(
        id="tool_" + tool.name,
        kind="tool",
        text=tool.description,
        source={
            "kind": "tool",
            "id": tool.name,
            "sha256": hashlib.sha256(descriptor.encode("utf-8")).hexdigest(),
            "unit": "value",
            "start": 0,
            "end": len(tool.description),
        },
    )


def optional_tool_candidates(host_tools, *, enabled, task, native_tools):
    """Derive descriptions only from the trusted, already allowed host catalog."""
    if (
        not enabled
        or task.get("mode") not in {"analyze", "work"}
        or "read" not in (native_tools or ())
        or task.get("review_id")
        or task.get("review_stage")
        or task.get("native_attempt")
        or task.get("managed")
    ):
        return []
    return [_candidate(tool) for tool in host_tools if tool.name in OPTIONAL_TOOLS]


def tool_presentation(host_tools, candidates, selection, *, can_discover):
    """Return load modes only; retain every callback, schema and original order.

    Unknown, unassessed, protected and stale candidate identities cannot hide a
    schema. This function never registers tools or changes native permissions.
    """
    modes = {tool.name: "essential" for tool in host_tools}
    reason = None
    if not can_discover:
        reason = "discovery_unavailable"
    elif not isinstance(selection, dict) or selection.get("status") != "completed":
        reason = "selection_unavailable"
    else:
        labels = selection.get("labels")
        assessed = selection.get("assessed_ids")
        if not isinstance(labels, dict) or not isinstance(assessed, list):
            reason = "selection_unavailable"
        else:
            allowed = {
                tool.name: _candidate(tool)
                for tool in host_tools
                if tool.name in OPTIONAL_TOOLS
            }
            for candidate in candidates:
                if not isinstance(candidate, FocusCandidate) or candidate.protected:
                    continue
                expected = allowed.get(candidate.source.get("id"))
                if (
                    expected is not None
                    and candidate == expected
                    and candidate.id in assessed
                    and labels.get(candidate.id) in ("supporting", "irrelevant")
                ):
                    modes[candidate.source["id"]] = "discoverable"
    deferred = [name for name, mode in modes.items() if mode == "discoverable"]
    return {
        "load_modes": modes,
        "metadata": {
            "status": "focused" if deferred else "essential",
            "reason": reason,
            "essential": [name for name, mode in modes.items() if mode == "essential"],
            "discoverable": deferred,
            "permissions_changed": False,
        },
    }
