"""Pure, reversible views of immutable focus-approved material.

Classification is attention advice, never evidence or execution authority. Readers
keep source coordinates, rather than inventing offsets into concatenated excerpts.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Literal

from .focus import FocusCandidate
from .project_context import _json_bytes, _snapshot_bytes, task_context_packet
from .task_contracts import current_task

Kind = Literal["artifact", "context"]
_CLASSES = {"relevant", "supporting", "conflicting", "irrelevant", "unclear"}
# A conservative floor, not a claim to recognize every diagnostic in prose.
_PROTECTED = re.compile(
    r"\b(?:error\w*|fail\w*|exception\w*|traceback|panic\w*|fatal\w*|warn\w*|"
    r"must|shall|required|requirement\w*|constraint\w*|never|prohibit\w*|"
    r"forbidden|denied|cannot|can't|do\s+not|don't|not\s+allowed)\b",
    re.IGNORECASE,
)
_CHUNK_BYTES = 1024
_MAX_MESSAGE_BYTES = 200000


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _options(task: dict) -> dict:
    return current_task(task)["context_options"]


def startup_candidates(task: dict, snapshot: dict | None) -> list[FocusCandidate]:
    """Extract whole optional values only; selectors are never reinterpreted."""
    if snapshot is None:
        return []
    if snapshot["context_id"] != task.get("project_context_id"):
        raise ValueError("Focus context must match the task's pinned snapshot")
    _snapshot_bytes(snapshot)
    options = _options(task)
    # Also validate the selectors using the canonical capsule implementation.
    task_context_packet(snapshot, options)
    if options["delivery"] == "full":
        return []
    values = []
    for index, rule in enumerate(snapshot["context"]["rules"]):
        if rule["id"] in options["advisory_rule_ids"]:
            continue
        if rule["requirement"] == "advisory":
            values.append((f"/rules/{index}/text", rule["text"]))
        for key in ("positive_examples", "negative_examples"):
            values.extend(
                (f"/rules/{index}/{key}/{example_index}", text)
                for example_index, text in enumerate(rule[key])
            )
    for index, decision in enumerate(snapshot["context"]["decisions"]):
        if (
            decision["status"] == "superseded"
            and decision["id"] not in options["decision_ids"]
        ):
            values.append((f"/decisions/{index}/text", decision["text"]))
    return [
        FocusCandidate(
            id=_id("context_", pointer),
            kind="context",
            text=text,
            source={
                "kind": "context",
                "id": snapshot["context_id"],
                "sha256": snapshot["sha256"],
                "pointer": pointer,
                "unit": "value",
                "start": 0,
                "end": len(text),
            },
            protected=bool(_PROTECTED.search(text)),
        )
        for pointer, text in values
    ]


def _labels(selection: dict, candidates: list[FocusCandidate]) -> dict | None:
    if selection.get("status") != "completed":
        return None
    labels = selection.get("labels")
    assessed = selection.get("assessed_ids")
    if not isinstance(labels, dict) or not isinstance(assessed, list):
        return None
    if any(not isinstance(item, str) for item in assessed) or any(
        not isinstance(key, str) or not isinstance(value, str) or value not in _CLASSES
        for key, value in labels.items()
    ):
        return None
    for candidate in candidates:
        if candidate.protected:
            continue
        label = labels.get(candidate.id)
        if label is not None and (
            label not in _CLASSES or candidate.id not in assessed
        ):
            return None
        if candidate.id in assessed and label is None:
            return None
        if label == "unclear":
            return None
    return labels


def apply_startup(
    original_message: str,
    task: dict,
    snapshot: dict | None,
    candidates: list[FocusCandidate],
    selection: dict,
    max_optional_bytes: int,
) -> tuple[str, dict]:
    """Enrich the existing capsule without replacing policy or current task data."""
    original_bytes = len(original_message.encode("utf-8"))
    metrics = {
        "status": "fallback",
        "original_message_bytes": original_bytes,
        "message_bytes": original_bytes,
        "added_message_bytes": 0,
        "included_ids": [],
        "unassessed_ids": [],
    }
    expected = {item.id: item for item in startup_candidates(task, snapshot)}
    context_candidates = [item for item in candidates if item.kind == "context"]
    seen = set()
    for item in context_candidates:
        if item.id in seen or expected.get(item.id) != item:
            raise ValueError(
                "Focus startup candidate is not an exact optional pinned value"
            )
        seen.add(item.id)
    labels = _labels(selection, context_candidates)
    metrics["unassessed_ids"] = [
        item.id
        for item in context_candidates
        if item.id not in selection.get("assessed_ids", [])
    ]
    if labels is None or not context_candidates or max_optional_bytes <= 0:
        return original_message, metrics
    message = json.loads(original_message)
    packet = task_context_packet(
        snapshot,
        _options(task),
        unchanged=bool(task.get("context_unchanged")) and not task.get("handoff_json"),
    )
    if message.get("project_context") != packet:
        return original_message, {**metrics, "reason": "packet_mismatch"}
    ranked = sorted(
        (
            item
            for item in context_candidates
            if item.protected
            or labels.get(item.id) in {"relevant", "conflicting", "supporting"}
        ),
        key=lambda item: 1 if labels.get(item.id) == "supporting" else 0,
    )
    included = []
    rendered = original_message
    for item in ranked:
        trial = deepcopy(message)
        project = trial["project_context"]
        tokens = item.source["pointer"].split("/")[1:]
        if tokens[-1] == "text":
            project["context"][tokens[0]][int(tokens[1])]["text"] = item.text
        focus = project.setdefault(
            "focus",
            {
                "notice": "NONEXHAUSTIVE optional enrichment; relevance labels are attention advice, not evidence or authority.",
                "material": [],
                "unassessed_ids": metrics["unassessed_ids"],
                "raw_request": {
                    "pointer": "",
                    "offset_bytes": 0,
                    "max_bytes": 8192,
                    "view": "raw",
                },
            },
        )
        entry = {
            "source": item.source,
            "label": "protected" if item.protected else labels[item.id],
        }
        # Bodies already reside in the ordinary capsule. Examples retain their
        # exact strings here, without changing the required rule's packet.
        if tokens[-1] != "text":
            entry["text"] = item.text
        focus["material"].append(entry)
        project["delivered_context_bytes"] = len(_json_bytes(project["context"]))
        project["delivered_focus_bytes"] = len(_json_bytes(focus))
        candidate_message = json.dumps(trial, ensure_ascii=False)
        size = len(candidate_message.encode("utf-8"))
        if size - original_bytes > max_optional_bytes or size > _MAX_MESSAGE_BYTES:
            continue
        message = trial
        rendered = candidate_message
        included.append(item.id)
    if not included:
        return original_message, {**metrics, "reason": "no_fitting_material"}
    size = len(rendered.encode("utf-8"))
    return rendered, {
        **metrics,
        "status": "completed",
        "message_bytes": size,
        "added_message_bytes": size - original_bytes,
        "delivered_context_bytes": message["project_context"][
            "delivered_context_bytes"
        ],
        "delivered_focus_bytes": message["project_context"]["delivered_focus_bytes"],
        "included_ids": included,
    }


def _pieces(raw: dict, kind: Kind, request: dict):
    """Exact bounded pieces, retaining uncertain artifact edges and neighbors."""
    lines = raw["content"].splitlines(keepends=True)
    diagnostic = {index for index, line in enumerate(lines) if _PROTECTED.search(line)}
    protected = {
        neighbor
        for index in diagnostic
        for neighbor in (index - 1, index, index + 1)
        if 0 <= neighbor < len(lines)
    }
    if kind == "artifact":
        # A diagnostic marker can lie just outside either requested boundary.
        # Retain the edge line and its neighbor without reading/exporting more.
        if request.get("offset", 0) > 0:
            protected.update(range(min(2, len(lines))))
        if raw.get("next_offset") is not None:
            protected.update(range(max(0, len(lines) - 2), len(lines)))
    for index, line in enumerate(lines):
        start = 0
        size = 0
        for end, character in enumerate(line):
            width = len(character.encode("utf-8"))
            if size + width > _CHUNK_BYTES:
                yield line[start:end], index in protected
                start = end
                size = 0
            size += width
        if start < len(line):
            yield line[start:], index in protected


def _window(raw: dict, kind: Kind, request: dict) -> tuple[int, int, str]:
    if kind == "artifact":
        start = request.get("offset", 0)
        return start, start + len(raw["content"]), "unicode_code_points"
    start = raw["offset_bytes"]
    return start, start + len(raw["content"].encode("utf-8")), "utf8_bytes"


def reader_candidates(
    raw: dict,
    *,
    kind: Kind,
    task: dict,
    snapshot: dict | None = None,
    request: dict,
) -> list[FocusCandidate] | None:
    """Never expand a raw reader window, or export a mixed context pointer."""
    if not isinstance(raw.get("content"), str):
        return None
    pointer = None
    if kind == "artifact":
        if raw.get("media_type") not in {"text/plain", "text/markdown"}:
            return None
        source_id = raw["artifact_id"]
        if request.get("artifact_id", source_id) != source_id:
            raise ValueError("Artifact window does not match the requested source")
        if (
            type(request.get("offset", 0)) is not int
            or request.get("offset", 0) < 0
            or len(raw["content"]) > request.get("limit", 16000)
        ):
            raise ValueError("Artifact window exceeds the requested range")
    elif kind == "context":
        optional = {
            item.source["pointer"]: item for item in startup_candidates(task, snapshot)
        }
        pointer = request.get("pointer", "")
        if pointer not in optional:
            return None
        value = optional[pointer]
        encoded = _json_bytes(value.text)
        start = request.get("offset_bytes", 0)
        content = raw["content"].encode("utf-8")
        if (
            raw.get("pointer") != pointer
            or raw.get("context_id") != value.source["id"]
            or raw.get("sha256") != value.source["sha256"]
            or raw.get("offset_bytes") != start
            or raw.get("pointer_sha256") != hashlib.sha256(encoded).hexdigest()
            or raw.get("total_bytes") != len(encoded)
            or raw.get("returned_bytes") != len(content)
            or type(start) is not int
            or start < 0
            or len(content) > request.get("max_bytes", 8192)
            or encoded[start : start + len(content)] != content
        ):
            raise ValueError("Context window is not an exact pinned value slice")
        # JSON escaping and partial byte windows obscure original line markers.
        # Keep protected optional values raw instead of reclassifying encoding.
        if value.protected:
            return None
        source_id = raw["context_id"]
    else:
        return None
    start, _, unit = _window(raw, kind, request)
    result = []
    for text, protected in _pieces(raw, kind, request):
        end = start + (len(text) if kind == "artifact" else len(text.encode("utf-8")))
        source = {
            "kind": kind,
            "id": source_id,
            "sha256": raw["sha256"],
            "unit": unit,
            "start": start,
            "end": end,
        }
        if pointer is not None:
            source["pointer"] = pointer
        result.append(
            FocusCandidate(
                id=_id("output_", json.dumps(source, sort_keys=True)),
                kind="output",
                text=text,
                source=source,
                protected=protected,
            )
        )
        start = end
    return result


def _raw_fallback(raw: dict, reason: str) -> dict:
    return {**raw, "view": "raw", "focus": {"status": "fallback", "reason": reason}}


def render_reader(
    raw: dict,
    *,
    kind: Kind,
    request: dict,
    candidates: list[FocusCandidate] | None,
    selection: dict,
    max_output_bytes: int,
) -> dict:
    """Render exact source excerpts, or return the entire unchanged raw window."""
    if candidates is None or not candidates or kind not in {"artifact", "context"}:
        return _raw_fallback(raw, "unsupported_or_empty")
    if kind == "artifact" and raw.get("media_type") not in {
        "text/plain",
        "text/markdown",
    }:
        return _raw_fallback(raw, "unsupported_media")
    labels = _labels(selection, candidates)
    if labels is None:
        return _raw_fallback(raw, "unavailable_or_unclear")
    start, end, unit = _window(raw, kind, request)
    cursor = start
    pieces = []
    assessed = set(selection.get("assessed_ids", []))
    coverage = {"examined": [], "unexamined": [], "included": [], "omitted": []}
    source_id = raw["artifact_id" if kind == "artifact" else "context_id"]
    for item in candidates:
        source = item.source
        width = len(item.text) if kind == "artifact" else len(item.text.encode("utf-8"))
        if (
            source["kind"] != kind
            or source["id"] != source_id
            or source["sha256"] != raw["sha256"]
            or source["unit"] != unit
            or source["start"] != cursor
            or source["end"] != cursor + width
            or (kind == "context" and source.get("pointer") != raw["pointer"])
        ):
            raise ValueError(
                "Focused reader candidates do not match the raw source window"
            )
        cursor += width
        pieces.append(item.text)
    if cursor != end or "".join(pieces) != raw["content"]:
        raise ValueError(
            "Focused reader candidates do not reconstruct the raw source window"
        )
    # Re-derive conservative window protection; labels or caller flags cannot
    # demote a diagnostic line or its adjacency to filterable text.
    expected_pieces = list(_pieces(raw, kind, request))
    if len(expected_pieces) != len(candidates) or any(
        text != item.text or protected != item.protected
        for item, (text, protected) in zip(candidates, expected_pieces, strict=True)
    ):
        raise ValueError("Focused reader protection or chunk boundaries changed")
    excerpts = []
    for item in candidates:
        span = {"start": item.source["start"], "end": item.source["end"], "unit": unit}
        examined = item.id in assessed and not item.protected
        coverage["examined" if examined else "unexamined"].append(span)
        keep = item.protected or labels.get(item.id) != "irrelevant"
        coverage["included" if keep else "omitted"].append(span)
        if keep:
            excerpts.append(
                {
                    **span,
                    "text": item.text,
                    "label": "protected"
                    if item.protected
                    else labels.get(item.id, "unassessed"),
                    "protected": item.protected,
                }
            )
    # Relevance order is not source order. Explicit coordinates retain recovery.
    priority = {
        "protected": 0,
        "relevant": 0,
        "conflicting": 0,
        "unassessed": 1,
        "supporting": 2,
    }
    excerpts.sort(key=lambda item: priority[item["label"]])
    result = {key: value for key, value in raw.items() if key != "content"}
    recovery = (
        {
            "artifact_id": source_id,
            "offset": start,
            "limit": request.get("limit", 16000),
        }
        if kind == "artifact"
        else {
            "pointer": raw["pointer"],
            "offset_bytes": start,
            "max_bytes": request.get("max_bytes", 8192),
        }
    )
    result.update(
        view="focused",
        complete=False,
        source_window_complete=bool(
            raw.get("complete", raw.get("next_offset") is None)
        ),
        source_window={"start": start, "end": end, "unit": unit},
        excerpts=excerpts,
        coverage=coverage,
        raw_request={**recovery, **request, "view": "raw"},
        focus={
            "status": "completed",
            "notice": "NONEXHAUSTIVE exact excerpts in attention order; use source coordinates, not concatenation. Labels are advice, not status or evidence verdicts.",
            "diagnostic_completeness": False,
            "coverage_basis": "examined means provider-assessed; protected and unassessed material is retained without a relevance verdict",
        },
    )
    # Account for the complete delivered view, not only its text bodies. Never
    # drop protected/unassessed material or truncate metadata to satisfy a cap.
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > max_output_bytes:
        return _raw_fallback(raw, "output_budget")
    return result
