"""What an unresolved question still needs, projected where a caller can act on it.

A clarification that cannot be answered is recorded and the stage continues, so by
the time the caller reads a terminal result the request for missing material has
already left the live `question` field. The caller then sees only that the outcome
was partial, which does not say what to change, and an unchanged retry buys another
assessment of the same gap. This module reads those saved rows back and says what
each one requires.

It projects stored records. It never changes a question's state, rewrites a saved
report, authorizes the paths a reviewer asked for, or treats the recorded list as
proof that nothing else is missing.
"""

from __future__ import annotations

import json

# Both states are terminal for the question. The first cannot be repaired inside
# the same immutable snapshot at all; the second can be answered somewhere else.
BLOCKED_STATES = ("context_required", "expired", "decision_unresolved")
_MAX_ITEMS = 256

NEW_CAPTURE_STEP = (
    "Create a new immutable capture containing the required material and start a "
    "new attempt. Do not reply to this question; free text cannot enter this "
    "independent stage."
)
RESOLVE_STEP = (
    "Obtain the missing answer or authoritative decision, then use an explicitly "
    "authorized new task or attempt. This closed question cannot be answered or "
    "reopened; do not automatically restart the run."
)


def _strings(value) -> tuple[list[str], bool]:
    """Correctly typed items and whether the field itself was well formed."""
    if value is None:
        return [], True
    if (
        not isinstance(value, list)
        or len(value) > _MAX_ITEMS
        or any(not isinstance(item, str) for item in value)
    ):
        return [], False
    return list(dict.fromkeys(value)), True


def extract_request(context) -> dict:
    """Read a stored question context without inventing what it does not say.

    `parse_status` carries the difference an empty list cannot: material that was
    never requested, a context this version does not recognise, and one that is not
    JSON at all are three different situations for the caller.
    """
    if context is None or (isinstance(context, str) and not context.strip()):
        return {
            "parse_status": "absent",
            "requested_paths": [],
            "requested_material": [],
            "reason": None,
        }
    try:
        parsed = json.loads(context)
    except (TypeError, ValueError):
        return {
            "parse_status": "invalid_json",
            "requested_paths": [],
            "requested_material": [],
            "reason": None,
        }
    if not isinstance(parsed, dict):
        return {
            "parse_status": "unrecognized",
            "requested_paths": [],
            "requested_material": [],
            "reason": None,
        }
    paths, paths_typed = _strings(parsed.get("requested_paths"))
    material, material_typed = _strings(parsed.get("requested_material"))
    reason = parsed.get("reason")
    reason_typed = reason is None or isinstance(reason, str)
    recognized = any(
        key in parsed for key in ("requested_paths", "requested_material", "reason")
    )
    # One malformed member does not discard the members beside it, but it does
    # stop this context being reported as understood.
    return {
        "parse_status": "parsed"
        if recognized and paths_typed and material_typed and reason_typed
        else "unrecognized",
        "requested_paths": paths,
        "requested_material": material,
        "reason": reason if reason_typed else None,
    }


def recovery(kind: str, stage: str | None) -> tuple[bool, str, str]:
    """A closed question is not by itself evidence that the snapshot must change.

    Only an independent stage, and the state that names a missing capture, cannot
    be repaired without a new one. An expired comparison question or an unresolved
    decision is answered elsewhere and then brought to a new authorized attempt.
    """
    if kind == "context_required" or stage == "independent":
        return True, "new_capture", NEW_CAPTURE_STEP
    return False, "resolve_then_new_task_or_attempt", RESOLVE_STEP


def blocked_question(row, stage: str | None) -> dict:
    """One saved record, with the exact stored text retained beside what was read."""
    kind = row["state"]
    context = row["context"]
    request = extract_request(context)
    requires_capture, action, next_step = recovery(kind, stage)
    return {
        "question_id": row["question_id"],
        "task_id": row["task_id"],
        "stage": stage,
        "kind": kind,
        "question": row["question"],
        "context": context,
        "requested_paths": request["requested_paths"],
        "requested_material": request["requested_material"],
        # A context without its own reason is explained by the question itself;
        # nothing here is generated to fill the field.
        "reason": request["reason"] or row["question"],
        "parse_status": request["parse_status"],
        "requires_new_capture": requires_capture,
        "reply_allowed": False,
        "recovery_action": action,
        "next_step": next_step,
    }


def blocked_envelope(items: list[dict]) -> dict | None:
    """None when nothing is outstanding; the caller distinguishes that from empty."""
    if not items:
        return None
    return {
        "count": len(items),
        "kinds": sorted({item["kind"] for item in items}),
        "requires_new_capture": any(item["requires_new_capture"] for item in items),
        "reply_allowed": False,
        "items": items,
        "truncated": False,
    }


def task_blockers(db, task_ids) -> dict | None:
    """Project every closed-but-unresolved question of the named tasks."""
    identifiers = [task_id for task_id in task_ids if task_id]
    if not identifiers:
        return None
    rows = db.execute(
        "SELECT q.question_id, q.task_id, q.question, q.context, q.state, "
        "t.review_stage FROM questions q JOIN tasks t ON t.task_id=q.task_id "
        f"WHERE q.task_id IN ({','.join('?' for _ in identifiers)}) "
        f"AND q.state IN ({','.join('?' for _ in BLOCKED_STATES)}) "
        "ORDER BY q.created",
        (*identifiers, *BLOCKED_STATES),
    )
    return blocked_envelope(
        [blocked_question(row, row["review_stage"]) for row in rows][:_MAX_ITEMS]
    )
