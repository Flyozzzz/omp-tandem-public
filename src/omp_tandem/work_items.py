"""Versioned shared work, independent acceptance, and fenced execution authority.

Public cards and event snapshots never contain worker credentials. All transitions,
including scheduler reservations, serialize in the launch project's SQLite database.
Acceptance is an attributed attestation, not a claim that this store ran checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import shlex
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .models import TaskRequirements, VerificationPlan
from .review_check_state import (
    ReviewCheckState,
    authorization_policy,
    declaration_revision,
    public_policy,
    selected_policy,
    valid_container,
)
from .task_store import initialize_database
from .verification import verification_requirements
from .workspace import ProjectScope

_NonBlank = Annotated[str, StringConstraints(pattern=r"\S", max_length=16000)]
_Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
]
_Actor = Literal["claude", "omp"]
_ACTIVE = {"reserved", "running"}
_TERMINAL = {"completed", "cancelled"}

# Historical managed Claude policy, now visible in CLI help and authorization.
CLAUDE_DEFAULT_MODEL = "sonnet"


def validate_model(model: str) -> str:
    """Validate a CLI model identifier without inventing a provider catalogue."""
    if (
        not isinstance(model, str)
        or not model
        or model.startswith("-")
        or any(
            character.isspace() or not character.isprintable() for character in model
        )
    ):
        raise ValueError(
            "Model must be a nonempty identifier without whitespace or control characters"
        )
    return model


# Default share of the grant one attempt may reserve when the operator sets no
# explicit ceiling: half of the grant, matching the supervisor's default of two
# parallel attempts. Deliberately independent of max_launches.
ATTEMPT_SHARE_DEFAULT = 2


def shell_permission(record: dict) -> bool:
    """Canonical shell permission; legacy records only carry allow_tests."""
    if "allow_shell" in record:
        return record["allow_shell"] is True
    return record.get("allow_tests") is True


def attempt_budget(grant: dict) -> dict:
    """Effective per-attempt ceiling with its provenance; legacy grants keep theirs."""
    if "max_attempt_cost_usd" in grant:
        return {
            "max_attempt_cost_usd": grant["max_attempt_cost_usd"],
            "attempt_cost_policy": grant.get("attempt_cost_policy", "explicit"),
        }
    return {
        "max_attempt_cost_usd": grant["max_cost_usd"] / grant["max_launches"],
        "attempt_cost_policy": "legacy_launch_share",
    }


def model_policy(grant: dict) -> dict:
    """How each seat's model is chosen: a fixed selector or OMP's dynamic default.

    A fixed selector is the operator's requested selector, not proof of the
    provider/model implementation that actually answers. The dynamic default is
    OMP's own configured selection, evaluated when each attempt starts.
    """
    try:
        selection = model_selection(grant)
    except ValueError as error:
        # A malformed record is labelled, never fatal for a read; launch refuses it.
        return {
            "claude": {"mode": "malformed", "selector": None, "resolution_time": None},
            "omp": {
                "mode": "malformed",
                "selector": None,
                "resolution_time": None,
                "note": str(error),
            },
        }
    omp_provenance = selection["model_provenance"]["omp"]
    if omp_provenance == "legacy_unpinned":
        omp = {
            "mode": "legacy_unpinned",
            "selector": None,
            "resolution_time": None,
            "note": "Pre-3.5 grant without a recorded OMP policy; launch is refused until reauthorized.",
        }
    elif selection["omp_model"] is None:
        omp = {
            "mode": "dynamic_default",
            "selector": None,
            "resolution_time": "attempt_start",
            "note": "OMP's configured default selection is evaluated when each attempt starts; pass --omp-model for a fixed selector.",
        }
    else:
        omp = {
            "mode": "fixed_selector",
            "selector": selection["omp_model"],
            "resolution_time": "authorization",
            "note": "The requested selector is recorded at authorization; the answering model is reported separately as observed identity.",
        }
    return {
        "claude": {
            "mode": "fixed_selector",
            "selector": selection["claude_model"],
            "resolution_time": "authorization",
            "note": "Managed Claude runs the recorded selector; the documented default is "
            + CLAUDE_DEFAULT_MODEL
            + ".",
        },
        "omp": omp,
    }


def grant_preview(grant: dict) -> dict:
    """Operator-facing summary of what an authorization actually permits."""
    budget = attempt_budget(grant)
    return {
        **budget,
        "model_selection": model_policy(grant),
        "reserve_policy": (
            "Each launch reserves min(max_attempt_cost_usd, max_cost_usd - used - "
            "active reserves); concurrent ready steps share the unreserved remainder "
            "in launch order; unknown reported cost stops new launches."
        ),
        "permissions": {
            "read": True,
            "edit_write": grant.get("allow_work") is True,
            "shell": shell_permission(grant),
            "review_checks": (grant.get("review_check_policy") or {}).get("policy")
            == "supervisor_checks_v1"
            and valid_container(
                (grant.get("review_check_policy") or {}).get("container")
            ),
            "network": "unrestricted for the worker process",
            "os_sandbox": False,
        },
    }


WITHHELD = {
    "withheld": "author interpretation is hidden until the independent report is recorded and comparison is opened"
}
AUTHOR_FIELDS = ("answer", "evidence")


def independent_stage(bound: dict | None) -> bool:
    """True while a trusted independent-first review attempt may not see author material."""
    return bool(
        bound
        and bound.get("kind") == "review"
        and bound.get("protocol") == "independent_first"
        and not bound.get("comparison_opened_at")
    )


def review_inputs(attempt: dict, plan: dict) -> dict:
    """Prospective immutable capture inputs, excluding author interpretation."""
    step = next(step for step in plan["steps"] if step["id"] == attempt["step_id"])
    submission = attempt["submission"]
    return {
        "requirements": "\n".join(
            [
                f"Plan goal: {plan['goal']}",
                f"Step {step['id']}: {step['goal']}",
                "Global constraints:",
                *(f"- {item}" for item in plan.get("constraints", [])),
                "Global acceptance:",
                *(f"- {item}" for item in plan["acceptance"]),
            ]
        ),
        "criteria": list(step["acceptance"]),
        "base": submission["base_commit"],
        "source": "commit",
        "commit": submission["commit"],
        "context_paths": sorted(step.get("review_context_paths", [])),
    }


def review_scope(attempt: dict, plan: dict) -> dict:
    """Versioned applicability identity available before capture or expenditure."""
    return {
        "work_id": attempt["work_id"],
        "step_id": attempt["step_id"],
        "plan_revision": attempt["plan_revision"],
        "submission_id": attempt["submission"]["submission_id"],
        "base_commit": attempt["submission"]["base_commit"],
        "commit": attempt["submission"]["commit"],
        "authorization_id": attempt.get("authorization_id"),
        "policy": "shell_review",
        "policy_version": 1,
        "snapshot_input_fingerprint": hashlib.sha256(
            _json(review_inputs(attempt, plan)).encode()
        ).hexdigest(),
    }


def _withhold(record):
    if isinstance(record, dict) and any(key in record for key in AUTHOR_FIELDS):
        return {
            **{key: value for key, value in record.items() if key not in AUTHOR_FIELDS},
            **{key: WITHHELD for key in AUTHOR_FIELDS if key in record},
        }
    return record


def _project_claim(response, attempt):
    """Project a claim response through the freshly created attempt's stage."""
    if not independent_stage(attempt):
        return response
    projected = redact_author(response, attempt)
    claim = dict(projected.get("claim") or {})
    if claim:
        claim = _withhold(claim)
        claim["submission"] = _withhold(claim.get("submission"))
        claim["dependencies"] = [
            _withhold(item) for item in claim.get("dependencies") or []
        ]
        projected["claim"] = claim
    return projected


def redact_author(payload, bound: dict | None):
    """One server-side visibility policy for every channel a reviewer can read.

    Applied to card views, history events and mutation responses handed to a
    review attempt before its comparison stage: submissions, submission intents,
    checkpoints and dependency submissions keep raw provenance (commit, tree,
    changed files) but lose the author's answer/evidence; history events that
    carried them are withheld too. Requirements, plan and blockers stay visible.
    """
    if not independent_stage(bound):
        return payload
    if isinstance(payload, dict) and "steps" in payload and "plan" in payload:
        view = json.loads(_json(payload))
        for step in view["steps"]:
            step["submission"] = _withhold(step.get("submission"))
            step["checkpoint"] = _withhold(step.get("checkpoint"))
            attempt = step.get("attempt")
            if attempt and attempt.get("kind") == "implement":
                # The implementer's finished answer/evidence are author material too.
                step["attempt"] = attempt = _withhold(attempt)
            if attempt:
                attempt["submission"] = _withhold(attempt.get("submission"))
                attempt["submission_intent"] = _withhold(
                    attempt.get("submission_intent")
                )
                attempt["checkpoint"] = _withhold(attempt.get("checkpoint"))
                attempt["dependencies"] = [
                    _withhold(item) for item in attempt.get("dependencies") or []
                ]
        if view.get("result"):
            view["result"] = _withhold(view["result"])
        if isinstance(view.get("recovery"), dict):
            view["recovery"]["submission"] = _withhold(
                view["recovery"].get("submission")
            )
        for transition in (
            view.get("transition"),
            *(view.get("transition_history") or []),
        ):
            for entry in (transition or {}).get("inventory", {}).values():
                saved = entry.get("saved")
                if isinstance(saved, dict):
                    for key in ("submission_intent", "checkpoint"):
                        saved[key] = _withhold(saved.get(key))
        view["visibility"] = "independent_stage"
        return view
    if (
        isinstance(payload, dict)
        and "events" in payload
        and isinstance(payload["events"], list)
    ):
        events = []
        for event in payload["events"]:
            details = event.get("details")
            if isinstance(details, dict) and any(
                key in details for key in ("note", *AUTHOR_FIELDS)
            ):
                details = {
                    key: (WITHHELD if key in ("note", *AUTHOR_FIELDS) else value)
                    for key, value in details.items()
                }
            projected = {**event, "details": details}
            if isinstance(event.get("snapshot"), dict):
                # Each history event embeds a full card snapshot; project it too.
                projected["snapshot"] = redact_author(event["snapshot"], bound)
            events.append(projected)
        return {**payload, "events": events, "visibility": "independent_stage"}
    return payload


def model_selection(record: dict) -> dict:
    """Decode saved policy; only wholly pre-selection records are legacy grants."""
    keys = {"claude_model", "omp_model", "model_provenance"}
    if not keys.intersection(record):
        return {
            "claude_model": CLAUDE_DEFAULT_MODEL,
            "omp_model": None,
            "model_provenance": {
                "claude": "legacy_default",
                "omp": "legacy_unpinned",
            },
        }
    if not keys.issubset(record):
        raise ValueError(
            "Incomplete authorized model selection; reauthorize before launch"
        )
    claude_model = validate_model(record["claude_model"])
    omp_model = record["omp_model"]
    if omp_model is not None:
        validate_model(omp_model)
    provenance = record["model_provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("claude") not in {"explicit", "default", "legacy_default"}
        or provenance.get("omp") not in {"explicit", "default", "legacy_unpinned"}
        or (provenance["claude"] != "explicit" and claude_model != CLAUDE_DEFAULT_MODEL)
        or (provenance["omp"] == "explicit") != (omp_model is not None)
    ):
        raise ValueError(
            "Invalid authorized model provenance; reauthorize before launch"
        )
    return {
        "claude_model": claude_model,
        "omp_model": omp_model,
        "model_provenance": dict(provenance),
    }


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


def _validate_work_path(path, *, context=False):
    parts = PurePosixPath(path).parts
    if (
        not parts
        or path != str(PurePosixPath(path))
        or path.startswith("/")
        or any(part in {".", "..", ".git"} for part in parts)
        or any(char in path for char in "\\\x00\n\r*?[]")
        or (
            context
            and (
                ":" in path
                or any(part.casefold() == ".git" for part in parts)
                or any(ord(char) < 32 for char in path)
            )
        )
    ):
        raise ValueError(
            "Work paths must be relative exact paths without escapes or globs"
        )


class WorkStep(_Model):
    id: _Identifier
    title: _NonBlank
    goal: _NonBlank
    owner: _Actor
    reviewer: _Actor
    owned_files: list[_NonBlank] = Field(default_factory=list, max_length=500)
    review_context_paths: list[_NonBlank] = Field(
        default_factory=list,
        max_length=256,
        description="Exact additional read-only review paths from the submitted commit. Does not expand owned_files; new context requires a new agreed snapshot.",
    )
    depends_on: list[_Identifier] = Field(
        default_factory=list,
        max_length=200,
        description="Prerequisite step IDs. One final integration step must depend, directly or transitively, on every other step, including investigation and verification steps.",
    )
    acceptance: list[_NonBlank] = Field(min_length=1, max_length=100)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    verification: VerificationPlan | None = None
    review_requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    review_verification: VerificationPlan | None = None

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        if self.owner == self.reviewer:
            raise ValueError("Step owner and reviewer must be distinct principals")
        if self.review_requirements.requires_write:
            raise ValueError("Review requirements cannot require writes")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Duplicate dependency")
        if len(set(self.owned_files)) != len(self.owned_files):
            raise ValueError("Duplicate owned file")
        # Preserve legacy ownership validation and operation identity; the new
        # read-context declaration also excludes URI and case-folded Git paths.
        for path in self.owned_files:
            _validate_work_path(path)
        for path in self.review_context_paths:
            _validate_work_path(path, context=True)
        self.review_context_paths = list(dict.fromkeys(self.review_context_paths))
        return self


class WorkPlan(_Model):
    title: _NonBlank
    goal: _NonBlank
    constraints: list[_NonBlank] = Field(default_factory=list, max_length=100)
    acceptance: list[_NonBlank] = Field(min_length=1, max_length=100)
    steps: list[WorkStep] = Field(
        min_length=1,
        max_length=200,
        description="Acyclic checklist with exactly one final integration step that depends transitively on all other steps. A one-step plan is valid.",
    )
    context: str = Field(default="", max_length=64000)

    @model_validator(mode="after")
    def acceptance_coverage_unsupported(self) -> Self:
        # Declared acceptance evidence is task-local in 3.10.0. Accepting these
        # fields here and ignoring them would be worse than refusing them.
        for step in self.steps:
            for plan in (step.verification, step.review_verification):
                if plan is None:
                    continue
                if (
                    plan.acceptance_coverage != "report_only"
                    or plan.coverage_scope is not None
                    or any(check.acceptance_refs for check in plan.checks)
                ):
                    raise ValueError(
                        "acceptance_coverage_unsupported_surface: declared acceptance "
                        "evidence is supported on task contracts, not on work steps"
                    )
        return self

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        steps = {step.id: step for step in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("Step IDs must be unique")
        ancestors: dict[str, set[str]] = {}
        visiting: set[str] = set()

        def visit(step_id):
            if step_id not in steps:
                raise ValueError("Dependency references a missing step")
            if step_id in visiting:
                raise ValueError("Dependencies must not form a cycle")
            if step_id not in ancestors:
                visiting.add(step_id)
                result = set()
                for dependency in steps[step_id].depends_on:
                    result.add(dependency)
                    result.update(visit(dependency))
                visiting.remove(step_id)
                ancestors[step_id] = result
            return ancestors[step_id]

        for step in self.steps:
            visit(step.id)
        for index, left in enumerate(self.steps):
            for right in self.steps[index + 1 :]:
                overlap = any(
                    a == b or a.startswith(b + "/") or b.startswith(a + "/")
                    for a in left.owned_files
                    for b in right.owned_files
                )
                if (
                    overlap
                    and left.id not in ancestors[right.id]
                    and right.id not in ancestors[left.id]
                ):
                    raise ValueError(
                        "Overlapping ownership requires an ancestor dependency"
                    )
        referenced = {
            dependency for step in self.steps for dependency in step.depends_on
        }
        sinks = set(steps) - referenced
        if len(sinks) != 1:
            raise ValueError(
                "Plan requires one final integration step depending transitively on every other step. "
                f"Independent final steps: {', '.join(sorted(sinks))}. "
                "Choose the intended final integration step and add the other listed IDs "
                "to its depends_on, or add a new integration step depending on all listed IDs. "
                "Do not remove required work to satisfy this check."
            )
        return self


class WorkCommand(_Model):
    """Shared-task command. create requires plan, expected_revision=0, a stable
    operation_id, and no work_id. All other mutations require the current revision
    from get and a stable operation_id. list/get/history need no operation_id.
    """

    action: Literal[
        "create",
        "get",
        "list",
        "history",
        "propose",
        "agree",
        "claim",
        "heartbeat",
        "block",
        "unblock",
        "submit",
        "accept",
        "reject",
        "report",
        "compare",
        "pause",
        "resume",
        "reconcile",
        "recover",
    ]
    work_id: str | None = None
    step_id: str | None = None
    expected_revision: int | None = Field(
        default=None,
        ge=0,
        description="Required for every mutation: exactly 0 for create; otherwise the current card revision from get. For history, an optional exclusive event cursor. Not required for list/get.",
    )
    operation_id: _NonBlank | None = Field(
        default=None,
        description="Required for every mutation, including create. Use a unique ID for each new or corrected request; reuse it only when retrying the exact same request. Not required for list/get/history.",
    )
    plan: WorkPlan | None = Field(
        default=None,
        description="Required for create/propose. Include every required step in the dependency graph under one final integration step.",
    )
    note: _NonBlank | None = None
    blocker_id: str | None = None
    condition: _NonBlank | None = None
    resolution: _NonBlank | None = Field(
        default=None,
        description="For unblock: resolved or not_applicable with note and evidence, or a legacy free-text resolution reason with evidence. For reconcile: retry or abandon.",
    )
    evidence: list[_NonBlank] = Field(default_factory=list, max_length=100)
    submission_id: str | None = None
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")] | None = (
        None
    )

    @model_validator(mode="after")
    def validate_mutation(self) -> Self:
        if self.action in {"list", "get", "history"}:
            return self
        missing = [
            field
            for field in ("expected_revision", "operation_id")
            if getattr(self, field) is None
        ]
        if missing:
            revision_help = (
                "Set expected_revision=0 for create and omit work_id."
                if self.action == "create"
                else "Read the current card with get and use its revision as expected_revision."
            )
            raise ValueError(
                f"{self.action} requires missing field(s): {', '.join(missing)}. "
                f"{revision_help} Supply a unique operation_id for each new or corrected "
                "request; reuse it only for an exact retry."
            )
        if self.action == "create" and (
            self.plan is None or self.expected_revision != 0 or self.work_id is not None
        ):
            raise ValueError(
                "Create requires plan, expected_revision=0, and no work_id"
            )
        return self


class WorkConflict(ValueError):
    """The caller must read the current card before revising it."""

    def __init__(self, message, *, current=None):
        super().__init__(message)
        self.current = current


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _operation_fingerprint(command, bound, *, version=2):
    """Preserve exact historical receipts when new declarations remain empty."""
    payload = command.model_dump()
    empty_requirements = TaskRequirements().model_dump()
    for step in (payload.get("plan") or {}).get("steps", []):
        if step.get("review_context_paths") == []:
            step.pop("review_context_paths")
        for key in ("requirements", "review_requirements"):
            if step.get(key) == empty_requirements:
                step.pop(key)
        for key in ("verification", "review_verification"):
            if step.get(key) is None:
                step.pop(key)
                continue
            # Additions that carry no declaration must not change an old receipt.
            plan = step[key]
            if plan.get("acceptance_coverage") == "report_only":
                plan.pop("acceptance_coverage")
            if plan.get("coverage_scope") is None:
                plan.pop("coverage_scope", None)
            for check in plan.get("checks") or []:
                if check.get("acceptance_refs") == []:
                    check.pop("acceptance_refs")
    digest = hashlib.sha256(
        _json(
            {"command": payload, "attempt_id": bound["attempt_id"] if bound else None}
        ).encode()
    ).hexdigest()
    return digest if version == 1 else f"v2:{digest}"


def _public_binding(binding):
    """Provenance without replay material: no operation ids, no host identities."""
    if not isinstance(binding, dict):
        return binding
    return {
        "version": binding.get("version"),
        "host_owner_bound": binding.get("host_owner") is not None,
        "task_id": binding.get("task_id"),
        "conversation_id": binding.get("conversation_id"),
        "bound_at": binding.get("bound_at"),
        "origin_settled": binding.get("origin_settled"),
        "successors": [
            {
                key: entry.get(key)
                for key in ("successor_id", "principal", "scope", "authorized_at")
            }
            | {"consumed": bool(entry.get("consumed"))}
            for entry in binding.get("successors") or []
        ],
        "recoveries": [
            {key: item.get(key) for key in ("at", "principal", "successor_id")}
            for item in binding.get("recoveries") or []
        ],
    }


def _public_attempt(attempt):
    public = {
        key: _public_binding(value) if key == "binding" else value
        for key, value in attempt.items()
        if key not in {"token", "token_hash", "review_check_policy"}
    }
    if attempt.get("review_check_policy"):
        public["review_check_policy"] = public_policy(attempt["review_check_policy"])
    if attempt.get("kind") == "implement":
        committed = bool((attempt.get("output") or {}).get("commit"))
        public["output_committed"] = committed
        public["submission_progress"] = (
            "output_committed"
            if committed
            else "capture_failed"
            if attempt.get("submission_intent") and attempt.get("finished_at")
            else "intent_recorded"
            if attempt.get("submission_intent")
            else "not_requested"
        )
    return public


def _claim_replay_allowed(attempt, origin):
    """An exact claim retry re-issues its credential only to the claiming origin.

    The receipt is keyed by principal and operation id, which another host of
    the same principal can learn; the durable binding supplies the missing
    session identity. Legacy attempts without a binding stay readable but never
    hand out fresh authority through replay.
    """
    binding = attempt.get("binding")
    if not binding:
        return False
    origin = origin or {}
    return origin.get("host_owner") == binding.get("host_owner") and (
        binding.get("task_id") is None
        or binding.get("task_id") == origin.get("task_id")
    )


RECOVERY_ACTIONS = frozenset(
    {"get", "history", "heartbeat", "block", "report", "compare", "accept", "reject"}
)
RECOVERY_CODES = (
    "recovery_not_authorized",
    "recovery_legacy_unbound",
    "recovery_claim_completed",
    "recovery_claim_fenced",
    "recovery_claim_expired",
    "recovery_context_changed",
    "recovery_stop_unconfirmed",
    "recovery_reconcile_required",
)


TRANSITION_OPEN = frozenset({"stopping", "ready"})
TRANSITION_CODES = (
    "transition_in_progress",
    "no_pending_proposal",
    "transition_not_begun",
    "attempt_not_in_inventory",
    "attempt_already_disposed",
    "stop_unconfirmed",
    "attestation_required",
    "inventory_not_disposed",
    "proposal_mismatch",
)


def _public_proposal(proposal):
    if not proposal:
        return None
    return {
        key: proposal.get(key)
        for key in (
            "proposal_id",
            "base_plan_revision",
            "proposed_by",
            "note",
            "at",
            "preview",
            "plan",
            "repository_observation",
        )
    }


def _binding(command, origin):
    """Durable manual-claim provenance; never dispatch authority."""
    origin = origin or {}
    return {
        "version": 1,
        "host_owner": origin.get("host_owner"),
        "task_id": origin.get("task_id"),
        "conversation_id": origin.get("conversation_id"),
        "claim_operation_id": command.operation_id,
        "bound_at": time.time(),
        "origin_settled": None,
        "successors": [],
        "recoveries": [],
    }


def _recovery_context(attempt):
    return {
        "plan_revision": attempt["plan_revision"],
        "source_commit": attempt.get("source_commit"),
        "submission_id": (attempt.get("submission") or {}).get("submission_id"),
        "review_stage": attempt.get("review_stage"),
        "protocol": attempt.get("protocol"),
    }


class WorkPresentation(_Model):
    """Transport options, deliberately outside exact operation identity."""

    view: Literal["summary", "plan", "step", "full"] = "summary"
    format: Literal["json", "markdown"] = "json"
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    include_snapshots: bool = False
    section: str | None = Field(
        default=None,
        pattern=r"^(?:[a-z_]+|verification/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        max_length=64,
    )


def _section_arguments(work_id, section):
    return {"request": {"action": "get", "work_id": work_id}, "section": section}


def present_work(
    payload, *, actor, view="summary", format="json", limit=50, step_id=None
):
    """Reduce an already authorized, stage-projected response; never redact here."""
    if "items" in payload:
        items = payload["items"]
        result = {
            "items": [
                present_work(item, actor=actor, view=view, limit=limit)
                for item in items[:limit]
            ]
        }
        for key in ("next_cursor", "as_of_event", "error"):
            if key in payload:
                result[key] = payload[key]
        if len(items) > limit:
            result["continuation"] = {
                "section": "items",
                "remaining_count": len(items) - limit,
                "request": {"action": "list", "limit": limit},
            }
    elif "plan" not in payload or view == "full":
        result = {key: value for key, value in payload.items() if key != "markdown"}
    else:
        result = {
            key: payload[key]
            for key in (
                "work_id",
                "revision",
                "plan_revision",
                "status",
                "visibility",
                "participant",
                "bound_attempt",
                "next_actions",
                "recovery",
                "application",
                "repository",
                "repository_observation",
                "closure",
                "predecessors",
                "provenance",
                "activation_preview",
            )
            if key in payload
        }
        result["title"] = payload["plan"]["title"]
        result["role"] = actor
        result["pointers"] = {
            "plan": {"view": "plan"},
            "step": {"view": "step", "required_fields": ["step_id"]},
            "history": {"action": "history"},
            "full": {"view": "full"},
        }
        if view == "plan":
            result["plan"] = payload["plan"]
            result["agreements"] = payload["agreements"]
            result["proposal"] = payload.get("proposal")
        elif view == "step":
            if not step_id:
                raise ValueError("view=step requires step_id")
            step = next(
                (item for item in payload["steps"] if item["id"] == step_id), None
            )
            if step is None:
                raise ValueError("Unknown step")
            spec = next(
                item for item in payload["plan"]["steps"] if item["id"] == step_id
            )
            result["step"] = {
                **spec,
                **{
                    key: value
                    for key, value in step.items()
                    if key not in {"attempt", "submission", "acceptance"}
                },
                "acceptance": spec["acceptance"],
                "review_acceptance": step["acceptance"],
                "attempts": step.get(
                    "attempts", [step["attempt"]] if step["attempt"] else []
                ),
                "submissions": step.get(
                    "submissions", [step["submission"]] if step["submission"] else []
                ),
            }
        else:
            result["paused"] = payload["status"] == "paused"
            proposal = payload.get("proposal")
            result["proposal"] = (
                None
                if not proposal
                else {
                    **{
                        key: proposal.get(key)
                        for key in (
                            "proposal_id",
                            "base_plan_revision",
                            "proposed_by",
                            "at",
                        )
                    },
                    "affected_attempts": len(
                        (proposal.get("preview") or {}).get("attempts") or []
                    ),
                    "card_wide": (proposal.get("preview") or {}).get("card_wide"),
                    "plan": {"view": "plan"},
                }
            )
            transition = payload.get("transition")
            result["transition"] = (
                None
                if not transition
                else {
                    "transition_id": transition["transition_id"],
                    "phase": transition["phase"],
                    "attempts": len(transition["inventory"]),
                    "disposed": sum(
                        1
                        for item in transition["inventory"].values()
                        if item.get("disposition")
                    ),
                }
            )
            if payload.get("operator_commands"):
                result["operator_commands"] = payload["operator_commands"]
            grant = payload["authorization"]
            result["authorization"] = (
                None
                if grant is None
                else {
                    key: grant.get(key)
                    for key in (
                        "authorization_id",
                        "plan_revision",
                        "deadline",
                        "revoked_at",
                        "unknown_cost",
                    )
                }
            )
            result["agreements"] = {
                principal: {
                    "plan_revision": record["plan_revision"],
                    "at": record["at"],
                }
                for principal, record in payload["agreements"].items()
            }
            result["steps"] = []
            blockers = list(payload["blockers"])
            for step in payload["steps"]:
                blockers.extend(step["blockers"])
                compact = {
                    key: step[key]
                    for key in ("id", "state", "owner", "reviewer", "depends_on")
                }
                compact["submission"] = (
                    None
                    if not step["submission"]
                    else {
                        key: step["submission"][key]
                        for key in ("submission_id", "commit")
                    }
                )
                attempt = step["attempt"]
                compact["attempt"] = (
                    None
                    if not attempt
                    else {
                        key: attempt.get(key)
                        for key in ("attempt_id", "actor", "kind", "state")
                    }
                )
                if compact["attempt"] is not None:
                    compact["attempt"]["stage"] = attempt.get("review_stage")
                compact["unresolved_blocker_count"] = len(
                    WorkStore._unresolved(payload, step)
                )
                result["steps"].append(compact)
            unresolved = [
                blocker for blocker in blockers if blocker["resolved_at"] is None
            ]
            result["blockers"] = unresolved[:limit]
            if len(unresolved) > limit:
                result.setdefault("continuation", []).append(
                    {
                        "section": "blockers",
                        "remaining_count": len(unresolved) - limit,
                        "arguments": _section_arguments(payload["work_id"], "blockers"),
                    }
                )
            if len(result["steps"]) > limit:
                result.setdefault("continuation", []).append(
                    {
                        "section": "steps",
                        "remaining_count": len(result["steps"]) - limit,
                        "arguments": _section_arguments(payload["work_id"], "steps"),
                    }
                )
                result["steps"] = result["steps"][:limit]
        if payload.get("claim"):
            result["claim"] = {
                key: value
                for key, value in payload["claim"].items()
                if key
                in {
                    "attempt_id",
                    "step_id",
                    "actor",
                    "kind",
                    "state",
                    "token",
                    "deadline",
                }
            }
    if "runtime_identity" in payload:
        result["runtime_identity"] = payload["runtime_identity"]
    if view == "summary" and "plan" in payload:
        # Each replaced section remains completely readable, including long text,
        # through a bounded, freshly authorized page. Never truncate a requirement.
        for key in sorted(
            result, key=lambda key: len(json.dumps(result[key])), reverse=True
        ):
            if len(json.dumps(result).encode("utf-8")) <= 12000:
                break
            if key in {
                "work_id",
                "revision",
                "plan_revision",
                "status",
                "claim",
                "visibility",
            }:
                continue
            value = result[key]
            result[key] = {
                "paged": True,
                "count": len(value) if isinstance(value, (list, dict)) else None,
                "arguments": _section_arguments(payload["work_id"], key),
            }
    if format == "markdown":
        # Render exactly the selected material, without a second JSON copy.
        return {
            "markdown": "```json\n"
            + json.dumps(result, ensure_ascii=False, indent=2)
            + "\n```"
        }
    return result


def _submission_request(command, bound):
    if command.work_id is None:
        command = command.model_copy(update={"work_id": bound["work_id"]})
    if command.step_id is None:
        command = command.model_copy(update={"step_id": bound["step_id"]})
    if command.work_id != bound["work_id"] or command.step_id != bound["step_id"]:
        raise ValueError("Bound worker cannot act on another work or step")
    return command


class WorkStore:
    def __init__(self, database: Path, scope: ProjectScope):
        self.database = Path(database)
        self.scope = scope
        self._review_checks = ReviewCheckState(self)
        # Per-thread delegation marker: set only while a successor host acts on a
        # recovered claim, so recorded events name who actually recorded them.
        self._local = threading.local()
        if (
            self.database != scope.directory / "tasks.sqlite3"
            or self.database.is_symlink()
        ):
            raise ValueError(
                "Work database must be this launch project's scoped task database"
            )
        # Check our own tables too: the shared initializer predates this store.
        if self.database.exists():
            with closing(sqlite3.connect(self.database)) as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                identity = (
                    db.execute(
                        "SELECT scope_id, project_root FROM bridge_scope WHERE singleton=1"
                    ).fetchone()
                    if "bridge_scope" in tables
                    else None
                )
                if identity is not None and identity != (scope.key, str(scope.root)):
                    raise ValueError("Database belongs to a different launch project")
                if identity is None and any(
                    table.startswith("work_") for table in tables
                ):
                    raise ValueError(
                        "Unscoped work data cannot be attached to a project"
                    )
        initialize_database(scope)
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS work_cards (work_id TEXT PRIMARY KEY, card TEXT NOT NULL)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(work_cards)")}
            for name, kind in (
                ("revision", "INTEGER"),
                ("status", "TEXT"),
                ("updated_at", "REAL"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE work_cards ADD COLUMN {name} {kind}")
                    db.execute(
                        f"UPDATE work_cards SET {name}=json_extract(card, '$.{name}')"
                    )
            db.execute(
                "CREATE TABLE IF NOT EXISTS work_events (work_id TEXT NOT NULL, revision INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(work_id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS work_attempts (attempt_id TEXT PRIMARY KEY, work_id TEXT NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, native_task_id TEXT UNIQUE, attempt TEXT NOT NULL)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS work_one_active_attempt ON work_attempts(work_id, step_id) WHERE state IN ('reserved','running','recovery_required')"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS work_operations (actor TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(actor, operation_id))"
            )
            self._review_checks._initialize_review_checks(db)

    @contextmanager
    def _transaction(self, *, timeout=10):
        if self.database.is_symlink():
            raise ValueError("Project database must not be a symbolic link")
        with closing(
            sqlite3.connect(self.database, timeout=timeout, isolation_level=None)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute(
                "SELECT scope_id, project_root FROM bridge_scope WHERE singleton=1"
            ).fetchone()
            if identity is None or tuple(identity) != (
                self.scope.key,
                str(self.scope.root),
            ):
                raise ValueError("Database belongs to a different launch project")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def _read_connection(self):
        if self.database.is_symlink():
            raise ValueError("Project database must not be a symbolic link")
        with closing(
            sqlite3.connect(
                self.database.as_uri() + "?mode=ro",
                uri=True,
                timeout=1,
                isolation_level=None,
            )
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            identity = db.execute(
                "SELECT scope_id, project_root FROM bridge_scope WHERE singleton=1"
            ).fetchone()
            if identity is None or tuple(identity) != (
                self.scope.key,
                str(self.scope.root),
            ):
                raise ValueError("Database belongs to a different launch project")
            yield db

    def _maintenance(self):
        # Observers never wait for a writer just to mark an already fenced lease.
        with self._read_connection() as db:
            due = (
                db.execute(
                    "SELECT 1 FROM work_attempts WHERE state IN ('reserved','running') "
                    "AND json_extract(attempt,'$.deadline')<=? LIMIT 1",
                    (time.time(),),
                ).fetchone()
                is not None
            )
        if not due:
            return
        try:
            with self._transaction(timeout=0) as db:
                self._expire(db)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise

    def _load(self, db, work_id):
        row = db.execute(
            "SELECT card FROM work_cards WHERE work_id=?", (work_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown work item")
        card = json.loads(row["card"])
        card.setdefault("blockers", [])
        legacy = [
            (blocker, step["id"] if step else None)
            for step in [None, *card["steps"]]
            for blocker in (step["blockers"] if step else card["blockers"])
            if "origin" not in blocker
        ]
        if legacy:
            origins = {}
            for row in db.execute(
                "SELECT event FROM work_events WHERE work_id=? ORDER BY revision",
                (work_id,),
            ):
                snapshot = json.loads(row["event"])["snapshot"]
                for step in snapshot["steps"]:
                    for blocker in step["blockers"]:
                        origins.setdefault(
                            blocker["blocker_id"],
                            {
                                "plan_revision": snapshot["plan_revision"],
                                "step_id": step["id"],
                            },
                        )
            for blocker, step_id in legacy:
                blocker["origin"] = origins.get(
                    blocker["blocker_id"],
                    {"plan_revision": card["plan_revision"], "step_id": step_id},
                )
                blocker["carried_from"] = []
                blocker["resolution_history"] = (
                    [
                        {
                            "kind": "resolved",
                            "note": blocker["resolution"],
                            "evidence": blocker["evidence"],
                            "actor": blocker["resolved_by"],
                            "at": blocker["resolved_at"],
                        }
                    ]
                    if blocker["resolved_at"] is not None
                    else []
                )
        return card

    def _attempt(self, db, attempt_id):
        row = db.execute(
            "SELECT attempt FROM work_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown work attempt")
        return json.loads(row["attempt"])

    def _attempts(self, db, work_id=None, *, states=None):
        conditions, parameters = [], []
        if work_id:
            conditions.append("work_id=?")
            parameters.append(work_id)
        if states:
            conditions.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(states)
        rows = db.execute(
            "SELECT attempt FROM work_attempts"
            + (" WHERE " + " AND ".join(conditions) if conditions else ""),
            parameters,
        )
        return [json.loads(row["attempt"]) for row in rows]

    def _save_attempt(self, db, attempt):
        db.execute(
            "INSERT INTO work_attempts VALUES (?,?,?,?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state, native_task_id=excluded.native_task_id, attempt=excluded.attempt",
            (
                attempt["attempt_id"],
                attempt["work_id"],
                attempt["step_id"],
                attempt["state"],
                attempt["token_hash"],
                attempt["native_task_id"],
                _json(attempt),
            ),
        )

    def _step(self, card, step_id):
        for step in card["steps"]:
            if step["id"] == step_id:
                return step
        raise ValueError("Unknown step")

    def _agreed(self, card):
        return all(
            card["agreements"].get(actor, {}).get("plan_revision")
            == card["plan_revision"]
            for actor in ("claude", "omp")
        )

    @staticmethod
    def _unresolved(card, step=None):
        return [
            blocker
            for blockers in (card["blockers"], step["blockers"] if step else [])
            for blocker in blockers
            if blocker["resolved_at"] is None
        ]

    def _refresh(self, card):
        if card["status"] in _TERMINAL:
            return
        steps = {step["id"]: step for step in card["steps"]}
        for step in card["steps"]:
            if step["state"] in {"running", "recovery_required", "accepted"}:
                continue
            if self._unresolved(card, step):
                step["state"] = "blocked"
            elif step["submission"] is not None:
                step["state"] = "review"
            elif all(steps[item]["state"] == "accepted" for item in step["depends_on"]):
                step["state"] = "ready" if self._agreed(card) else "todo"
            elif step["state"] != "changes_requested":
                step["state"] = "todo"
        if card["status"] != "paused":
            card["status"] = (
                "completed"
                if all(step["state"] == "accepted" for step in card["steps"])
                and not self._unresolved(card)
                else "active"
                if self._agreed(card)
                else "draft"
            )

    def _view(self, db, card):
        view = json.loads(_json(card))
        if view["authorization"] is not None:
            try:
                view["authorization"].update(model_selection(view["authorization"]))
            except ValueError as error:
                view["authorization"]["model_selection_error"] = str(error)
            view["authorization"]["preview"] = grant_preview(view["authorization"])
            if view["authorization"].get("review_check_policy"):
                view["authorization"]["review_check_policy"] = public_policy(
                    view["authorization"]["review_check_policy"]
                )
        identifiers = [step["attempt"] for step in view["steps"] if step["attempt"]]
        attempts = (
            {
                row["attempt_id"]: json.loads(row["attempt"])
                for row in db.execute(
                    "SELECT attempt_id,attempt FROM work_attempts WHERE attempt_id IN ("
                    + ",".join("?" for _ in identifiers)
                    + ")",
                    identifiers,
                )
            }
            if identifiers
            else {}
        )
        for step in view["steps"]:
            step["attempt"] = (
                _public_attempt(attempts[step["attempt"]]) if step["attempt"] else None
            )
            if step["attempt"] and step["attempt"].get("kind") == "review":
                # Truthful protocol label: attempts created before independent-first
                # stages existed disclosed author material from the start.
                step["attempt"].setdefault("protocol", "legacy_disclosure")
            attempt = step["attempt"]
            committed = bool((step.get("submission") or {}).get("commit"))
            step["output_committed"] = committed
            step["submission_progress"] = (
                "output_committed"
                if committed
                else (attempt or {}).get("submission_progress", "not_requested")
            )
            if (
                step["attempt"]
                and step["attempt"]["state"] in _ACTIVE
                and step["attempt"]["deadline"] <= time.time()
            ):
                step["state"] = "recovery_required"
                step["attempt"]["state"] = "recovery_required"
        view["events"] = {"revision": card["revision"], "cursor": card["revision"]}
        records = card.get("application") or []
        view["application"] = {
            # Acceptance never implies application; absence of a record is
            # unknown/not_recorded, never a claim that nothing was applied.
            "status": "observed" if records else "not_recorded",
            "records": records,
            "assessment": "explicit operator apply/assess CLI only; progress reads perform no Git assessment",
        }
        view["proposal"] = _public_proposal(card.get("proposal"))
        view["transition"] = card.get("transition")
        view["transition_history"] = card.get("transition_history") or []
        view["repository"] = card.get("repository") or {
            "project_root": str(self.scope.root),
            "scope_id": self.scope.key,
            "provenance": "legacy scope binding; no creation-time Git observation",
        }
        view["closure"] = card.get("closure")
        view["predecessors"] = card.get("predecessors") or []
        view["provenance"] = {
            "target_verification": "not_performed",
            "reciprocal_link": "unverified; record each direction in its own scope",
            "authority_transfer": "none",
        }
        view["activation_preview"] = self._activation_preview(card)
        view["operator_commands"] = self._operator_commands(card)
        view["next_action"] = (
            "Terminal cancellation; history is retained, not accepted. Continue only under a separately agreed card."
            if card["status"] == "cancelled"
            else "Resume explicitly; fenced attempts require operator reconciliation."
            if card["status"] == "paused"
            else self._transition_next_action(card)
            if (card.get("transition") or {}).get("phase") in TRANSITION_OPEN
            else "A plan revision is pending negotiation; execution continues under the current plan until the operator begins the transition."
            if card.get("proposal")
            else "Both principals must agree to this exact plan revision."
            if not self._agreed(card)
            else (
                "All steps independently accepted. Application to the user's checkout "
                + (
                    "is not_recorded; an explicit operator apply or assess records it."
                    if not records
                    else f"was last observed as {records[-1]['kind']} ({records[-1].get('relation') or 'applied'})."
                )
            )
            if card["status"] == "completed"
            else "Reconcile uncertain attempts; never replay possibly launched work."
            if any(step["state"] == "recovery_required" for step in view["steps"])
            else "Resolve blockers or claim an eligible implementation/review step."
        )
        referenced = {
            dependency for step in card["steps"] for dependency in step["depends_on"]
        }
        view["final_step_id"] = next(
            step["id"] for step in card["steps"] if step["id"] not in referenced
        )
        view["result"] = (
            self._step(card, view["final_step_id"])["submission"]
            if card["status"] == "completed"
            else None
        )
        return self._redact(db, view)

    @staticmethod
    def _when(value):
        if not isinstance(value, (int, float)):
            return "unknown time"
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(value))

    def report_markdown(
        self, current, *, actor, attempt_token=None, usage=None, identity=None
    ):
        """Human report rendered from ALREADY projected material plus projected
        history. Presentation happens after authorization and stage projection;
        withheld author material stays withheld and nothing is narrated by a model.
        """
        with self._read_connection() as db:
            bound = (
                self._authenticate(db, attempt_token, actor) if attempt_token else None
            )
            rows = db.execute(
                "SELECT event FROM work_events WHERE work_id=? ORDER BY revision",
                (current["work_id"],),
            ).fetchall()
            attempt_steps = {
                attempt["attempt_id"]: attempt["step_id"]
                for attempt in self._attempts(db, current["work_id"])
            }
        events = redact_author(
            {"events": [json.loads(row[0]) for row in rows]},
            bound
            if bound
            else (
                {"kind": "review", "protocol": "independent_first"}
                if current.get("visibility") == "independent_stage"
                else None
            ),
        )["events"]
        previous = None
        for event in events:
            snapshot = event.pop("snapshot", None) or {}
            details = event.get("details") or {}
            step_id = details.get("step_id") or attempt_steps.get(
                details.get("attempt_id")
            )
            if step_id is None and snapshot.get("steps"):
                before = {
                    step["id"]: step for step in (previous or {}).get("steps") or []
                }
                changed = [
                    step["id"]
                    for step in snapshot["steps"]
                    if _json(step) != _json(before.get(step["id"]))
                ]
                step_id = changed[0] if len(changed) == 1 else None
            event["step_id"] = step_id
            previous = snapshot
        plan = current["plan"]
        lines = [
            f"# {plan['title']}",
            "",
            f"Work `{current['work_id']}` · revision {current['revision']} · plan {current['plan_revision']} · {current['status']}",
            "",
            plan["goal"],
        ]
        repository = current.get("repository") or {}
        lines.extend(
            [
                "",
                "## Repository and continuation provenance",
                f"- Pinned project root: `{repository.get('project_root', str(self.scope.root))}`",
                f"- Scope: `{repository.get('scope_id', self.scope.key)}`; {repository.get('provenance', 'legacy scope binding')}",
            ]
        )
        for label, observation in (
            ("Creation observation", repository.get("initial_observation")),
            ("Latest observation", current.get("repository_observation")),
        ):
            if not observation:
                lines.append(f"- {label}: not recorded")
                continue
            provenance = observation["provenance"]
            lines.append(
                f"- {label}: {observation['status']}; Git toplevel "
                f"`{observation.get('git_toplevel') or 'unverified'}`; HEAD "
                f"`{observation.get('head') or 'unverified'}`; "
                f"{provenance['action']} at {self._when(provenance['observed_at'])}"
            )
            if observation.get("reason"):
                lines.append("  Reason: " + observation["reason"])
        closure = current.get("closure")
        if closure:
            lines.extend(
                [
                    f"- Closure: {closure['disposition']} by {closure['actor']} at {self._when(closure['at'])}; not acceptance",
                    "  Reason: " + closure["note"],
                    *("  Evidence: " + item for item in closure["evidence"]),
                ]
            )
        continuation = (closure or {}).get("continuation")
        for label, links in (
            ("Continuation", [continuation] if continuation else []),
            ("Predecessor", current.get("predecessors") or []),
        ):
            for link in links:
                lines.append(
                    f"- {label}: work `{link['work_id']}` in `{link['project_root']}`; "
                    "target verification: not_performed; reciprocal link: unverified; "
                    "no cross-scope access, agreement, grant or acceptance transfer"
                )
                if link.get("note"):
                    lines.append("  Reason: " + link["note"])
                    lines.extend("  Evidence: " + item for item in link["evidence"])
        if plan.get("context"):
            lines.extend(["", "## Context", plan["context"]])
        for title, items in (
            ("Constraints", plan["constraints"]),
            ("Global acceptance", plan["acceptance"]),
        ):
            lines.extend(["", "## " + title, *("- " + item for item in items)])
        lines.extend(["", "## Agreements"])
        for principal in ("claude", "omp"):
            record = current["agreements"].get(principal)
            lines.append(
                f"- {principal}: "
                + (
                    f"plan revision {record['plan_revision']} at {self._when(record['at'])}"
                    if record
                    else "not agreed"
                )
            )
        authorization = current.get("authorization")
        lines.extend(["", "## Authorization"])
        if not authorization:
            lines.append("- none: autonomous execution is not authorized")
        else:
            grant_view = authorization.get("preview") or {}
            # A stored grant keeps authorized_at and deadline, not the requested
            # budget_seconds; render the persisted window.
            authorized_at = authorization.get("authorized_at")
            deadline = authorization.get("deadline")
            window = (
                f"{round(deadline - authorized_at)}s until {self._when(deadline)}"
                if isinstance(authorized_at, (int, float))
                and isinstance(deadline, (int, float))
                else "unknown window"
            )
            lines.append(
                "- "
                + ("revoked" if authorization.get("revoked_at") else "active")
                + f" grant {authorization.get('authorization_id')}: {window}, "
                f"max launches {authorization.get('max_launches')}, max cost {authorization.get('max_cost_usd')} USD, "
                f"per-attempt ceiling {grant_view.get('max_attempt_cost_usd')} ({grant_view.get('attempt_cost_policy')})"
            )
            permissions = grant_view.get("permissions") or {}
            lines.append(
                f"- Permissions: edit/write {permissions.get('edit_write')}, shell {permissions.get('shell')}, "
                f"os sandbox {permissions.get('os_sandbox')}"
            )
            for seat, policy in (grant_view.get("model_selection") or {}).items():
                lines.append(
                    f"- Model policy {seat}: {policy.get('mode')}"
                    + (f" `{policy['selector']}`" if policy.get("selector") else "")
                    + (
                        f" (resolved at {policy['resolution_time']})"
                        if policy.get("resolution_time")
                        else ""
                    )
                    + (f"; {policy['note']}" if policy.get("note") else "")
                )
            if authorization.get("model_selection_error"):
                lines.append(
                    "- Model selection error: " + authorization["model_selection_error"]
                )
        unresolved = [
            blocker for blocker in current["blockers"] if blocker["resolved_at"] is None
        ]
        if unresolved:
            lines.extend(["", "## Card blockers (apply to every step)"])
            lines.extend(
                "- " + WorkStore._blocker_markdown(item) for item in unresolved
            )
        lines.extend(["", "## Steps"])
        for step in current["steps"]:
            spec = next(item for item in plan["steps"] if item["id"] == step["id"])
            lines.extend(
                [
                    f"- [{'x' if step['state'] == 'accepted' else ' '}] {step['id']}: {spec['title']} — {step['state']} (owner {step['owner']}; reviewer {step['reviewer']})",
                    "  Goal: " + spec["goal"],
                    "  Dependencies: " + (", ".join(step["depends_on"]) or "none"),
                    "  Owned files: " + (", ".join(spec["owned_files"]) or "none"),
                ]
            )
            lines.extend("  - Acceptance: " + item for item in spec["acceptance"])
            lines.extend(
                "  - " + WorkStore._blocker_markdown(blocker)
                for blocker in step["blockers"]
                if blocker["resolved_at"] is None
            )
            submission = step.get("submission")
            if submission:
                lines.append(
                    f"  Submission: {submission['submission_id']} @ {submission['commit']}"
                )
            attempt = step.get("attempt")
            if attempt:
                lines.append(
                    "  Attempt: "
                    + f"{attempt['attempt_id']} {attempt['kind']} by {attempt['actor']} — {attempt['state']}; "
                    + ("managed" if attempt.get("autonomous") else "manual")
                    + (
                        f"; protocol {attempt.get('protocol')} stage {attempt.get('review_stage')}"
                        if attempt["kind"] == "review"
                        else ""
                    )
                )
            verdict = step.get("acceptance")
            if verdict:
                lines.append(
                    f"  Verdict: accepted by {verdict.get('by', step['reviewer'])} at {self._when(verdict.get('at'))}"
                )
                lines.extend(
                    "  - Evidence: " + str(item)
                    for item in verdict.get("evidence") or []
                )
            history = [event for event in events if event["step_id"] == step["id"]]
            if history:
                lines.append("  History:")
                for event in history:
                    details = event.get("details") or {}
                    extra = ", ".join(
                        f"{key}={details[key]}"
                        for key in (
                            "resolution",
                            "outcome",
                            "submission_id",
                            "reason",
                            "code",
                        )
                        if details.get(key) is not None
                    )
                    note = details.get("note")
                    lines.append(
                        f"  - r{event['revision']} {self._when(event['created_at'])} {event['kind']} by {event['actor']}"
                        + (f" ({extra})" if extra else "")
                        + (
                            f": {note}"
                            if isinstance(note, str)
                            else (" [note withheld]" if isinstance(note, dict) else "")
                        )
                    )
        proposal = current.get("proposal")
        transition = current.get("transition")
        history = current.get("transition_history") or []
        if proposal or transition or history:
            lines.extend(["", "## Plan transition"])
            if proposal:
                preview = proposal.get("preview") or {}
                lines.append(
                    f"- Pending proposal {proposal['proposal_id']} by {proposal['proposed_by']} "
                    f"at {self._when(proposal['at'])} over plan {proposal['base_plan_revision']}: "
                    f"{len(preview.get('attempts') or [])} attempt(s) affected; "
                    f"changed {preview.get('changed_steps')}, removed {preview.get('removed_steps')}, "
                    f"added {preview.get('added_steps')}"
                )
            if transition:
                lines.append(
                    f"- Transition {transition['transition_id']} phase {transition['phase']} "
                    f"begun {self._when(transition.get('begun_at'))}"
                )
                for attempt_id, entry in transition["inventory"].items():
                    disposition = entry.get("disposition")
                    stop = entry.get("stop") or {}
                    lines.append(
                        f"  - {attempt_id} {entry['kind']} by {entry['actor']} "
                        f"({'managed' if entry['autonomous'] else 'manual'}, was {entry['state_at_begin']}): "
                        + (
                            f"stop {stop.get('source')}, disposition {disposition['kind']}"
                            if disposition
                            else "awaiting stop evidence and operator disposition"
                        )
                    )
            for past in history:
                lines.append(
                    f"- Transition {past['transition_id']} {past['phase']} "
                    f"(proposal {past['proposal_id']}, {len(past['inventory'])} attempt(s))"
                )
                for attempt_id, entry in past["inventory"].items():
                    disposition = entry.get("disposition") or {}
                    stop = entry.get("stop") or {}
                    lines.append(
                        f"  - {attempt_id} {entry['kind']} by {entry['actor']} "
                        f"({'managed' if entry['autonomous'] else 'manual'}): "
                        f"stop {stop.get('source') or 'unconfirmed'}, "
                        f"disposition {disposition.get('kind') or 'none'}"
                    )
                for attempt_id, outcome in (past.get("continuation") or {}).items():
                    status = outcome.get("status")
                    if status == "blocked":
                        # 3.7.0 wrote "blocked" for a failed transfer; it never
                        # gated execution and is shown under its current name.
                        status = "not_transferable (recorded as 'blocked' by 3.7.0)"
                    lines.append(
                        f"  - continuation for {attempt_id}: {status}"
                        + (
                            f" — {outcome.get('reason')}"
                            if outcome.get("reason")
                            else ""
                        )
                        + (
                            " (capture failure acknowledged)"
                            if outcome.get("acknowledged")
                            else ""
                        )
                    )
            preview = current.get("activation_preview")
            if preview:
                lines.append(
                    "- Before activation (transfer eligibility, not launch readiness): "
                    f"steps with checkpoint {preview['steps_with_checkpoint']}, "
                    f"steps starting without checkpoint {preview['steps_without_checkpoint']}, "
                    + (
                        f"steps undetermined until their dispositions land {preview['steps_undetermined']}, "
                        if preview["steps_undetermined"]
                        else ""
                    )
                    + f"pending dispositions {preview['pending_dispositions']}, "
                    f"capture failures {preview['capture_failures']}"
                    + (
                        f" (unacknowledged: {preview['capture_failures_unacknowledged']})"
                        if preview["capture_failures_unacknowledged"]
                        else ""
                    )
                )
                for attempt_id, item in preview["attempts"].items():
                    outcome = item["continuation"]
                    lines.append(
                        f"  - {attempt_id} ({item['step_id']}): {outcome['status']}"
                        + (f" — {outcome['reason']}" if outcome.get("reason") else "")
                        + (
                            f"; preserved {item['preserved']['commit'][:12]} {item['preserved']['changed_files']}"
                            if item.get("preserved")
                            else ""
                        )
                    )
        if current.get("operator_commands"):
            lines.extend(
                [
                    "",
                    "## Operator commands (operator seat only; shown for discoverability, not permission)",
                ]
            )
            for item in current["operator_commands"]:
                lines.append(f"- {item['purpose']}: `{item['command']}`")
        application = current.get("application") or {
            "status": "not_recorded",
            "records": [],
        }
        lines.extend(["", "## Application", f"Status: {application['status']}"])
        for record in application["records"]:
            lines.append(
                f"- {record['kind']} at {self._when(record.get('recorded_at'))}: "
                + ", ".join(
                    f"{key}={record[key]}"
                    for key in (
                        "commit",
                        "expected_head",
                        "observed_head",
                        "target_commit",
                        "relation",
                    )
                    if record.get(key) is not None
                )
            )
        if identity:
            lines.extend(
                [
                    "",
                    "## Runtime",
                    f"- package {identity.get('package_version')} ({identity.get('distribution_origin')}, build {identity.get('exact_build')})",
                    f"- protocols: work {identity.get('work_protocol')}, review {identity.get('review_protocol')}",
                ]
            )
        if usage:
            lines.extend(["", "## Usage (linked native turns only)"])
            cost = usage["usage"]["cost"] if usage.get("usage") else None
            lines.append(
                f"- linked tasks: {usage['linked_task_count']}; coverage: {usage['coverage']}"
            )
            if cost:
                lines.append(
                    f"- native cost: value={cost['value']} known_subtotal={cost['known_subtotal']} ({cost['status']})"
                )
            for bucket, count in usage["unattributed"].items():
                if count:
                    lines.append(
                        f"- {bucket}: {count} attempt(s) without linked turns (unknown cost)"
                    )
        lines.extend(
            [
                "",
                "Acceptance records are attributed attestations; the store does not execute or certify checks.",
                "",
                current["next_action"],
            ]
        )
        return "\n".join(lines)

    def work_usage(self, work_id):
        """Distinct proven native turns of this work, each counted once.

        Links come only from authenticated bindings (managed dispatch
        native_task_id, native-tool claim origin task). Attempts without a
        linked turn are reported as unattributed buckets; missing cost stays
        unknown and makes coverage partial.
        """
        from .execution import conversation_usage

        with self._read_connection() as db:
            attempts = self._attempts(db, work_id)
            task_ids, unattributed = (
                [],
                {"manual_claude": 0, "manual_omp": 0, "legacy_unlinked": 0},
            )
            for attempt in attempts:
                linked = set()
                if attempt.get("native_task_id"):
                    linked.add(attempt["native_task_id"])
                binding = attempt.get("binding") or {}
                if binding.get("task_id"):
                    linked.add(binding["task_id"])
                for entry in binding.get("successors") or []:
                    consumed = entry.get("consumed") or {}
                    if consumed.get("task_id"):
                        linked.add(consumed["task_id"])
                if linked:
                    task_ids.extend(linked)
                elif not attempt.get("autonomous") and "binding" in attempt:
                    unattributed[f"manual_{attempt['actor']}"] += 1
                else:
                    unattributed["legacy_unlinked"] += 1
            task_ids = list(dict.fromkeys(task_ids))
            rows = []
            try:
                for identifier in task_ids:
                    row = db.execute(
                        "SELECT * FROM tasks WHERE task_id=?", (identifier,)
                    ).fetchone()
                    if row is not None:
                        rows.append(dict(row))
            except sqlite3.OperationalError:
                rows = []
        usage = conversation_usage(rows) if rows else None
        gaps = sum(unattributed.values()) + (len(task_ids) - len(rows))
        return {
            "work_id": work_id,
            "linked_task_ids": task_ids,
            "linked_task_count": len(rows),
            "usage": usage,
            "unattributed": unattributed,
            "coverage": "unknown"
            if usage is None
            else "partial"
            if gaps or usage["coverage"] != "complete"
            else "complete",
            "provenance": "distinct native turns proven by attempt bindings; no prompt parsing; Claude host turns are not metered here",
        }

    def _record(self, db, card, kind, actor, details=None):
        self._refresh(card)
        card["revision"] += 1
        card["updated_at"] = time.time()
        db.execute(
            "INSERT INTO work_cards (work_id,card,revision,status,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET card=excluded.card, revision=excluded.revision, status=excluded.status, updated_at=excluded.updated_at",
            (
                card["work_id"],
                _json(card),
                card["revision"],
                card["status"],
                card["updated_at"],
            ),
        )
        view = self._view(db, card)
        event = {
            "work_id": card["work_id"],
            "revision": card["revision"],
            "plan_revision": card["plan_revision"],
            "kind": kind,
            "actor": actor,
            "created_at": card["updated_at"],
            "details": self._redact(
                db,
                {
                    **(details or {}),
                    **(
                        {"recorded_by": delegation}
                        if (delegation := getattr(self._local, "delegation", None))
                        else {}
                    ),
                },
            ),
            "snapshot": view,
        }
        db.execute(
            "INSERT INTO work_events VALUES (?,?,?)",
            (card["work_id"], card["revision"], _json(event)),
        )
        return view

    def _redact(self, db, value):
        encoded = _json(value)
        # Cross-card secrets must still be removed, but their attempt bodies need
        # not be decoded (reports and historical bindings can be very large).
        for row in db.execute(
            "SELECT json_extract(attempt,'$.token'),token_hash FROM work_attempts"
        ):
            for secret in row:
                if secret:
                    encoded = encoded.replace(secret, "[REDACTED]")
        return json.loads(encoded)

    @staticmethod
    def _new_steps(plan):
        return [
            {
                "id": step["id"],
                "owner": step["owner"],
                "reviewer": step["reviewer"],
                "depends_on": step["depends_on"],
                "state": "todo",
                "blockers": [],
                "submission": None,
                "checkpoint": None,
                "acceptance": None,
                "attempt": None,
            }
            for step in plan["steps"]
        ]

    @staticmethod
    def _blocker_markdown(blocker):
        origin = blocker["origin"]
        return (
            f"Blocker {blocker['blocker_id']}: {blocker['note']}; "
            f"condition: {blocker['condition']}; "
            f"origin: plan {origin['plan_revision']}, step {origin['step_id']}; "
            f"may resolve: {blocker['actor']} or operator"
        )

    def _fence(self, db, card, reason):
        for attempt in self._attempts(db, card["work_id"]):
            if attempt["state"] in _ACTIVE:
                attempt.update(
                    state="recovery_required", error=reason, fenced_at=time.time()
                )
                self._save_attempt(db, attempt)
                for step in card["steps"]:
                    if step["id"] == attempt["step_id"]:
                        step["state"] = "recovery_required"
                        step["attempt"] = attempt["attempt_id"]

    def _expire(self, db):
        # A timeout is uncertainty, never permission for another writer.
        cards = {}
        for attempt in self._attempts(db):
            if attempt["state"] in _ACTIVE and attempt["deadline"] <= time.time():
                card = cards.setdefault(
                    attempt["work_id"], self._load(db, attempt["work_id"])
                )
                attempt.update(
                    state="recovery_required",
                    error="Attempt deadline expired; side effects may exist",
                    fenced_at=time.time(),
                )
                self._save_attempt(db, attempt)
                self._step(card, attempt["step_id"])["state"] = "recovery_required"
        for card in cards.values():
            self._record(db, card, "expired", "supervisor")

    def _credential(self, db, token, actor=None):
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute(
            "SELECT attempt FROM work_attempts WHERE token_hash=?", (digest,)
        ).fetchone()
        if row is None:
            raise ValueError("Invalid attempt credential")
        attempt = json.loads(row["attempt"])
        if not hmac.compare_digest(attempt["token_hash"], digest):
            raise ValueError("Invalid attempt credential")
        if (
            actor is not None
            and actor != attempt["actor"]
            and self._delegation(attempt, actor) is None
        ):
            raise ValueError("Attempt belongs to a different principal")
        return attempt

    @staticmethod
    def _delegation(attempt, actor, host=None):
        """The consumed successor authorization under which this caller acts.

        Scope follows the recovering host and principal, not principal
        inequality alone: a successor with the claim's own principal on another
        host is still limited to report-only closure. Without a host the
        original holder is indistinguishable from a same-principal successor,
        so only a different principal is delegated.
        """
        if attempt is None:
            return None
        for entry in (attempt.get("binding") or {}).get("successors") or []:
            if not entry.get("consumed") or entry["principal"] != actor:
                continue
            if host is None and actor == attempt["actor"]:
                continue
            if host is not None and entry["host_owner"] != host:
                continue
            return {
                "principal": actor,
                "successor_id": entry["successor_id"],
                "scope": entry["scope"],
                "on_behalf_of": attempt["actor"],
            }
        return None

    def _authenticate(self, db, token, actor=None):
        attempt = self._credential(db, token, actor)
        if attempt["state"] not in _ACTIVE or attempt["deadline"] <= time.time():
            raise ValueError("Attempt credential is fenced or expired")
        card = self._load(db, attempt["work_id"])
        if (
            card["status"] in {"paused", "cancelled", "completed"}
            or card["plan_revision"] != attempt["plan_revision"]
        ):
            raise ValueError("Attempt is no longer current")
        if attempt["autonomous"]:
            grant = card["authorization"]
            if (
                not grant
                or grant["revoked_at"] is not None
                or grant["authorization_id"] != attempt["authorization_id"]
            ):
                raise ValueError("Attempt authorization was revoked")
        return attempt

    def progress(self, work_id, *, actor=None, attempt_token=None, step_id=None):
        """Indexed observation only; permission is rechecked before returning a view."""
        with self._read_connection() as db:
            if attempt_token:
                bound = self._authenticate(db, attempt_token, actor)
                if work_id != bound["work_id"] or (
                    step_id is not None and step_id != bound["step_id"]
                ):
                    raise ValueError("Bound worker cannot observe unrelated work")
            row = db.execute(
                "SELECT revision,status,updated_at FROM work_cards WHERE work_id=?",
                (work_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown work item")
            return dict(row)

    def step_material(self, current, *, step_id, actor, attempt_token=None, limit=50):
        """Historical step material uses the same snapshot disclosure projection."""
        if not step_id:
            raise ValueError("view=step requires step_id")
        step = self._step(current, step_id)
        with self._read_connection() as db:
            bound = (
                self._authenticate(db, attempt_token, actor) if attempt_token else None
            )
            revision = db.execute(
                "SELECT revision FROM work_cards WHERE work_id=?", (current["work_id"],)
            ).fetchone()[0]
            if revision != current["revision"]:
                safe = redact_author(
                    self._view(db, self._load(db, current["work_id"])), bound
                )
                return {
                    "error": {"code": "cursor_stale"},
                    "current": present_work(safe, actor=actor),
                }
            rows = db.execute(
                "SELECT event FROM work_events WHERE work_id=? ORDER BY revision",
                (current["work_id"],),
            ).fetchall()
            events = redact_author(
                {"events": [json.loads(row[0]) for row in rows]}, bound
            )["events"]
            projected = [
                historical
                for event in events
                for historical in event["snapshot"]["steps"]
                if historical["id"] == step_id
            ]
            projected.append(step)
        attempts, submissions = {}, {}
        for snapshot in projected:
            if snapshot.get("attempt"):
                attempts[snapshot["attempt"]["attempt_id"]] = snapshot["attempt"]
            if snapshot.get("submission"):
                submissions[snapshot["submission"]["submission_id"]] = snapshot[
                    "submission"
                ]
        with self._read_connection() as db:
            card = self._load(db, current["work_id"])
            step["recovery"] = self.recovery_descriptor(
                db, card, self._step(card, step_id), bound=bound
            )
        for name, records in (("attempts", attempts), ("submissions", submissions)):
            step[name] = list(records.values())[-limit:]
            if len(records) > limit:
                step.setdefault("continuation", []).append(
                    {
                        "section": name,
                        "remaining_ids": list(records)[:-limit],
                        "action": "history",
                        "include_snapshots": True,
                    }
                )
        return current

    def list_page(
        self,
        *,
        actor,
        limit=50,
        cursor=None,
        attempt_token=None,
        claims=None,
        origin=None,
        view="summary",
    ):
        """Public keyset page. Domain list callers deliberately retain the full list."""
        if actor not in {"claude", "omp", "operator"}:
            raise ValueError("Invalid work principal")
        if attempt_token:
            raise ValueError("Bound worker cannot enumerate unrelated work")
        if not 1 <= limit <= 200:
            raise ValueError("Invalid page limit")
        self._maintenance()
        with self._read_connection() as db:
            head = db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM work_events"
            ).fetchone()[0]
            binding = {
                "actor": actor,
                "visibility": "full",
                "bound_attempt": None,
                "as_of_event": head,
                "section": "items",
                "view": view,
            }
            binding["reader_context"] = hashlib.sha256(
                _json(
                    {
                        "claims": sorted(
                            (list(key), value) for key, value in (claims or {}).items()
                        ),
                        "origin": origin,
                    }
                ).encode()
            ).hexdigest()
            after = ""
            if cursor:
                try:
                    page = json.loads(cursor)
                    if page["binding"] != binding or not isinstance(page["after"], str):
                        raise ValueError("Cursor binding differs")
                    after = page["after"]
                except (ValueError, KeyError, TypeError):
                    return {
                        "items": [],
                        "error": {"code": "cursor_stale"},
                        "next_cursor": None,
                    }
            rows = db.execute(
                "SELECT work_id FROM work_cards WHERE work_id>? ORDER BY work_id LIMIT ?",
                (after, limit + 1),
            ).fetchall()
            items = []
            page_bytes = 2
            for row in rows[:limit]:
                stored = self._load(db, row["work_id"])
                card = self._view(db, stored)
                card["next_actions"] = self.next_actions(
                    card,
                    actor=actor,
                    claims=claims,
                    origin=origin,
                    _db=db,
                    _card=stored,
                )
                item = present_work(card, actor=actor, view=view, limit=limit)
                item_bytes = (
                    len(json.dumps(item).encode("utf-8")) if view == "summary" else 0
                )
                separator_bytes = 2 if items else 0
                if (
                    view == "summary"
                    and items
                    and page_bytes + separator_bytes + item_bytes > 14000
                ):
                    break
                items.append(item)
                page_bytes += separator_bytes + item_bytes
            more = len(rows) > len(items)
            return {
                "items": items,
                "as_of_event": head,
                "next_cursor": _json(
                    {"binding": binding, "after": rows[len(items) - 1]["work_id"]}
                )
                if more and items
                else None,
            }

    def section_page(self, current, *, actor, section, cursor=None, attempt_token=None):
        """JSON-text chunks of one complete projected section, not arbitrary reads.

        Concatenate content in cursor order, then JSON-decode. The offset counts
        characters; each chunk is at most 2000 characters (including Unicode).
        """
        with self._read_connection() as db:
            bound = (
                self._authenticate(db, attempt_token, actor) if attempt_token else None
            )
            current = redact_author(current, bound)
            revision = db.execute(
                "SELECT revision FROM work_cards WHERE work_id=?", (current["work_id"],)
            ).fetchone()[0]
            stage = bound.get("review_stage") if bound else None
            binding = {
                "work_id": current["work_id"],
                "revision": current["revision"],
                "actor": actor,
                "visibility": current.get("visibility", "full"),
                "bound_attempt": bound["attempt_id"] if bound else None,
                "stage": stage,
                "section": section,
            }
            after = 0
            try:
                if revision != current["revision"]:
                    raise ValueError("Revision changed")
                if cursor:
                    page = json.loads(cursor)
                    if (
                        page["binding"] != binding
                        or type(page["after"]) is not int
                        or page["after"] < 0
                    ):
                        raise ValueError("Cursor binding differs")
                    after = page["after"]
            except (ValueError, KeyError, TypeError):
                return {
                    "error": {"code": "cursor_stale"},
                    "work_id": current["work_id"],
                }
            if section == "blockers":
                value = [
                    *current["blockers"],
                    *(
                        blocker
                        for step in current["steps"]
                        for blocker in step["blockers"]
                    ),
                ]
            elif section == "title":
                value = current["plan"]["title"]
            elif section in current and section not in {"claim", "markdown"}:
                value = current[section]
            else:
                summary = present_work(current, actor=actor)
                if section not in summary or (
                    isinstance(summary[section], dict) and summary[section].get("paged")
                ):
                    raise ValueError("Unknown work section")
                value = summary[section]
            text = _json(value)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if cursor and page.get("digest") != digest:
                return {
                    "error": {"code": "cursor_stale"},
                    "work_id": current["work_id"],
                }
            if after > len(text):
                raise ValueError("Invalid section offset")
            end = min(len(text), after + 2000)
            return {
                "work_id": current["work_id"],
                "revision": revision,
                "section": section,
                "encoding": "json",
                "offset": after,
                "content": text[after:end],
                "total_characters": len(text),
                "next_cursor": _json(
                    {"binding": binding, "after": end, "digest": digest}
                )
                if end < len(text)
                else None,
            }

    def history_page(
        self,
        current,
        *,
        actor,
        limit,
        cursor,
        include_snapshots,
        after=0,
        attempt_token=None,
    ):
        """Page events only after the caller obtained an authorized projected card."""
        bound = self.authenticate(attempt_token) if attempt_token else None
        binding = {
            "work_id": current["work_id"],
            "as_of_revision": current["revision"],
            "actor": actor,
            "visibility": current.get("visibility", "full"),
            "bound_attempt": current.get("bound_attempt"),
            "stage": bound.get("review_stage") if bound else None,
            "include_snapshots": include_snapshots,
            "section": "history",
        }
        if cursor:
            try:
                page = json.loads(cursor)
                if page["binding"] != binding or type(page["after"]) is not int:
                    raise ValueError("Cursor binding differs")
                after = page["after"]
            except (ValueError, KeyError, TypeError):
                return {
                    "error": {"code": "cursor_stale"},
                    "current": present_work(current, actor=actor),
                }
        with self._read_connection() as db:
            bound = (
                self._authenticate(db, attempt_token, actor) if attempt_token else None
            )
            if (bound.get("review_stage") if bound else None) != binding["stage"]:
                return {
                    "error": {"code": "cursor_stale"},
                    "work_id": current["work_id"],
                }
            revision = db.execute(
                "SELECT revision FROM work_cards WHERE work_id=?", (current["work_id"],)
            ).fetchone()[0]
            if revision != current["revision"]:
                current = redact_author(
                    self._view(db, self._load(db, current["work_id"])), bound
                )
                return {
                    "error": {"code": "cursor_stale"},
                    "current": present_work(current, actor=actor),
                }
            rows = db.execute(
                (
                    "SELECT event"
                    if include_snapshots
                    else "SELECT json_remove(event,'$.snapshot')"
                )
                + " FROM work_events WHERE work_id=? AND revision>? AND revision<=? ORDER BY revision LIMIT ?",
                (current["work_id"], after, revision, limit + 1),
            ).fetchall()
            events = self._redact(db, [json.loads(row[0]) for row in rows[:limit]])
        result = {
            "work_id": current["work_id"],
            "revision": revision,
            "as_of_revision": revision,
            "events": events,
            "next_cursor": _json({"binding": binding, "after": events[-1]["revision"]})
            if len(rows) > limit
            else None,
        }
        bound = (
            {"kind": "review", "protocol": "independent_first"}
            if current.get("visibility") == "independent_stage"
            else None
        )
        result = redact_author(result, bound)
        for event in result["events"]:
            if not include_snapshots:
                event.pop("snapshot", None)
            elif "snapshot" in event:
                event["snapshot"].pop("markdown", None)
        return result

    def review_check_context(self, attempt_id):
        return self._review_checks.review_check_context(attempt_id)

    def reserve_review_check(self, attempt_id, check_id):
        return self._review_checks.reserve_review_check(attempt_id, check_id)

    def start_review_check(self, run_id, *, pid=None, execution):
        return self._review_checks.start_review_check(
            run_id, pid=pid, execution=execution
        )

    def finish_review_check(
        self,
        run_id,
        *,
        result,
        exit_code,
        error,
        output,
        execution,
        process_confirmed_gone,
        input_unchanged,
    ):
        return self._review_checks.finish_review_check(
            run_id,
            result=result,
            exit_code=exit_code,
            error=error,
            output=output,
            execution=execution,
            process_confirmed_gone=process_confirmed_gone,
            input_unchanged=input_unchanged,
        )

    def review_check_assessment(self, attempt_id, *, current=True):
        return self._review_checks.review_check_assessment(attempt_id, current=current)

    def trusted_verification_context(self, attempt_id):
        return self._review_checks.trusted_verification_context(attempt_id)

    def review_check_section(
        self,
        request,
        *,
        actor,
        attempt_token=None,
        origin=None,
        section="verification",
        cursor=None,
        limit=50,
    ):
        return self._review_checks.review_check_section(
            request,
            actor=actor,
            attempt_token=attempt_token,
            origin=origin,
            section=section,
            cursor=cursor,
            limit=limit,
        )

    def _require_review_checks(self, db, attempt, *, accept=False):
        return self._review_checks._require_review_checks(db, attempt, accept=accept)

    def prepare_submission(self, request, *, actor, attempt_token, origin=None):
        """Resolve exact receipts before filesystem preflight; dry-run shared admission."""
        command = WorkCommand.model_validate(request)
        if command.action != "submit" or actor not in {"claude", "omp"}:
            raise ValueError("Submission preflight requires a managed owner submit")
        self._local.delegation = None
        try:
            with self._transaction() as db:
                bound = self._credential(db, attempt_token, actor)
                if not bound["autonomous"] or bound["kind"] != "implement":
                    raise ValueError(
                        "Submission preflight requires a managed implementation"
                    )
                command = _submission_request(command, bound)
                receipt = db.execute(
                    "SELECT fingerprint,response FROM work_operations WHERE actor=? AND operation_id=?",
                    (actor, command.operation_id),
                ).fetchone()
                if receipt:
                    # The ordinary receipt path checks fingerprint and caller binding;
                    # it intentionally does not demand a still-live historical attempt.
                    response = self._perform_command(
                        db, command, actor, attempt_token, origin, None
                    )
                    return {"replayed": True, "response": response}
                # Run the real admission path in a rolled-back savepoint, rather
                # than maintain a second, inevitably drifting CAS/phase validator.
                db.execute("SAVEPOINT submission_preflight")
                try:
                    self._perform_command(
                        db, command, actor, attempt_token, origin, None
                    )
                finally:
                    db.execute("ROLLBACK TO submission_preflight")
                    db.execute("RELEASE submission_preflight")
                return {
                    "replayed": False,
                    "attempt": bound,
                    "plan": self._load(db, bound["work_id"])["plan"],
                }
        finally:
            self._local.delegation = None

    def perform(
        self,
        command: WorkCommand | dict,
        *,
        actor: str,
        attempt_token: str | None = None,
        origin: dict | None = None,
    ) -> dict:
        command = WorkCommand.model_validate(command)
        if actor not in {"claude", "omp", "operator"}:
            raise ValueError("Invalid work principal")
        # Opportunistic expiry is separate from CAS; reads never wait for its write.
        self._maintenance()
        source_commit = None
        connection = (
            self._read_connection
            if command.action in {"get", "list", "history"}
            else self._transaction
        )
        self._local.delegation = None
        try:
            with connection() as db:
                return self._perform_command(
                    db, command, actor, attempt_token, origin, source_commit
                )
        finally:
            self._local.delegation = None

    def _perform_command(
        self, db, command, caller, attempt_token, origin, source_commit
    ):
        bound = self._credential(db, attempt_token, caller) if attempt_token else None
        delegation = self._delegation(bound, caller, (origin or {}).get("host_owner"))
        # A successor host acts on behalf of the claim's principal, within its
        # authorized reporting scope; receipts stay keyed by the real caller.
        actor = bound["actor"] if delegation else caller
        self._local.delegation = delegation
        if delegation and command.action not in RECOVERY_ACTIONS:
            raise ValueError("recovery_report_only")
        if bound:
            if command.work_id is None:
                command = command.model_copy(update={"work_id": bound["work_id"]})
            if command.work_id != bound["work_id"] or command.action in {
                "create",
                "list",
                "claim",
                "agree",
                "resume",
                "reconcile",
            }:
                raise ValueError(
                    "Bound worker cannot mutate or enumerate unrelated work"
                )
            if command.step_id is not None and command.step_id != bound["step_id"]:
                raise ValueError("Bound worker cannot act on another step")
            if (
                command.action not in {"get", "history", "propose", "pause"}
                and command.step_id is None
            ):
                command = command.model_copy(update={"step_id": bound["step_id"]})
        if command.action == "list":
            return {
                "items": [
                    self._view(db, self._load(db, row["work_id"]))
                    for row in db.execute(
                        "SELECT work_id FROM work_cards ORDER BY work_id"
                    )
                ]
            }
        if command.action in {"get", "history"}:
            if bound:
                self._authenticate(db, attempt_token, actor)
            card = self._load(db, command.work_id)
            if command.action == "get":
                return redact_author(self._view(db, card), bound)
            return redact_author(
                {
                    "work_id": card["work_id"],
                    "revision": card["revision"],
                    "cursor": card["revision"],
                    "events": [
                        json.loads(row["event"])
                        for row in db.execute(
                            "SELECT event FROM work_events WHERE work_id=? AND revision>? ORDER BY revision",
                            (card["work_id"], command.expected_revision or 0),
                        )
                    ],
                },
                bound,
            )
        fingerprint = _operation_fingerprint(command, bound)
        receipt = db.execute(
            "SELECT fingerprint,response FROM work_operations WHERE actor=? AND operation_id=?",
            (caller, command.operation_id),
        ).fetchone()
        if receipt:
            expected = (
                fingerprint
                if receipt["fingerprint"].startswith("v2:")
                else _operation_fingerprint(command, bound, version=1)
            )
            if receipt["fingerprint"] != expected:
                raise WorkConflict(
                    "Operation ID was already used for a different command"
                )
            response = json.loads(receipt["response"])
            # Credentials are never persisted in operation responses.
            if command.action in {"claim", "recover"}:
                attempt = self._attempt(db, response["claim"]["attempt_id"])
                if (
                    command.action == "claim"
                    and attempt["state"] in _ACTIVE
                    and _claim_replay_allowed(attempt, origin)
                ):
                    response["claim"]["token"] = attempt["token"]
                elif command.action == "recover":
                    # An exact recovery retry re-installs the credential only for
                    # the caller and host that recovered, while the same claim is
                    # still live in the exact recorded context; the descriptor is
                    # rebuilt from current state, never replayed.
                    response = self._recovery_replay(
                        db, command, caller, origin, response, attempt
                    )
                return _project_claim(response, attempt)
            return redact_author(response, bound)
        if bound:
            self._authenticate(db, attempt_token, actor)
        if command.action == "create":
            plan = command.plan.model_dump()
            observation = self._observe_repository(plan, action="create")
            card = {
                "work_id": str(uuid4()),
                "revision": 0,
                "plan_revision": 1,
                "status": "draft",
                "plan": plan,
                "repository": {
                    "project_root": str(self.scope.root),
                    "scope_id": self.scope.key,
                    "provenance": "creation launch scope; immutable",
                    "initial_observation": observation,
                },
                "repository_observation": observation,
                "agreements": {},
                "steps": self._new_steps(plan),
                "blockers": [],
                "authorization": None,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            result = self._record(db, card, "created", actor)
        else:
            card = self._load(db, command.work_id)
            if command.expected_revision != card["revision"]:
                raise WorkConflict(
                    f"Expected revision {command.expected_revision}; current revision is {card['revision']}",
                    current=redact_author(self._view(db, card), bound),
                )
            result = self._perform(
                db, card, command, actor, bound, source_commit, origin=origin
            )
        if command.action in {"claim", "recover"}:
            # The claimant becomes a bound reviewer at this moment: its own claim
            # response (and any exact replay of it) must already be projected.
            result = _project_claim(
                result, self._attempt(db, result["claim"]["attempt_id"])
            )
        saved = json.loads(_json(result))
        if "claim" in saved:
            saved["claim"].pop("token", None)
        db.execute(
            "INSERT INTO work_operations VALUES (?,?,?,?)",
            (caller, command.operation_id, fingerprint, _json(saved)),
        )
        if bound and command.action != "compare":
            # Re-read the credential: report/compare change what may be shown.
            bound = self._credential(db, attempt_token, actor)
        return redact_author(result, bound)

    def _perform(
        self, db, card, command, actor, bound, source_commit=None, origin=None
    ):
        action = command.action
        self._require_open(card)
        if action == "recover":
            return self._recover(db, card, command, actor, origin)
        if action == "propose":
            if command.plan is None:
                raise ValueError("Propose requires a complete plan")
            plan = command.plan.model_dump()
            transition = card.get("transition")
            if transition and transition["phase"] in TRANSITION_OPEN:
                raise WorkConflict(
                    "transition_in_progress: the begun proposal is frozen; only the "
                    "operator may withdraw or activate it"
                )
            card["repository_observation"] = self._observe_repository(
                plan, action="propose"
            )
            if plan == WorkPlan.model_validate(card["plan"]).model_dump():
                if card.get("proposal"):
                    withdrawn = card["proposal"]
                    card["proposal"] = None
                    return self._record(
                        db,
                        card,
                        "proposal_withdrawn",
                        actor,
                        {"note": command.note, "proposal_id": withdrawn["proposal_id"]},
                    )
                return self._record(db, card, "proposed", actor, {"note": command.note})
            inventory = self._transition_inventory(db, card)
            proposal = {
                "proposal_id": str(uuid4()),
                "base_plan_revision": card["plan_revision"],
                "plan": plan,
                "proposed_by": actor,
                "note": command.note,
                "at": time.time(),
                "preview": self._proposal_preview(card, plan, inventory),
                "repository_observation": card["repository_observation"],
            }
            replaced = (card.get("proposal") or {}).get("proposal_id")
            card["proposal"] = proposal
            return self._record(
                db,
                card,
                "proposed",
                actor,
                {
                    "note": command.note,
                    "proposal_id": proposal["proposal_id"],
                    "pending": True,
                    "replaced_proposal_id": replaced,
                    "affected_attempts": [item["attempt_id"] for item in inventory],
                },
            )
        if action == "agree":
            if actor not in {"claude", "omp"}:
                raise ValueError("Only the two work principals may agree")
            card["agreements"][actor] = {
                "plan_revision": card["plan_revision"],
                "at": time.time(),
                "note": command.note,
            }
        elif action == "pause":
            card["status"] = "paused"
            self._fence(
                db, card, "Work paused; process must stop before reconciliation"
            )
        elif action == "resume":
            if card["status"] != "paused":
                raise ValueError("Work is not paused")
            card["status"] = "draft"
        elif action == "reconcile":
            if actor != "operator" or bound:
                raise ValueError(
                    "Only the trusted operator may reconcile uncertain work"
                )
            if (
                not command.note
                or not command.evidence
                or command.resolution not in {"retry", "abandon"}
            ):
                raise ValueError(
                    "Reconcile requires note, evidence, and retry or abandon resolution"
                )
            attempts = [
                item
                for item in self._attempts(db, card["work_id"])
                if item["state"] == "recovery_required"
                and (command.step_id is None or item["step_id"] == command.step_id)
            ]
            if not attempts or any(
                not item["process_confirmed_gone"] for item in attempts
            ):
                raise ValueError(
                    "Supervisor must confirm every old process has stopped before reconciliation"
                )
            for attempt in attempts:
                if (
                    attempt["autonomous"]
                    and not attempt["cost_recorded"]
                    and card["authorization"]
                    and card["authorization"]["authorization_id"]
                    == attempt["authorization_id"]
                ):
                    card["authorization"]["unknown_cost"] = True
                    card["status"] = "paused"
                attempt.update(
                    state="reconciled",
                    reconciliation={
                        "resolution": command.resolution,
                        "note": command.note,
                        "evidence": command.evidence,
                        "at": time.time(),
                    },
                )
                self._save_attempt(db, attempt)
                for step in card["steps"]:
                    if step["id"] == attempt["step_id"]:
                        step["attempt"] = None
                        step["state"] = "todo"
                        if command.resolution == "abandon":
                            step["blockers"].append(
                                self._blocker(
                                    actor,
                                    command.note,
                                    "Operator must explicitly resolve abandonment before continuing",
                                    card["plan_revision"],
                                    step["id"],
                                )
                            )
            if command.resolution == "abandon":
                card["status"] = "paused"
        elif action == "unblock":
            if not command.resolution or not command.evidence:
                raise ValueError("Unblock requires resolution and evidence")
            kind = (
                command.resolution
                if command.resolution in {"resolved", "not_applicable"}
                else "resolved"
            )
            reason = (
                command.note
                if command.resolution in {"resolved", "not_applicable"}
                else command.resolution
            )
            if not reason:
                raise ValueError("Unblock resolution kind requires a reason in note")
            step = self._step(card, command.step_id) if command.step_id else None
            blocker = next(
                (
                    item
                    for item in self._unresolved(card, step)
                    if item["blocker_id"] == command.blocker_id
                ),
                None,
            )
            if blocker is None:
                raise ValueError("Unknown unresolved blocker")
            if actor not in {blocker["actor"], "operator"}:
                raise ValueError("Only blocker author or operator may resolve it")
            now = time.time()
            blocker["resolution_history"].append(
                {
                    "kind": kind,
                    "note": reason,
                    "evidence": command.evidence,
                    "actor": actor,
                    "at": now,
                    "plan_revision": card["plan_revision"],
                    **(
                        {"scope": self._review_scope(card, step)}
                        if actor == "operator"
                        and blocker["actor"] == "operator"
                        and blocker.get("policy") == "shell_review"
                        and step is not None
                        and step["submission"] is not None
                        else {}
                    ),
                }
            )
            blocker.update(
                resolved_at=now,
                resolution=reason,
                resolution_kind=kind,
                evidence=command.evidence,
                resolved_by=actor,
            )
        elif action == "claim":
            step = self._step(card, command.step_id)
            kind = "review" if step["submission"] is not None else "implement"
            attempt = self._reserve(
                db,
                card,
                step["id"],
                actor=actor,
                kind=kind,
                owner_id="manual:" + actor,
                autonomous=False,
                source_commit=source_commit,
                binding=_binding(command, origin),
            )
            result = self._record(
                db, card, "claimed", actor, {"attempt_id": attempt["attempt_id"]}
            )
            result["claim"] = {**_public_attempt(attempt), "token": attempt["token"]}
            return result
        else:
            step = self._step(card, command.step_id)
            if action in {"heartbeat", "submit", "accept", "reject"}:
                if bound is None:
                    raise ValueError(
                        "This action requires the step's active attempt credential"
                    )
                if bound["step_id"] != step["id"]:
                    raise ValueError("Attempt is bound to another step")
            if action in {"submit", "accept", "reject"} and self._unresolved(
                card, step
            ):
                raise ValueError(
                    "Resolve outstanding blockers before submitting or deciding acceptance"
                )
            if action == "heartbeat":
                bound["heartbeat_at"] = time.time()
                self._save_attempt(db, bound)
            elif action == "block":
                if not command.note or not command.condition:
                    raise ValueError("Block requires note and a resolution condition")
                if actor not in {step["owner"], step["reviewer"], "operator"}:
                    raise ValueError("Principal cannot block this step")
                if step["state"] == "accepted":
                    raise ValueError(
                        "Revise the plan before reopening an accepted output"
                    )
                blocker = self._blocker(
                    actor,
                    command.note,
                    command.condition,
                    card["plan_revision"],
                    step["id"],
                )
                step["blockers"].append(blocker)
                if bound and bound["attempt_id"] == step["attempt"]:
                    intent = bound.get("block_intent") or {
                        "blocker_ids": [],
                        "at": time.time(),
                    }
                    intent["blocker_ids"].append(blocker["blocker_id"])
                    bound["block_intent"] = intent
                    self._save_attempt(db, bound)
                else:
                    self._fence_step(db, step, "Step externally blocked")
            elif action == "submit":
                if (
                    bound["kind"] != "implement"
                    or actor != step["owner"]
                    or not command.note
                    or not command.evidence
                ):
                    raise ValueError(
                        "Only implementation owner may submit with note and evidence"
                    )
                if bound["autonomous"] and command.commit is not None:
                    raise ValueError(
                        "Managed submission commit is captured by the supervisor, never agent text"
                    )
                if not bound["autonomous"] and command.commit is None:
                    raise ValueError(
                        "Manual submission requires the exact committed output hash"
                    )
                if bound.get("submission_intent") is not None:
                    raise ValueError(
                        "Submission intent is immutable; finish the attempt or propose a revision"
                    )
                bound["submission_intent"] = {
                    "answer": command.note,
                    "evidence": command.evidence,
                    "commit": command.commit,
                    "plan_revision": card["plan_revision"],
                    "at": time.time(),
                }
                self._save_attempt(db, bound)
            elif action == "report":
                if (
                    bound is None
                    or bound["kind"] != "review"
                    or actor != step["reviewer"]
                    or not command.note
                    or not command.evidence
                ):
                    raise ValueError(
                        "Only the bound distinct reviewer may record an independent report with note and evidence"
                    )
                if reason := self._review_phase_reason(bound, action):
                    raise ValueError(reason)
                if (
                    bound.get("clarification_request")
                    and command.resolution == "success"
                ):
                    raise ValueError(
                        "Clarification requires a new snapshot; this stage is blocked"
                    )
                if command.resolution not in {"success", "partial", "blocked"}:
                    raise ValueError(
                        "Report requires resolution success, partial or blocked"
                    )
                if (
                    not step["submission"]
                    or command.submission_id != step["submission"]["submission_id"]
                    or bound["submission"]["submission_id"] != command.submission_id
                ):
                    raise WorkConflict(
                        "Report must reference the exact reviewed submission"
                    )
                bound["independent_report"] = {
                    "outcome": command.resolution,
                    "answer": command.note,
                    "evidence": command.evidence,
                    "submission_id": command.submission_id,
                    "at": time.time(),
                }
                self._save_attempt(db, bound)
                return self._record(
                    db,
                    card,
                    "independent_report",
                    actor,
                    {
                        "attempt_id": bound["attempt_id"],
                        "outcome": command.resolution,
                        "submission_id": command.submission_id,
                    },
                )
            elif action == "compare":
                if (
                    bound is None
                    or bound["kind"] != "review"
                    or actor != step["reviewer"]
                ):
                    raise ValueError(
                        "Only the bound distinct reviewer may open comparison"
                    )
                if reason := self._review_phase_reason(bound, action):
                    raise ValueError(reason)
                self._require_review_checks(db, bound)
                bound["comparison_opened_at"] = time.time()
                bound["review_stage"] = "comparison"
                self._save_attempt(db, bound)
                return self._record(
                    db,
                    card,
                    "comparison_opened",
                    actor,
                    {"attempt_id": bound["attempt_id"]},
                )
            elif action in {"accept", "reject"}:
                if (
                    bound["kind"] != "review"
                    or actor != step["reviewer"]
                    or not command.note
                    or not command.evidence
                ):
                    raise ValueError(
                        "Only distinct reviewer may decide with note and evidence"
                    )
                submission = step["submission"]
                if (
                    not submission
                    or command.submission_id != submission["submission_id"]
                    or submission["plan_revision"] != card["plan_revision"]
                    or bound["submission"]["submission_id"] != command.submission_id
                ):
                    raise WorkConflict(
                        "Review must reference the exact current submission and plan"
                    )
                if reason := self._review_phase_reason(bound, action):
                    raise ValueError(reason)
                if action == "accept":
                    self._require_review_checks(db, bound, accept=True)
                if any(
                    self._step(card, dependency)["state"] != "accepted"
                    for dependency in step["depends_on"]
                ):
                    raise WorkConflict("Dependencies are no longer accepted")
                verdict = {
                    "verdict": action,
                    "actor": actor,
                    **(
                        {"recorded_by": delegation}
                        if (delegation := getattr(self._local, "delegation", None))
                        else {}
                    ),
                    "submission_id": command.submission_id,
                    "plan_revision": card["plan_revision"],
                    "note": command.note,
                    "evidence": command.evidence,
                    "criteria": next(
                        item["acceptance"]
                        for item in card["plan"]["steps"]
                        if item["id"] == step["id"]
                    ),
                    "at": time.time(),
                }
                if action == "accept" and all(
                    item["id"] == step["id"] or item["state"] == "accepted"
                    for item in card["steps"]
                ):
                    verdict["global_criteria_attested"] = card["plan"]["acceptance"]
                bound["verdict"] = verdict
                self._save_attempt(db, bound)
                if not bound["autonomous"]:
                    step["acceptance"] = verdict if action == "accept" else None
                    step["state"] = (
                        "accepted" if action == "accept" else "changes_requested"
                    )
                    if action == "reject":
                        step["checkpoint"] = {
                            **step["submission"],
                            "checkpoint_id": str(uuid4()),
                            "step_id": step["id"],
                            "plan_revision": card["plan_revision"],
                            "created_at": time.time(),
                        }
                        step["submission"] = None
                    bound.update(
                        state="succeeded",
                        outcome="success",
                        finished_at=time.time(),
                        cost_usd=None,
                        cost_recorded=True,
                    )
                    self._save_attempt(db, bound)
            else:
                raise ValueError("Unsupported transition")
        result = self._record(
            db,
            card,
            action,
            actor,
            {"note": command.note, "evidence": command.evidence},
        )
        if action == "submit":
            result["submission_progress"] = "intent_recorded"
            result["output_committed"] = False
        return result

    @staticmethod
    def _blocker(actor, note, condition, plan_revision, step_id):
        return {
            "blocker_id": str(uuid4()),
            "actor": actor,
            "note": note,
            "condition": condition,
            "created_at": time.time(),
            "resolved_at": None,
            "origin": {"plan_revision": plan_revision, "step_id": step_id},
            "carried_from": [],
            "resolution_history": [],
        }

    def _fence_step(self, db, step, reason):
        if step["attempt"]:
            attempt = self._attempt(db, step["attempt"])
            if attempt["state"] in _ACTIVE:
                attempt.update(
                    state="recovery_required", error=reason, fenced_at=time.time()
                )
                self._save_attempt(db, attempt)
                step["state"] = "recovery_required"

    def _head(self):
        from .work_workspace import WorkWorkspace

        return WorkWorkspace(self.scope).source_commit()

    def _observe_repository(self, plan, *, action, required=False):
        from .work_workspace import WorkWorkspace

        return WorkWorkspace(self.scope).observe_repository(
            plan, action=action, required=required
        )

    @staticmethod
    def _require_open(card):
        if card["status"] in _TERMINAL:
            raise ValueError(
                f"work_terminal: {card['status']}; execution cannot reopen"
            )

    @staticmethod
    def preview_authorization(
        *,
        budget_seconds: int,
        max_launches: int,
        max_cost_usd: float,
        allow_work: bool,
        allow_shell: bool | None = None,
        allow_tests: bool | None = None,
        max_attempt_cost_usd: float | None = None,
        claude_model: str | None = None,
        omp_model: str | None = None,
        allow_review_checks: bool = False,
        review_check_timeout: int = 300,
        review_check_env: list[str] | None = None,
        review_check_container: dict | None = None,
    ) -> dict:
        """Validate and describe a grant without storing anything.

        Returns the grant fields the operator is about to activate plus the same
        `preview` block that get/show render, so the ceiling, reserve policy and
        real permissions can be inspected before any launch becomes possible.
        """
        if allow_shell is None and allow_tests is None:
            allow_shell = False
        elif allow_shell is None:
            allow_shell = allow_tests  # deprecated alias, identical permission
        elif allow_tests is not None and allow_tests != allow_shell:
            raise ValueError(
                "allow_tests is a deprecated alias of allow_shell; the values conflict"
            )
        if (
            type(budget_seconds) is not int
            or budget_seconds <= 0
            or type(max_launches) is not int
            or max_launches <= 0
            or isinstance(max_cost_usd, bool)
            or not math.isfinite(max_cost_usd)
            or max_cost_usd <= 0
            or type(allow_work) is not bool
            or type(allow_shell) is not bool
        ):
            raise ValueError(
                "Authorization requires positive finite budgets and explicit boolean permissions"
            )
        if max_attempt_cost_usd is None:
            ceiling = max_cost_usd / ATTEMPT_SHARE_DEFAULT
            policy = "default_share"
        else:
            if (
                isinstance(max_attempt_cost_usd, bool)
                or not math.isfinite(max_attempt_cost_usd)
                or max_attempt_cost_usd <= 0
            ):
                raise ValueError(
                    "max_attempt_cost_usd must be a positive finite amount"
                )
            ceiling = min(float(max_attempt_cost_usd), max_cost_usd)
            policy = "explicit"
        selection = {
            "claude_model": CLAUDE_DEFAULT_MODEL
            if claude_model is None
            else validate_model(claude_model),
            "omp_model": None if omp_model is None else validate_model(omp_model),
            "model_provenance": {
                "claude": "default" if claude_model is None else "explicit",
                "omp": "default" if omp_model is None else "explicit",
            },
        }
        review_policy = authorization_policy(
            allow_review_checks,
            review_check_timeout,
            review_check_env,
            review_check_container,
        )
        grant = {
            "budget_seconds": budget_seconds,
            "max_launches": max_launches,
            "max_cost_usd": max_cost_usd,
            "allow_work": allow_work,
            "allow_shell": allow_shell,
            "max_attempt_cost_usd": ceiling,
            "attempt_cost_policy": policy,
            **selection,
            **({"review_check_policy": review_policy} if review_policy else {}),
        }
        return {**grant, "preview": grant_preview(grant), "stored": False}

    def authorize(self, work_id, **request) -> dict:
        preview = self.preview_authorization(**request)
        fields = {
            key: value
            for key, value in preview.items()
            if key not in {"preview", "stored"}
        }
        budget_seconds = fields.pop("budget_seconds")
        source_commit = self._head()
        with self._transaction() as db:
            card = self._load(db, work_id)
            if not self._agreed(card) or card["status"] != "active":
                raise ValueError(
                    "Only an active, exactly agreed plan may be authorized"
                )
            if any(
                item["state"] in _ACTIVE | {"recovery_required"}
                for item in self._attempts(db, work_id)
            ):
                raise ValueError(
                    "Stop and reconcile outstanding attempts before replacing authorization"
                )
            if fields.get("review_check_policy"):
                fields["review_check_policy"].update(
                    plan_revision=card["plan_revision"],
                    declarations={
                        spec["id"]: declaration_revision(spec)
                        for spec in card["plan"]["steps"]
                    },
                )
                prospective = {**card, "authorization": fields}
                for spec in card["plan"]["steps"]:
                    if spec.get("review_verification") or (
                        spec.get("review_requirements") or {}
                    ).get("requires_shell"):
                        selected_policy(prospective, spec)
            now = time.time()
            card["authorization"] = {
                "authorization_id": str(uuid4()),
                "plan_revision": card["plan_revision"],
                "authorized_at": now,
                "deadline": now + budget_seconds,
                "launches": 0,
                "used_cost_usd": 0.0,
                "unknown_cost": False,
                **fields,
                "source_commit": source_commit,
                "revoked_at": None,
            }
            return self._record(db, card, "authorized", "operator")

    def bind_review(self, attempt_id: str, review_id: str) -> dict:
        """Pin the immutable snapshot a review attempt reads; set once."""
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            self._require_open(self._load(db, attempt["work_id"]))
            if attempt["kind"] != "review":
                raise ValueError("Only review attempts read a pinned snapshot")
            if attempt.get("review_id") not in (None, review_id):
                raise ValueError("Review attempt is already bound to another snapshot")
            attempt["review_id"] = review_id
            self._save_attempt(db, attempt)
            return _public_attempt(attempt)

    def revoke(self, work_id) -> dict:
        with self._transaction() as db:
            card = self._load(db, work_id)
            self._require_open(card)
            if card["authorization"]:
                card["authorization"]["revoked_at"] = time.time()
            self._fence(db, card, "Operator revoked execution authorization")
            return self._record(db, card, "revoked", "operator")

    def _claim_reason(self, card, step, attempts):
        """The reservation gate, shared by execution and participant hints."""
        if card["status"] in _TERMINAL:
            return "work_terminal"
        if card["status"] == "paused":
            return "paused"
        transition = card.get("transition")
        if transition and transition["phase"] in TRANSITION_OPEN:
            return "transition_in_progress"
        if not self._agreed(card):
            return "plan_not_agreed"
        if self._unresolved(card, step):
            return "blocker_open"
        if any(
            item["state"] == "recovery_required"
            or (item["state"] in _ACTIVE and item["deadline"] <= time.time())
            for item in attempts
        ):
            return "recovery_required"
        if any(
            item["step_id"] == step["id"] and item["state"] in _ACTIVE
            for item in attempts
        ):
            return "another_claim_active"
        if any(
            self._step(card, dependency)["state"] != "accepted"
            for dependency in step["depends_on"]
        ):
            return "dependency_not_accepted"
        if card["status"] != "active" or step["state"] not in {
            "ready",
            "review",
            "changes_requested",
        }:
            return "step_not_ready"
        return None

    def _ready(self, db, card):
        attempts = self._attempts(
            db, card["work_id"], states=(*_ACTIVE, "recovery_required")
        )
        return [
            {
                "work_id": card["work_id"],
                "step_id": step["id"],
                "actor": step["reviewer"] if step["submission"] else step["owner"],
                "kind": "review" if step["submission"] else "implement",
            }
            for step in card["steps"]
            if self._claim_reason(card, step, attempts) is None
        ]

    @staticmethod
    def _review_phase_reason(bound, action):
        report = bound.get("independent_report")
        if action == "report":
            if bound.get("protocol") != "independent_first":
                return "legacy_disclosure"
            if report:
                return "report_already_recorded"
        elif action == "compare":
            if bound.get("clarification_request"):
                return "clarification_requires_new_snapshot"
            if not report or report["outcome"] != "success":
                return "successful_report_required"
            if bound.get("comparison_opened_at"):
                return "comparison_already_opened"
        elif action in {"accept", "reject"}:
            if bound.get("verdict"):
                return "verdict_already_recorded"
            if bound.get("protocol") == "independent_first":
                if not report:
                    return "independent_report_required"
                if action == "accept" and report["outcome"] != "success":
                    return "successful_report_required"
        return None

    def next_actions(
        self,
        current,
        *,
        actor,
        attempt_token=None,
        claims=None,
        origin=None,
        _db=None,
        _card=None,
    ):
        """Hints share the reservation/stage gates; they confer no authority."""
        if "plan" not in current or actor not in {"claude", "omp"}:
            return []
        with nullcontext(_db) if _db is not None else self._read_connection() as db:
            card = _card if _card is not None else self._load(db, current["work_id"])
            attempts = self._attempts(
                db, card["work_id"], states=(*_ACTIVE, "recovery_required")
            )
            bound = None
            if attempt_token:
                bound = self._credential(db, attempt_token, actor)
            actions = []

            def add(action, step, kind, credential=None, reason=None):
                submission = step["submission"] if step else None
                if card["revision"] != current["revision"]:
                    reason = "receipt_revision_stale"
                if card["status"] == "paused":
                    reason = "paused"
                if card["status"] in _TERMINAL:
                    reason = "work_terminal"
                required = ["operation_id"]
                if action in {"submit", "report", "accept", "reject"}:
                    required += ["note", "evidence"]
                if action == "submit" and credential and not credential["autonomous"]:
                    required += ["commit"]
                if action == "report":
                    required += ["resolution"]
                actions.append(
                    {
                        "action": action,
                        "step_id": step["id"] if step else None,
                        "kind": kind,
                        "stage": credential.get("review_stage") if credential else None,
                        "submission_id": submission["submission_id"]
                        if submission
                        else None,
                        "expected_revision": current["revision"],
                        "required_fields": required,
                        "allowed": reason is None,
                        "blocked_reason": reason,
                    }
                )

            if (
                bound is None
                and card["agreements"].get(actor, {}).get("plan_revision")
                != card["plan_revision"]
            ):
                add("agree", None, None)
            # Operator commands (transition steps, blocker resolution) are shown to
            # participants as discoverability only; none of them is allowed here.
            for item in self._operator_commands(card):
                if True:
                    actions.append(
                        {
                            "action": item.get("action", "transition"),
                            "step_id": None,
                            "kind": None,
                            "stage": None,
                            "submission_id": None,
                            "expected_revision": current["revision"],
                            "required_fields": [],
                            "allowed": False,
                            "blocked_reason": "operator_required",
                            "purpose": item["purpose"],
                            "command": item["command"],
                        }
                    )
            for step in card["steps"]:
                if bound and bound["step_id"] != step["id"]:
                    continue
                if step["state"] == "accepted":
                    continue
                kind = "review" if actor == step["reviewer"] else "implement"
                token = attempt_token or (claims or {}).get(
                    (card["work_id"], step["id"])
                )
                credential = None
                if token:
                    try:
                        credential = self._authenticate(db, token, actor)
                    except ValueError:
                        add("claim", step, kind, reason="capability_retired")
                if not credential:
                    if not token:
                        reason = self._claim_reason(card, step, attempts)
                        if reason is None and bool(step["submission"]) != (
                            kind == "review"
                        ):
                            reason = (
                                "awaiting_review"
                                if step["submission"]
                                else "submission_required"
                            )
                        add("claim", step, kind, reason=reason)
                    if step.get("attempt"):
                        # A live claim nobody here holds: show whether this caller
                        # could recover it and why not; the hint grants nothing.
                        code, live = self._recovery_target(db, card, step)
                        if live is not None and code != "recovery_claim_completed":
                            if code is None:
                                try:
                                    self._recovery_authorization(
                                        live, actor, origin, None
                                    )
                                except ValueError as error:
                                    code = str(error)
                            add("recover", step, live["kind"], reason=code)
                    continue
                reason = None
                if credential["step_id"] != step["id"] or credential["kind"] != kind:
                    reason = "foreign_attempt"
                elif self._unresolved(card, step):
                    reason = "blocker_open"
                if kind == "implement":
                    if credential.get("submission_intent"):
                        reason = "submission_intent_already_recorded"
                    add("submit", step, kind, credential, reason)
                    continue
                submission = step["submission"]
                if (
                    not submission
                    or credential["submission"]["submission_id"]
                    != submission["submission_id"]
                    or submission["plan_revision"] != card["plan_revision"]
                ):
                    reason = "foreign_submission"
                candidates = (
                    ["report"]
                    if credential.get("protocol") == "independent_first"
                    and not credential.get("independent_report")
                    else ["compare", "accept", "reject"]
                )
                for action in candidates:
                    if action == "report" and reason == "blocker_open":
                        reason = None
                    phase_reason = self._review_phase_reason(credential, action)
                    if action == "compare" and phase_reason in {
                        "legacy_disclosure",
                        "comparison_already_opened",
                    }:
                        continue
                    add(action, step, kind, credential, reason or phase_reason)
            return self._redact(db, actions)

    def ready(self, work_id: str | None = None) -> list[dict]:
        self._maintenance()
        with self._read_connection() as db:
            cards = (
                [self._load(db, work_id)]
                if work_id
                else [
                    self._load(db, row["work_id"])
                    for row in db.execute(
                        "SELECT work_id FROM work_cards ORDER BY work_id"
                    )
                ]
            )
            return [entry for card in cards for entry in self._ready(db, card)]

    def _dependencies(self, card, step):
        ordered = []
        visited = set()

        def visit(step_id):
            if step_id in visited:
                return
            visited.add(step_id)
            parent = self._step(card, step_id)
            for dependency in parent["depends_on"]:
                visit(dependency)
            if parent["submission"]:
                ordered.append({**parent["submission"], "step_id": step_id})

        for dependency in step["depends_on"]:
            visit(dependency)
        return ordered

    def _reserve(
        self,
        db,
        card,
        step_id,
        *,
        actor,
        kind,
        owner_id,
        autonomous,
        source_commit=None,
        binding=None,
    ):
        if (
            not owner_id
            or actor not in {"claude", "omp"}
            or kind not in {"implement", "review"}
        ):
            raise ValueError("Reservation requires valid owner, principal, and kind")
        eligible = {
            "work_id": card["work_id"],
            "step_id": step_id,
            "actor": actor,
            "kind": kind,
        }
        if eligible not in self._ready(db, card):
            raise WorkConflict("Step is not ready for this actor and attempt kind")
        observation = self._observe_repository(
            card["plan"], action="claim", required=True
        )
        card["repository_observation"] = observation
        if not autonomous:
            source_commit = observation["head"]
        grant = card["authorization"]
        spec = next(item for item in card["plan"]["steps"] if item["id"] == step_id)
        prefix = "review_" if kind == "review" else ""
        requirements = TaskRequirements.model_validate(
            spec.get(prefix + "requirements") or {}
        )
        selected_plan = spec.get(prefix + "verification")
        verification = verification_requirements(selected_plan or VerificationPlan())
        if kind == "review" and requirements.requires_write:
            raise ValueError(
                "Declared write requirement is incompatible with review role"
            )
        runner_policy = (
            selected_policy(card, spec)
            if autonomous
            and kind == "review"
            and (spec.get("review_verification") or requirements.requires_shell)
            else None
        )
        needs_shell = requirements.requires_shell or verification["requires_shell"]
        if (
            needs_shell
            and not runner_policy
            and (
                not autonomous or kind == "review" or not shell_permission(grant or {})
            )
        ):
            raise ValueError(
                "Declared shell requirement is not granted for this attempt"
            )
        if requirements.requires_write and (
            not autonomous or not (grant or {}).get("allow_work")
        ):
            raise ValueError(
                "Declared write requirement is not granted for this attempt"
            )
        if (
            autonomous
            and grant
            and verification["estimated_seconds"]
            > max(0, grant["deadline"] - time.time())
        ):
            raise ValueError(
                "Selected verification does not fit the remaining authorization time"
            )
        envelope = 0.0
        selection = {}
        if autonomous:
            if (
                not grant
                or grant["revoked_at"] is not None
                or grant["plan_revision"] != card["plan_revision"]
                or grant["deadline"] <= time.time()
            ):
                raise ValueError("Current operator authorization is required")
            selection = model_selection(grant)
            if (
                actor == "omp"
                and selection["model_provenance"]["omp"] == "legacy_unpinned"
            ):
                raise ValueError(
                    "Legacy OMP model was not pinned; reauthorize before launch"
                )
            if (
                grant["unknown_cost"]
                or grant["used_cost_usd"] >= grant["max_cost_usd"]
                or grant["launches"] >= grant["max_launches"]
            ):
                raise ValueError(
                    "Autonomous cost or launch budget is exhausted or unknown"
                )
            if kind == "implement" and not grant["allow_work"]:
                raise ValueError("Operator did not grant implementation permission")
            if kind == "review" and shell_permission(grant) and not runner_policy:
                step = self._step(card, step_id)
                if not self._shell_waiver(step, self._review_scope(card, step)):
                    raise ValueError(
                        "Review waiver applicability changed before reservation"
                    )
            active = [item for item in self._attempts(db) if item["state"] in _ACTIVE]
            if len(active) >= 4:
                raise ValueError(
                    "Project work concurrency is limited to four active attempts"
                )
            reserved = sum(
                item["reserved_cost_usd"]
                for item in active
                if item["authorization_id"] == grant["authorization_id"]
            )
            envelope = min(
                attempt_budget(grant)["max_attempt_cost_usd"],
                grant["max_cost_usd"] - grant["used_cost_usd"] - reserved,
            )
            if envelope <= 0:
                raise ValueError("No unreserved monetary budget remains")
            grant["launches"] += 1
        step = self._step(card, step_id)
        token = secrets.token_urlsafe(32)
        now = time.time()
        attempt = {
            "attempt_id": str(uuid4()),
            "token": token,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "work_id": card["work_id"],
            "step_id": step_id,
            "actor": actor,
            "kind": kind,
            "owner_id": owner_id,
            "plan_revision": card["plan_revision"],
            "requirements": requirements.model_dump(),
            "verification": selected_plan,
            "autonomous": autonomous,
            "authorization_id": grant["authorization_id"] if autonomous else None,
            "source_commit": grant["source_commit"] if autonomous else source_commit,
            "repository_observation": observation,
            "deadline": grant["deadline"] if autonomous else now + 3600,
            "created_at": now,
            "started_at": None,
            "heartbeat_at": None,
            "state": "reserved",
            "workspace": None,
            "native_task_id": None,
            "session_id": None,
            "allow_work": grant["allow_work"] if autonomous else False,
            "allow_shell": shell_permission(grant)
            if autonomous and kind != "review"
            else False,
            **(
                {
                    "protocol": "independent_first",
                    "review_stage": "independent",
                    "independent_report": None,
                    "comparison_opened_at": None,
                    "review_scope": self._review_scope(
                        card, step, autonomous=autonomous
                    ),
                    "shell_check_policy": "supervisor_checks_v1"
                    if runner_policy
                    else "blocked_no_stage_scoped_execution"
                    if autonomous and shell_permission(grant)
                    else "not_granted",
                }
                if kind == "review"
                else {}
            ),
            **selection,
            "remaining_cost_usd": envelope,
            "reserved_cost_usd": envelope,
            "submission": step["submission"] if kind == "review" else None,
            "checkpoint": step.get("checkpoint")
            if kind == "implement"
            and (step.get("checkpoint") or {}).get("plan_revision")
            == card["plan_revision"]
            else None,
            "dependencies": self._dependencies(card, step),
            "process_confirmed_gone": False,
            "verdict": None,
            "submission_intent": None,
            "block_intent": None,
            "cost_recorded": False,
            **({"binding": binding} if binding is not None else {}),
            **(
                {
                    "review_check_policy": {
                        **runner_policy,
                        "commit": step["submission"]["commit"],
                        "declaration_revision": declaration_revision(spec),
                    }
                }
                if runner_policy
                else {}
            ),
        }
        self._save_attempt(db, attempt)
        step.update(state="running", attempt=attempt["attempt_id"])
        return attempt

    SHELL_REVIEW_BLOCK = (
        "Independent-first review cannot execute the granted shell checks: no "
        "stage-scoped execution path exists"
    )

    @staticmethod
    def _review_scope(card, step, *, autonomous=True):
        return review_scope(
            {
                "work_id": card["work_id"],
                "step_id": step["id"],
                "plan_revision": card["plan_revision"],
                "submission": step["submission"],
                "authorization_id": (card.get("authorization") or {}).get(
                    "authorization_id"
                )
                if autonomous
                else None,
            },
            card["plan"],
        )

    @staticmethod
    def _shell_waiver(step, scope):
        return any(
            blocker.get("policy") == "shell_review"
            and blocker["actor"] == "operator"
            and blocker.get("resolved_at") is not None
            and blocker.get("resolved_by") == "operator"
            and any(
                resolution.get("actor") == "operator"
                and resolution.get("scope") == scope
                for resolution in blocker.get("resolution_history", [])
            )
            for blocker in step["blockers"]
        )

    def _block_shell_review(self, work_id, step_id):
        """Record an explicit pre-launch blocker instead of silently dropping shell."""
        with self._transaction() as db:
            card = self._load(db, work_id)
            self._require_open(card)
            grant = card["authorization"]
            if not grant or not shell_permission(grant):
                return None
            spec = next(item for item in card["plan"]["steps"] if item["id"] == step_id)
            if (
                spec.get("review_verification")
                or (spec.get("review_requirements") or {}).get("requires_shell")
            ) and selected_policy(card, spec):
                return None
            step = self._step(card, step_id)
            if step["submission"] is None:
                # No prospective review exists; reservation's readiness check
                # rejects this request without minting a policy decision.
                return None
            # Only the server-minted policy blocker counts: it carries a policy
            # marker no participant command can set, is owned by the operator, and
            # only an operator resolution waives it. A same-text blocker created
            # and resolved by a participant is ignored here.
            recorded = [
                blocker
                for blocker in step["blockers"]
                if blocker.get("policy") == "shell_review"
                and blocker["actor"] == "operator"
            ]
            if any(blocker["resolved_at"] is None for blocker in recorded):
                return (
                    "Review launch is blocked until the operator resolves the "
                    "recorded shell-check blocker"
                )
            scope = self._review_scope(card, step)
            if self._shell_waiver(step, scope):
                return None
            policy_blocker = self._blocker(
                "operator",
                self.SHELL_REVIEW_BLOCK,
                "Operator resolves the blocker after re-authorizing without "
                "--allow-shell, or after a stage-scoped check execution path exists",
                card["plan_revision"],
                step["id"],
            )
            policy_blocker["policy"] = "shell_review"
            policy_blocker["reason"] = (
                "applicability_review_required"
                if recorded
                else "shell_review_requires_operator"
            )
            step["blockers"].append(policy_blocker)
            self._record(
                db,
                card,
                "blocked",
                "operator",
                {
                    "step_id": step["id"],
                    "reason": policy_blocker["reason"],
                },
            )
            return (
                "Review launch blocked before start: "
                + self.SHELL_REVIEW_BLOCK
                + "; resolve the recorded blocker"
            )

    def reserve(
        self, work_id, step_id, *, actor, kind, owner_id, autonomous=True
    ) -> dict:
        self._maintenance()
        source_commit = None
        if autonomous and kind == "review":
            blocked = self._block_shell_review(work_id, step_id)
            if blocked:
                raise ValueError(blocked)
        with self._transaction() as db:
            card = self._load(db, work_id)
            attempt = self._reserve(
                db,
                card,
                step_id,
                actor=actor,
                kind=kind,
                owner_id=owner_id,
                autonomous=autonomous,
                source_commit=source_commit,
            )
            self._record(
                db,
                card,
                "reserved",
                "supervisor",
                {"attempt_id": attempt["attempt_id"]},
            )
            return attempt

    def attempt(self, attempt_id) -> dict:
        with self._read_connection() as db:
            return self._attempt(db, attempt_id)

    def authenticate(self, token) -> dict:
        with self._read_connection() as db:
            return self._authenticate(db, token)

    def started(
        self,
        attempt_id,
        *,
        workspace: str,
        native_task_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            self._authenticate(db, attempt["token"])
            if attempt["workspace"] is not None and attempt["workspace"] != workspace:
                raise ValueError("Attempt workspace is immutable")
            if (
                attempt["native_task_id"] is not None
                and native_task_id is not None
                and attempt["native_task_id"] != native_task_id
            ):
                raise ValueError("Attempt native task binding is immutable")
            attempt.update(
                state="running",
                started_at=attempt["started_at"] or time.time(),
                workspace=workspace,
                native_task_id=native_task_id or attempt["native_task_id"],
                session_id=session_id or attempt["session_id"],
            )
            self._save_attempt(db, attempt)
            self._record(
                db,
                self._load(db, attempt["work_id"]),
                "started",
                "supervisor",
                {"attempt_id": attempt_id},
            )

    def bind_native(self, attempt_id, task_id) -> None:
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            self._authenticate(db, attempt["token"])
            if attempt["native_task_id"] not in {None, task_id}:
                raise ValueError("Attempt native task binding is immutable")
            attempt["native_task_id"] = task_id
            self._save_attempt(db, attempt)

    def native_attempt(self, task_id) -> dict | None:
        with self._read_connection() as db:
            row = db.execute(
                "SELECT attempt FROM work_attempts WHERE native_task_id=?", (task_id,)
            ).fetchone()
            return json.loads(row["attempt"]) if row else None

    def _stage_attempts(self, db, task_id):
        """Disclosure bindings only; never return these as execution capabilities."""
        managed = db.execute(
            "SELECT attempt FROM work_attempts WHERE native_task_id=?", (task_id,)
        ).fetchone()
        if managed:
            return [json.loads(managed["attempt"])]
        return [
            json.loads(row["attempt"])
            for row in db.execute(
                "SELECT attempt FROM work_attempts WHERE state IN ('reserved','running') "
                "AND json_extract(attempt,'$.autonomous')=0 "
                "AND json_extract(attempt,'$.kind')='review' "
                "AND json_extract(attempt,'$.binding.task_id')=? ORDER BY attempt_id",
                (task_id,),
            )
        ]

    def stage_attempt(self, task_id) -> dict | None:
        """Most restrictive active review binding, without changing tool policy."""
        with self._read_connection() as db:
            attempts = self._stage_attempts(db, task_id)
            return next(
                (attempt for attempt in attempts if independent_stage(attempt)),
                attempts[0] if attempts else None,
            )

    def block_review_context(self, db, task_id, request):
        """Close clarification within the caller's task/question transaction."""
        for attempt in self._stage_attempts(db, task_id):
            self._block_review_context(db, attempt, request)

    def block_review_capture(self, attempt_id, reason, paths):
        """Persist a pre-dispatch capture failure without inventing worker effects."""
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            self._block_review_context(
                db,
                attempt,
                {
                    "clarification_requires_new_snapshot": True,
                    "reason_code": "review_capture_failed",
                    "reason": reason,
                    "requested_paths": paths,
                    "next_step": "Correct the declared context and create a new immutable snapshot/attempt.",
                },
            )

    def _block_review_context(self, db, attempt, request):
        if not independent_stage(attempt) or attempt.get("clarification_request"):
            return
        self._authenticate(db, attempt["token"])
        card = self._load(db, attempt["work_id"])
        step = self._step(card, attempt["step_id"])
        blocker = self._blocker(
            attempt["actor"],
            request["reason"],
            request["next_step"],
            attempt["plan_revision"],
            attempt["step_id"],
        )
        blocker["reason"] = request.get(
            "reason_code", "clarification_requires_new_snapshot"
        )
        step["blockers"].append(blocker)
        intent = attempt.get("block_intent") or {"blocker_ids": [], "at": time.time()}
        intent["blocker_ids"].append(blocker["blocker_id"])
        attempt.update(
            clarification_request=request,
            review_stage="blocked",
            block_intent=intent,
        )
        self._save_attempt(db, attempt)
        self._record(
            db,
            card,
            blocker["reason"],
            attempt["actor"],
            {"attempt_id": attempt["attempt_id"], "request": request},
        )

    def active_attempts(self, owner_id: str | None = None) -> list[dict]:
        with self._read_connection() as db:
            return [
                attempt
                for attempt in self._attempts(db)
                if attempt["state"] in _ACTIVE | {"recovery_required"}
                and (owner_id is None or attempt["owner_id"] == owner_id)
            ]

    def confirm_stopped(self, attempt_id) -> None:
        """Supervisor-only assertion after verified child exit, never based on text."""
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            attempt["process_confirmed_gone"] = True
            self._save_attempt(db, attempt)
            self._record(
                db,
                self._load(db, attempt["work_id"]),
                "process_stopped",
                "supervisor",
                {"attempt_id": attempt_id},
            )

    @staticmethod
    def _validate_output(card, step, attempt, output, answer, evidence):
        if (
            not output
            or any(
                not output.get(key)
                for key in ("commit", "base_commit", "workspace", "tree_hash")
            )
            or not evidence
            or not answer.strip()
        ):
            raise ValueError(
                "Implementation completion requires immutable output, answer, and evidence"
            )
        if attempt["workspace"] is None or output["workspace"] != attempt["workspace"]:
            raise ValueError(
                "Submission output must come from the bound attempt workspace"
            )
        if any(
            not re.fullmatch(r"[0-9a-f]{40,64}", output[key])
            for key in ("commit", "base_commit", "tree_hash")
        ):
            raise ValueError(
                "Submission commit and tree identities must be immutable Git object IDs"
            )
        intent = attempt["submission_intent"]
        if (
            not attempt["autonomous"]
            and intent
            and output["commit"] != intent["commit"]
        ):
            raise ValueError(
                "Committed output differs from immutable manual submission intent"
            )
        allowed = set(
            next(
                item["owned_files"]
                for item in card["plan"]["steps"]
                if item["id"] == step["id"]
            )
        )
        if (
            not isinstance(output.get("changed_files"), list)
            or set(output["changed_files"]) - allowed
        ):
            raise ValueError("Submission changed files exceed declared ownership")

    def finish_attempt(
        self,
        attempt_id,
        *,
        outcome: str,
        answer: str,
        evidence: list[str],
        output: dict | None,
        cost_usd: float | None,
        error: str | None = None,
    ) -> dict:
        if outcome not in {"success", "blocked", "failed", "interrupted"}:
            raise ValueError("Invalid attempt outcome")
        if cost_usd is not None and (
            isinstance(cost_usd, bool) or not math.isfinite(cost_usd) or cost_usd < 0
        ):
            raise ValueError("Cost must be finite and nonnegative, or unknown")
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            card = self._load(db, attempt["work_id"])
            fingerprint = hashlib.sha256(
                _json(
                    {
                        "outcome": outcome,
                        "answer": answer,
                        "evidence": evidence,
                        "output": output,
                        "cost_usd": cost_usd,
                        "error": error,
                    }
                ).encode()
            ).hexdigest()
            if attempt.get("finish_fingerprint"):
                if attempt["finish_fingerprint"] != fingerprint:
                    raise WorkConflict("Attempt completion is immutable")
                return self._view(db, card)
            self._require_open(card)
            if attempt["state"] not in _ACTIVE | {"recovery_required"}:
                raise ValueError("Attempt is already terminal")
            current = (
                attempt["state"] in _ACTIVE
                and attempt["deadline"] > time.time()
                and card["status"] != "paused"
                and attempt["plan_revision"] == card["plan_revision"]
            )
            if attempt["autonomous"]:
                grant = card["authorization"]
                current = (
                    current
                    and grant is not None
                    and grant["revoked_at"] is None
                    and grant["authorization_id"] == attempt["authorization_id"]
                )
                if grant and grant["authorization_id"] == attempt["authorization_id"]:
                    if cost_usd is None:
                        grant["unknown_cost"] = True
                    else:
                        grant["used_cost_usd"] += cost_usd
            attempt.update(
                finish_fingerprint=fingerprint,
                outcome=outcome,
                answer=answer,
                evidence=evidence,
                output=output,
                cost_usd=cost_usd,
                cost_recorded=True,
                error=error or attempt.get("error"),
                finished_at=time.time(),
            )
            step = next(
                (item for item in card["steps"] if item["id"] == attempt["step_id"]),
                None,
            )
            unresolved = self._unresolved(card, step)
            cooperative_block = (
                current
                and outcome == "blocked"
                and attempt.get("block_intent")
                and any(
                    blocker["blocker_id"] in attempt["block_intent"]["blocker_ids"]
                    for blocker in unresolved
                )
                and (
                    not attempt["autonomous"]
                    or (cost_usd is not None and attempt["process_confirmed_gone"])
                )
                and (attempt["kind"] == "review" or output is not None)
            )
            if (
                current
                and outcome == "success"
                and attempt["kind"] == "implement"
                and not unresolved
            ):
                self._validate_output(card, step, attempt, output, answer, evidence)
                submission = {
                    **output,
                    "submission_id": str(uuid4()),
                    "attempt_id": attempt_id,
                    "plan_revision": card["plan_revision"],
                    "evidence": evidence,
                    "answer": answer,
                    "submitted_at": time.time(),
                }
                step.update(
                    submission=submission,
                    checkpoint=None,
                    acceptance=None,
                    state="review",
                )
                attempt["state"] = "succeeded"
            elif cooperative_block:
                if attempt["kind"] == "implement":
                    self._validate_output(card, step, attempt, output, answer, evidence)
                    step["checkpoint"] = {
                        **output,
                        "checkpoint_id": str(uuid4()),
                        "attempt_id": attempt_id,
                        "step_id": step["id"],
                        "plan_revision": card["plan_revision"],
                        "evidence": evidence,
                        "answer": answer,
                        "created_at": time.time(),
                    }
                attempt["state"] = "blocked"
                step["state"] = "blocked"
                step["acceptance"] = None
            elif (
                current
                and outcome == "success"
                and attempt["kind"] == "review"
                and attempt["verdict"]
                and not unresolved
            ):
                self._review_checks.require_finalization(db, attempt)
                attempt["state"] = "succeeded"
                verdict = attempt["verdict"]
                step["acceptance"] = verdict if verdict["verdict"] == "accept" else None
                step["state"] = (
                    "accepted"
                    if verdict["verdict"] == "accept"
                    else "changes_requested"
                )
                if verdict["verdict"] == "reject":
                    step["checkpoint"] = {
                        **step["submission"],
                        "checkpoint_id": str(uuid4()),
                        "step_id": step["id"],
                        "plan_revision": card["plan_revision"],
                        "created_at": time.time(),
                    }
                    step["submission"] = None
            else:
                attempt["state"] = "recovery_required"
                attempt["error"] = (
                    error
                    or "Outcome is uncertain or lacks explicit reviewer verdict; side effects may exist"
                )
                if step:
                    step["state"] = "recovery_required"
                    # A process failure after a verdict cannot certify a completed step.
                    step["acceptance"] = None
            self._save_attempt(db, attempt)
            if attempt["autonomous"] and cost_usd is None:
                card["status"] = "paused"
                self._fence(
                    db,
                    card,
                    "Unknown provider usage; operator must reconcile budget before continuation",
                )
            return self._record(
                db,
                card,
                "attempt_finished",
                "supervisor",
                {
                    "attempt_id": attempt_id,
                    "outcome": outcome,
                    "output": output,
                    "evidence": evidence,
                    "error": attempt["error"],
                },
            )

    # ---- plan transitions -------------------------------------------------

    def _transition_inventory(self, db, card):
        """Every attempt whose execution or uncertainty an activation would retire."""
        return [
            {
                "attempt_id": attempt["attempt_id"],
                "step_id": attempt["step_id"],
                "actor": attempt["actor"],
                "kind": attempt["kind"],
                "autonomous": attempt["autonomous"],
                "state": attempt["state"],
            }
            for attempt in self._attempts(db, card["work_id"])
            if attempt["state"] in _ACTIVE | {"recovery_required"}
        ]

    @staticmethod
    def _proposal_preview(card, plan, inventory):
        before = {step["id"]: step for step in card["plan"]["steps"]}
        after = {step["id"]: step for step in plan["steps"]}
        plan_level = any(
            card["plan"].get(key) != plan.get(key)
            for key in ("title", "goal", "constraints", "acceptance", "context")
        )
        return {
            "card_wide": True,
            "statement": (
                "Activation retires every current attempt of this card, including "
                "attempts on unchanged steps; stopped work is preserved, never replayed."
            ),
            "attempts": inventory,
            "removed_steps": sorted(before.keys() - after.keys()),
            "added_steps": sorted(after.keys() - before.keys()),
            "changed_steps": sorted(
                identifier
                for identifier in before.keys() & after.keys()
                if before[identifier] != after[identifier]
            ),
            "plan_level_changes": plan_level,
        }

    def _activate_plan(self, db, card, plan):
        """Install a plan as the next draft revision; callers ensure quiescence."""
        self._fence(db, card, "Plan changed; old attempt cannot publish")
        previous = {step["id"]: step for step in card["steps"]}
        old_revision = card["plan_revision"]
        card.update(
            plan=plan,
            plan_revision=card["plan_revision"] + 1,
            agreements={},
            steps=self._new_steps(plan),
        )
        current = {step["id"]: step for step in card["steps"]}
        for old in [None, *previous.values()]:
            blockers = old["blockers"] if old else list(card["blockers"])
            target = current.get(old["id"]) if old else None
            for blocker in blockers:
                blocker["carried_from"].append(
                    {
                        "plan_revision": old_revision,
                        "step_id": old["id"] if old else None,
                        "to_plan_revision": card["plan_revision"],
                        "step_id_after": target["id"] if target else None,
                    }
                )
            if old:
                (target["blockers"] if target else card["blockers"]).extend(blockers)
            if target and old["state"] == "recovery_required":
                target.update(state="recovery_required", attempt=old["attempt"])
        if card["authorization"]:
            card["authorization"]["revoked_at"] = time.time()

    def _operator_command(self, work_id, *arguments):
        parts = [
            shlex.quote(sys.executable),
            "-m",
            "omp_tandem.work_daemon",
            "--project-root",
            shlex.quote(str(self.scope.root)),
            "--state-dir",
            shlex.quote(str(self.scope.base)),
            *arguments,
        ]
        return " ".join(parts)

    def _operator_commands(self, card):
        """Exact operator commands for the current negotiation state; hints only.

        Every mutating command names the exact proposal or transition it acts on
        and the card revision the operator observed, so a retained command cannot
        act on a replacement identity or a moved card.
        """
        if card["status"] in _TERMINAL:
            return []
        work_id = card["work_id"]
        transition = card.get("transition")
        proposal = card.get("proposal")
        revision = ["--expected-revision", str(card["revision"])]
        commands = []
        # Blockers name their CURRENT location (card-level or the step that holds
        # them now), never their historical origin, so the command resolves the
        # blocker where the store actually keeps it. Resolving a blocker does not
        # resume a paused card or authorize execution.
        for step_id, blockers in (
            (None, card["blockers"]),
            *((step["id"], step["blockers"]) for step in card["steps"]),
        ):
            for blocker in blockers:
                if blocker["resolved_at"] is not None:
                    continue
                commands.append(
                    {
                        "action": "unblock",
                        "purpose": (
                            f"resolve blocker {blocker['blocker_id']} "
                            f"({'card-level' if step_id is None else 'step ' + step_id}; "
                            f"condition: {blocker['condition']}) with evidence, or mark it not_applicable with a reason"
                        ),
                        "blocker_id": blocker["blocker_id"],
                        "step_id": step_id,
                        "command": self._operator_command(
                            work_id,
                            "unblock",
                            work_id,
                            "--blocker",
                            blocker["blocker_id"],
                            *(("--step", step_id) if step_id else ()),
                            *revision,
                            "--resolution",
                            "resolved",
                            "--note",
                            "'…'",
                            "--evidence",
                            "'…'",
                        ),
                    }
                )
        if transition and transition["phase"] in TRANSITION_OPEN:
            bound = ["--transition", transition["transition_id"]]
            commands.append(
                {
                    "purpose": "inspect the begun transition",
                    "command": self._operator_command(
                        work_id, "transition", work_id, "inspect"
                    ),
                }
            )
            for attempt_id, entry in transition["inventory"].items():
                if entry.get("disposition"):
                    continue
                flags = [
                    *bound,
                    *revision,
                    "--attempt",
                    attempt_id,
                    "--note",
                    "'…'",
                    "--evidence",
                    "'…'",
                ]
                if not entry["autonomous"]:
                    flags.append("--confirm-stopped")
                commands.append(
                    {
                        "purpose": (
                            "attest the manual stop and dispose the attempt"
                            if not entry["autonomous"]
                            else "dispose the attempt after the supervisor confirmed its stop"
                        ),
                        "attempt_id": attempt_id,
                        "command": self._operator_command(
                            work_id, "transition", work_id, "resolve", *flags
                        ),
                    }
                )
            if all(
                entry.get("disposition") for entry in transition["inventory"].values()
            ):
                acknowledgments = [
                    flag
                    for attempt_id, entry in transition["inventory"].items()
                    if (entry.get("saved") or {}).get("capture_failure")
                    and (entry.get("disposition") or {}).get("kind") == "superseded"
                    for flag in ("--acknowledge-capture-failure", attempt_id)
                ]
                commands.append(
                    {
                        "purpose": "activate the frozen proposal as the next draft revision"
                        + (
                            " (acknowledging the listed capture failures: their saved work is lost)"
                            if acknowledgments
                            else ""
                        ),
                        "command": self._operator_command(
                            work_id,
                            "transition",
                            work_id,
                            "activate",
                            *bound,
                            *revision,
                            *acknowledgments,
                            "--note",
                            "'…'",
                        ),
                    }
                )
            commands.append(
                {
                    "purpose": "withdraw the proposal (fenced attempts still need reconciliation)",
                    "command": self._operator_command(
                        work_id,
                        "transition",
                        work_id,
                        "withdraw",
                        *bound,
                        *revision,
                        "--note",
                        "'…'",
                    ),
                }
            )
        elif proposal:
            bound = ["--proposal", proposal["proposal_id"]]
            affected = (proposal.get("preview") or {}).get("attempts") or []
            commands.append(
                {
                    "purpose": "begin the transition: freeze the proposal, fence and inventory every attempt",
                    "command": self._operator_command(
                        work_id,
                        "transition",
                        work_id,
                        "begin",
                        *bound,
                        *revision,
                        "--note",
                        "'…'",
                    ),
                }
            )
            if not affected:
                commands.append(
                    {
                        "purpose": "nothing executes: freeze and activate the proposal in one operation",
                        "command": self._operator_command(
                            work_id,
                            "transition",
                            work_id,
                            "activate",
                            *bound,
                            *revision,
                            "--note",
                            "'…'",
                        ),
                    }
                )
            commands.append(
                {
                    "purpose": "withdraw the pending proposal without touching execution",
                    "command": self._operator_command(
                        work_id,
                        "transition",
                        work_id,
                        "withdraw",
                        *bound,
                        *revision,
                        "--note",
                        "'…'",
                    ),
                }
            )
            commands.append(
                {
                    "purpose": "inspect the pending proposal and its impact",
                    "command": self._operator_command(
                        work_id, "transition", work_id, "inspect"
                    ),
                }
            )
        return commands

    @staticmethod
    def _transition_request(operation, **request):
        """Canonical fingerprint of one exact operator command (verb and every argument)."""
        return hashlib.sha256(
            _json({"operation": operation, "request": request}).encode("utf-8")
        ).hexdigest()

    def _transition_operation(self, card, operation_id, fingerprint):
        """Durable operation identity: only the exact same command replays its outcome.

        A different command under a used identity is refused instead of being
        acknowledged with someone else's receipt.
        """
        if not operation_id:
            return None
        record = (card.get("transition_operations") or {}).get(operation_id)
        if record is None:
            return None
        if record["fingerprint"] != fingerprint:
            raise WorkConflict("Operation ID was already used for a different command")
        return record

    def _record_transition_operation(
        self, card, operation_id, operation, fingerprint, outcome
    ):
        """Store verb, exact-request fingerprint and the identities the command produced."""
        if operation_id:
            card.setdefault("transition_operations", {})[operation_id] = {
                "operation": operation,
                "fingerprint": fingerprint,
                "revision": card["revision"] + 1,
                "at": time.time(),
                "outcome": outcome,
            }

    @staticmethod
    def _check_expected_revision(card, expected_revision):
        if expected_revision is not None and expected_revision != card["revision"]:
            raise WorkConflict(
                f"Expected revision {expected_revision}; current revision is {card['revision']}"
            )

    def _transition_next_action(self, card):
        transition = card["transition"]
        pending = [
            attempt_id
            for attempt_id, entry in transition["inventory"].items()
            if not entry.get("disposition")
        ]
        if pending:
            return (
                f"Plan transition in progress: {len(pending)} attempt(s) still need stop "
                "evidence and an operator disposition; no claims until activation."
            )
        return "Plan transition ready: every attempt is disposed; the operator may activate the frozen proposal."

    def _require_operator(self, actor):
        if actor != "operator":
            raise ValueError("Only the trusted operator drives plan transitions")

    def transition_inspect(self, work_id) -> dict:
        with self._read_connection() as db:
            card = self._load(db, work_id)
            attempts = {
                attempt["attempt_id"]: attempt
                for attempt in self._attempts(db, card["work_id"])
            }
            return self._redact(db, self._inspect_view(card, attempts))

    def _inspect_view(self, card, attempts):
        work_id = card["work_id"]
        transition = card.get("transition")
        inventory = []
        for attempt_id, entry in (transition or {}).get("inventory", {}).items():
            attempt = attempts.get(attempt_id) or {}
            inventory.append(
                {
                    **entry,
                    "attempt_id": attempt_id,
                    "current_state": attempt.get("state"),
                    "process_confirmed_gone": attempt.get("process_confirmed_gone"),
                    "required": None
                    if entry.get("disposition")
                    else (
                        "supervisor teardown confirmation, then operator disposition"
                        if entry["autonomous"]
                        and not attempt.get("process_confirmed_gone")
                        else "operator disposition (--note/--evidence)"
                        if entry["autonomous"]
                        else "operator attestation of the manual stop (--confirm-stopped) and disposition"
                    ),
                    "inspect": [
                        item
                        for item in (
                            attempt.get("workspace")
                            and f"workspace {attempt['workspace']}",
                            attempt.get("submission_intent")
                            and "recorded submission intent",
                            attempt.get("independent_report") and "independent report",
                            attempt.get("verdict") and "recorded verdict",
                        )
                        if item
                    ],
                }
            )
        return {
            "work_id": work_id,
            "revision": card["revision"],
            "plan_revision": card["plan_revision"],
            "status": card["status"],
            "proposal": _public_proposal(card.get("proposal")),
            "transition": {**transition, "inventory": inventory}
            if transition
            else None,
            "transition_history": card.get("transition_history") or [],
            "activation_preview": self._activation_preview(card),
            "commands": self._operator_commands(card),
        }

    @staticmethod
    def _provenance_target(root, work_id):
        # A foreign root is an operator assertion, not a filesystem/read capability.
        if (
            not isinstance(root, str)
            or not Path(root).is_absolute()
            or any(part in {".", "..", ""} for part in root.split("/")[1:])
            or any(ord(char) < 32 or ord(char) == 127 for char in root)
        ):
            raise ValueError(
                "Link root requires an exact absolute path without traversal"
            )
        if not isinstance(work_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", work_id
        ):
            raise ValueError("Link requires an exact work ID")
        return {
            "project_root": root,
            "work_id": work_id,
            "target_verification": "not_performed",
            "reciprocal_link": "unverified",
        }

    def cancel(
        self,
        work_id,
        *,
        disposition,
        note,
        evidence,
        expected_revision,
        operation_id,
        continuation_root=None,
        continuation_work_id=None,
        actor="operator",
    ) -> dict:
        """Truthful terminal closure, never a verdict or a stop attestation."""
        self._require_operator(actor)
        self._validate_provenance_command(
            note, evidence, expected_revision, operation_id
        )
        if disposition not in {"cancelled", "superseded"}:
            raise ValueError("Cancellation disposition must be cancelled or superseded")
        continuation = None
        if continuation_root is not None or continuation_work_id is not None:
            continuation = self._provenance_target(
                continuation_root, continuation_work_id
            )
            if (
                continuation_root == str(self.scope.root)
                and continuation_work_id == work_id
            ):
                raise ValueError("A card cannot continue in itself")
        fingerprint = self._transition_request(
            "cancel",
            work_id=work_id,
            disposition=disposition,
            note=note,
            evidence=evidence,
            expected_revision=expected_revision,
            continuation_root=continuation_root,
            continuation_work_id=continuation_work_id,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            self._require_open(card)
            if self._transition_inventory(db, card):
                raise ValueError(
                    "cancel_requires_disposition: stop and reconcile every active or "
                    "recovery-required attempt before cancellation"
                )
            transition = card.get("transition")
            if (
                transition
                and transition["phase"] in TRANSITION_OPEN
                and any(
                    not entry.get("disposition")
                    for entry in transition["inventory"].values()
                )
            ):
                raise ValueError(
                    "cancel_requires_disposition: resolve the frozen transition inventory"
                )
            closure = {
                "disposition": disposition,
                "note": note,
                "evidence": list(evidence),
                "actor": actor,
                "at": time.time(),
                "continuation": continuation,
                "revision": card["revision"] + 1,
                "withdrawn_proposal_id": (card.get("proposal") or {}).get(
                    "proposal_id"
                ),
            }
            if transition and transition["phase"] in TRANSITION_OPEN:
                transition.update(
                    phase="cancelled",
                    cancelled_at=closure["at"],
                    closure_revision=closure["revision"],
                )
                card.setdefault("transition_history", []).append(transition)
            card["transition"] = None
            card["proposal"] = None
            card["closure"] = closure
            card["status"] = "cancelled"
            if card["authorization"]:
                card["authorization"]["revoked_at"] = time.time()
            self._record_transition_operation(
                card, operation_id, "cancel", fingerprint, closure
            )
            return self._record(db, card, "cancelled", actor, closure)

    @staticmethod
    def _validate_provenance_command(note, evidence, expected_revision, operation_id):
        if (
            not isinstance(note, str)
            or not note.strip()
            or len(note) > 16000
            or not isinstance(evidence, list)
            or not evidence
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 16000
                for item in evidence
            )
            or type(expected_revision) is not int
            or expected_revision < 1
            or not isinstance(operation_id, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", operation_id)
        ):
            raise ValueError(
                "Operator provenance requires note, evidence, expected revision and exact operation ID"
            )

    def link(
        self,
        work_id,
        *,
        predecessor_root,
        predecessor_work_id,
        note,
        evidence,
        expected_revision,
        operation_id,
        actor="operator",
    ) -> dict:
        """Append predecessor provenance in THIS scope, including after terminal closure."""
        self._require_operator(actor)
        self._validate_provenance_command(
            note, evidence, expected_revision, operation_id
        )
        predecessor = self._provenance_target(predecessor_root, predecessor_work_id)
        if predecessor_root == str(self.scope.root) and predecessor_work_id == work_id:
            raise ValueError("A card cannot be its own predecessor")
        fingerprint = self._transition_request(
            "link",
            work_id=work_id,
            predecessor_root=predecessor_root,
            predecessor_work_id=predecessor_work_id,
            note=note,
            evidence=evidence,
            expected_revision=expected_revision,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            if any(
                item["project_root"] == predecessor_root
                and item["work_id"] == predecessor_work_id
                for item in card.get("predecessors") or []
            ):
                raise WorkConflict("Predecessor provenance is already recorded")
            record = {
                **predecessor,
                "note": note,
                "evidence": list(evidence),
                "actor": actor,
                "at": time.time(),
            }
            card.setdefault("predecessors", []).append(record)
            self._record_transition_operation(
                card, operation_id, "link", fingerprint, record
            )
            return self._record(db, card, "predecessor_linked", actor, record)

    def transition_begin(
        self,
        work_id,
        *,
        note,
        proposal_id,
        expected_revision=None,
        operation_id=None,
        actor="operator",
    ) -> dict:
        self._require_operator(actor)
        if not note:
            raise ValueError("Begin requires a note")
        fingerprint = self._transition_request(
            "begin",
            work_id=work_id,
            note=note,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            self._require_open(card)
            proposal = card.get("proposal")
            if not proposal:
                raise ValueError("no_pending_proposal")
            if not proposal_id or proposal_id != proposal["proposal_id"]:
                raise ValueError("proposal_mismatch")
            if proposal["base_plan_revision"] != card["plan_revision"]:
                raise ValueError("proposal_stale")
            transition = card.get("transition")
            if transition and transition["phase"] in TRANSITION_OPEN:
                raise ValueError("transition_in_progress")
            inventory = self._transition_inventory(db, card)
            card["transition"] = {
                "transition_id": str(uuid4()),
                "proposal_id": proposal["proposal_id"],
                "base_plan_revision": proposal["base_plan_revision"],
                "phase": "ready" if not inventory else "stopping",
                "begun_at": time.time(),
                "note": note,
                "inventory": {
                    item["attempt_id"]: {
                        **{k: v for k, v in item.items() if k != "attempt_id"},
                        "state_at_begin": item["state"],
                        "stop": None,
                        "saved": None,
                        "disposition": None,
                    }
                    for item in inventory
                },
                "continuation": {},
            }
            self._fence(db, card, "Plan transition begun; old attempt cannot publish")
            self._record_transition_operation(
                card,
                operation_id,
                "begin",
                fingerprint,
                {
                    "transition_id": card["transition"]["transition_id"],
                    "proposal_id": proposal["proposal_id"],
                    "phase": card["transition"]["phase"],
                    "attempt_ids": [item["attempt_id"] for item in inventory],
                },
            )
            return self._record(
                db,
                card,
                "transition_begun",
                actor,
                {
                    "note": note,
                    "transition_id": card["transition"]["transition_id"],
                    "proposal_id": proposal["proposal_id"],
                    "attempt_ids": [item["attempt_id"] for item in inventory],
                },
            )

    def transition_resolve(
        self,
        work_id,
        attempt_id,
        *,
        note,
        evidence,
        confirm_stopped=False,
        abandon=False,
        saved_commit=None,
        transition_id=None,
        expected_revision=None,
        operation_id=None,
        actor="operator",
        workspaces=None,
    ) -> dict:
        self._require_operator(actor)
        if not note or not evidence:
            raise ValueError("Resolve requires note and evidence")
        fingerprint = self._transition_request(
            "resolve",
            work_id=work_id,
            attempt_id=attempt_id,
            note=note,
            evidence=list(evidence),
            confirm_stopped=bool(confirm_stopped),
            abandon=bool(abandon),
            saved_commit=saved_commit,
            transition_id=transition_id,
            expected_revision=expected_revision,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            self._require_open(card)
            transition = card.get("transition")
            if not transition or transition["phase"] not in TRANSITION_OPEN:
                raise ValueError("transition_not_begun")
            if not transition_id or transition_id != transition["transition_id"]:
                raise ValueError("transition_mismatch")
            entry = transition["inventory"].get(attempt_id)
            if entry is None:
                raise ValueError("attempt_not_in_inventory")
            if entry.get("disposition"):
                raise ValueError("attempt_already_disposed")
            attempt = self._attempt(db, attempt_id)
            if attempt["state"] in _ACTIVE:
                # Fenced at begin; a still-active row means a concurrent revival,
                # which this operator path never reconciles silently.
                raise ValueError("stop_unconfirmed")
            if attempt["autonomous"]:
                if not attempt.get("process_confirmed_gone"):
                    raise ValueError(
                        "stop_unconfirmed: the supervisor has not confirmed teardown of "
                        f"managed attempt {attempt_id}; wait for its evidence"
                    )
                stop = {"source": "supervisor_confirmed", "at": time.time()}
            else:
                if not confirm_stopped:
                    raise ValueError(
                        "attestation_required: manual attempts need --confirm-stopped as "
                        "the operator's explicit statement that the execution stopped"
                    )
                attempt["process_confirmed_gone"] = True
                stop = {"source": "operator_attested", "at": time.time()}
            step = self._step(card, attempt["step_id"])
            saved = {
                "workspace": attempt.get("workspace"),
                "output": attempt.get("output"),
                "checkpoint": step.get("checkpoint")
                if (step.get("checkpoint") or {}).get("attempt_id") == attempt_id
                else None,
                "submission_intent": attempt.get("submission_intent"),
                "independent_report": attempt.get("independent_report"),
                "verdict": attempt.get("verdict"),
                "preserved": None,
                "capture_failure": None,
            }
            owned_files = list(
                next(
                    item["owned_files"]
                    for item in card["plan"]["steps"]
                    if item["id"] == attempt["step_id"]
                )
            )
            if saved_commit is not None:
                # An operator-supplied commit is input, not observation: a wrong
                # commit is refused loudly instead of being recorded as a failure.
                if attempt["kind"] != "implement" or attempt["autonomous"]:
                    raise ValueError(
                        "A saved commit applies to manual implementation attempts only; "
                        "managed attempts preserve their own workspace"
                    )
                if workspaces is None:
                    raise ValueError(
                        "Preserving a saved commit requires the workspace layer"
                    )
                preserved = workspaces.adopt_submission(
                    attempt, card["plan"], saved_commit
                )
                saved["preserved"] = {
                    **preserved,
                    "validated_against_plan_revision": card["plan_revision"],
                    "owned_files": owned_files,
                }
            elif (
                attempt["kind"] == "implement"
                and attempt["autonomous"]
                and attempt.get("workspace")
                and workspaces is not None
            ):
                try:
                    preserved = workspaces.preserve(attempt, card["plan"])
                    saved["preserved"] = {
                        **preserved,
                        "validated_against_plan_revision": card["plan_revision"],
                        "owned_files": owned_files,
                    }
                except (ValueError, OSError) as error:
                    saved["capture_failure"] = f"{type(error).__name__}: {error}"
            if (
                attempt["autonomous"]
                and not attempt["cost_recorded"]
                and card["authorization"]
                and card["authorization"]["authorization_id"]
                == attempt["authorization_id"]
            ):
                card["authorization"]["unknown_cost"] = True
                card["status"] = "paused"
            attempt.update(
                state="reconciled",
                reconciliation={
                    "resolution": "abandon" if abandon else "retry",
                    "note": note,
                    "evidence": evidence,
                    "at": time.time(),
                    "transition_id": transition["transition_id"],
                    # One decision, one word: the attempt record, the transition
                    # inventory and the operation receipt must agree.
                    "disposition": "abandoned" if abandon else "superseded",
                },
            )
            self._save_attempt(db, attempt)
            if step["attempt"] == attempt_id:
                step["attempt"] = None
                step["state"] = "todo"
            if abandon:
                step["blockers"].append(
                    self._blocker(
                        actor,
                        note,
                        "Operator must explicitly resolve abandonment before continuing",
                        card["plan_revision"],
                        step["id"],
                    )
                )
                # Same gate as operator reconciliation: abandonment pauses the card.
                card["status"] = "paused"
            self._record_transition_operation(
                card,
                operation_id,
                "resolve",
                fingerprint,
                {
                    "transition_id": transition["transition_id"],
                    "attempt_id": attempt_id,
                    "disposition": "abandoned" if abandon else "superseded",
                    "stop": stop.get("source"),
                    "preserved_commit": (saved.get("preserved") or {}).get("commit"),
                    "status": card["status"],
                },
            )
            entry.update(
                stop=stop,
                saved=saved,
                disposition={
                    "kind": "abandoned" if abandon else "superseded",
                    "note": note,
                    "evidence": evidence,
                    "at": time.time(),
                    "by": actor,
                },
            )
            if all(
                item.get("disposition") for item in transition["inventory"].values()
            ):
                transition["phase"] = "ready"
            return self._record(
                db,
                card,
                "transition_resolved",
                actor,
                {
                    "note": note,
                    "evidence": evidence,
                    "transition_id": transition["transition_id"],
                    "attempt_id": attempt_id,
                    "step_id": step["id"],
                    "stop": stop,
                    "disposition": entry["disposition"]["kind"],
                    "preserved": bool(saved["preserved"]),
                    "capture_failure": saved["capture_failure"],
                },
            )

    @staticmethod
    def _continuation_outcome(entry, plan_steps):
        """How one inventory attempt's saved work would continue under a plan.

        Transfer eligibility only: `checkpoint` (validated saved bytes fit the new
        ownership), `not_transferable` (step removed or ownership excludes saved
        files) or `not_available` (no validated saved bytes, including a capture
        failure). None of these gates launch readiness by itself.
        """
        disposition = entry.get("disposition")
        saved = entry.get("saved") or {}
        preserved = saved.get("preserved")
        if not disposition:
            return {"status": "pending", "reason": "attempt not yet disposed"}
        if disposition["kind"] != "superseded":
            return {
                "status": "none",
                "reason": f"disposition {disposition['kind']} keeps no continuation",
            }
        if not preserved:
            return {
                "status": "not_available",
                "reason": saved.get("capture_failure") or "no validated saved bytes",
                "capture_failure": saved.get("capture_failure"),
            }
        spec = plan_steps.get(entry["step_id"])
        if spec is None:
            return {
                "status": "not_transferable",
                "reason": f"step {entry['step_id']} is not part of the new plan",
                "commit": preserved["commit"],
            }
        outside = sorted(set(preserved["changed_files"]) - set(spec["owned_files"]))
        if outside:
            return {
                "status": "not_transferable",
                "reason": f"preserved changes outside the new ownership: {outside}",
                "commit": preserved["commit"],
            }
        return {
            "status": "checkpoint",
            "step_id": entry["step_id"],
            "commit": preserved["commit"],
        }

    def _activation_preview(self, card):
        """Read-only view of what activation would carry over; nothing is mutated.

        Derived from the frozen proposal and the current inventory so the operator
        sees what was preserved, what failed and which new steps would start
        without a checkpoint before deciding. It describes transfer eligibility,
        not launch readiness: pauses, blockers, agreements and authorization stay
        separate gates.
        """
        transition = card.get("transition")
        proposal = card.get("proposal")
        if not (transition and transition["phase"] in TRANSITION_OPEN and proposal):
            return None
        plan_steps = {step["id"]: step for step in proposal["plan"]["steps"]}
        attempts = {}
        with_checkpoint = set()
        capture_failures = []
        pending = []
        for attempt_id, entry in transition["inventory"].items():
            outcome = self._continuation_outcome(entry, plan_steps)
            saved = entry.get("saved") or {}
            preserved = saved.get("preserved")
            attempts[attempt_id] = {
                "step_id": entry["step_id"],
                "actor": entry["actor"],
                "kind": entry["kind"],
                "autonomous": entry["autonomous"],
                "disposition": (entry.get("disposition") or {}).get("kind"),
                "stop": (entry.get("stop") or {}).get("source"),
                "preserved": {
                    "commit": preserved["commit"],
                    "changed_files": preserved["changed_files"],
                }
                if preserved
                else None,
                "capture_failure": saved.get("capture_failure"),
                "continuation": outcome,
            }
            if outcome["status"] == "checkpoint":
                with_checkpoint.add(entry["step_id"])
            if outcome["status"] == "pending":
                pending.append(attempt_id)
            if (
                outcome["status"] == "not_available"
                and saved.get("capture_failure")
                and (entry.get("disposition") or {}).get("kind") == "superseded"
            ):
                capture_failures.append(attempt_id)
        acknowledged = sorted(transition.get("acknowledged_capture_failures") or [])
        # A step whose attempt is not yet disposed may still yield a checkpoint;
        # it is undetermined, not "without checkpoint", until the disposition lands.
        undetermined = {
            transition["inventory"][attempt_id]["step_id"]
            for attempt_id in pending
            if transition["inventory"][attempt_id]["step_id"] in plan_steps
        } - with_checkpoint
        return {
            "proposal_id": proposal["proposal_id"],
            "transition_id": transition["transition_id"],
            "plan_revision_after": card["plan_revision"] + 1,
            "ready": not pending,
            "pending_dispositions": pending,
            "attempts": attempts,
            "steps_with_checkpoint": sorted(with_checkpoint),
            "steps_undetermined": sorted(undetermined),
            "steps_without_checkpoint": sorted(
                step_id
                for step_id in plan_steps
                if step_id not in with_checkpoint and step_id not in undetermined
            ),
            "capture_failures": sorted(capture_failures),
            "capture_failures_unacknowledged": sorted(
                set(capture_failures) - set(acknowledged)
            ),
            "meaning": (
                "Transfer eligibility of saved work, not launch readiness: pauses, "
                "blockers, fresh agreements and authorization remain separate gates."
            ),
        }

    def transition_activate(
        self,
        work_id,
        *,
        note,
        transition_id=None,
        proposal_id=None,
        expected_revision=None,
        operation_id=None,
        acknowledge_capture_failures=(),
        actor="operator",
    ) -> dict:
        self._require_operator(actor)
        if not note:
            raise ValueError("Activate requires a note")
        acknowledged = sorted(set(acknowledge_capture_failures or ()))
        fingerprint = self._transition_request(
            "activate",
            work_id=work_id,
            note=note,
            transition_id=transition_id,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
            acknowledge_capture_failures=acknowledged,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            self._require_open(card)
            transition = card.get("transition")
            proposal = card.get("proposal")
            if not (transition and transition["phase"] in TRANSITION_OPEN):
                # One-operation path: nothing executes, so freezing and activating
                # the exact named proposal is a single operator decision.
                if not proposal:
                    raise ValueError("no_pending_proposal")
                if not proposal_id or proposal_id != proposal["proposal_id"]:
                    raise ValueError("proposal_mismatch")
                if proposal["base_plan_revision"] != card["plan_revision"]:
                    raise ValueError("proposal_stale")
                if self._transition_inventory(db, card):
                    raise ValueError("transition_not_begun")
                transition = {
                    "transition_id": str(uuid4()),
                    "proposal_id": proposal["proposal_id"],
                    "base_plan_revision": proposal["base_plan_revision"],
                    "phase": "ready",
                    "begun_at": time.time(),
                    "note": note,
                    "inventory": {},
                    "continuation": {},
                }
                card["transition"] = transition
            elif not transition_id or transition_id != transition["transition_id"]:
                raise ValueError("transition_mismatch")
            if not proposal or proposal["proposal_id"] != transition["proposal_id"]:
                raise ValueError("proposal_mismatch")
            if proposal["base_plan_revision"] != card["plan_revision"]:
                raise ValueError("proposal_stale")
            undisposed = [
                attempt_id
                for attempt_id, entry in transition["inventory"].items()
                if not entry.get("disposition")
            ]
            if undisposed:
                raise ValueError(
                    "inventory_not_disposed: " + ", ".join(sorted(undisposed))
                )
            if any(
                attempt["state"] in _ACTIVE | {"recovery_required"}
                for attempt in self._attempts(db, card["work_id"])
            ):
                raise ValueError(
                    "inventory_not_disposed: a live attempt appeared after begin"
                )
            # Capture failures are an informed operator decision, not a footnote:
            # every superseded attempt whose saved bytes could not be captured must
            # be acknowledged by exact attempt id before the plan changes.
            plan_steps = {step["id"]: step for step in proposal["plan"]["steps"]}
            outcomes = {
                attempt_id: self._continuation_outcome(entry, plan_steps)
                for attempt_id, entry in transition["inventory"].items()
            }
            failed = sorted(
                attempt_id
                for attempt_id, outcome in outcomes.items()
                if outcome.get("capture_failure")
            )
            unknown = sorted(set(acknowledged) - set(failed))
            if unknown:
                raise ValueError("capture_failure_unknown: " + ", ".join(unknown))
            missing = sorted(set(failed) - set(acknowledged))
            if missing:
                raise ValueError(
                    "capture_failure_unacknowledged: "
                    + ", ".join(missing)
                    + " (repeat --acknowledge-capture-failure ATTEMPT for each)"
                )
            if acknowledged:
                transition["acknowledged_capture_failures"] = acknowledged
            was_paused = card["status"] == "paused"
            self._activate_plan(db, card, proposal["plan"])
            self._record_transition_operation(
                card,
                operation_id,
                "activate",
                fingerprint,
                {
                    "transition_id": transition["transition_id"],
                    "proposal_id": proposal["proposal_id"],
                    "plan_revision": card["plan_revision"],
                    "acknowledged_capture_failures": acknowledged,
                },
            )
            if was_paused:
                # Activation never clears an operator pause or unknown-cost fence.
                card["status"] = "paused"
            for attempt_id, entry in transition["inventory"].items():
                outcome = outcomes[attempt_id]
                step_id = entry["step_id"]
                if outcome["status"] in {"pending", "none"}:
                    continue
                if outcome["status"] != "checkpoint":
                    transition["continuation"][attempt_id] = {
                        key: value
                        for key, value in outcome.items()
                        if key != "capture_failure" or value
                    }
                    if attempt_id in acknowledged:
                        transition["continuation"][attempt_id]["acknowledged"] = True
                    continue
                preserved = entry["saved"]["preserved"]
                self._step(card, step_id)["checkpoint"] = {
                    **{
                        key: preserved[key]
                        for key in (
                            "commit",
                            "base_commit",
                            "workspace",
                            "tree_hash",
                            "changed_files",
                        )
                    },
                    "checkpoint_id": str(uuid4()),
                    "attempt_id": attempt_id,
                    "step_id": step_id,
                    "plan_revision": card["plan_revision"],
                    "created_at": time.time(),
                    "evidence": list(entry["disposition"]["evidence"]),
                    "answer": entry["disposition"]["note"],
                    "continuation": "operator_approved",
                    "transition_id": transition["transition_id"],
                }
                transition["continuation"][attempt_id] = {
                    "status": "checkpoint",
                    "step_id": step_id,
                    "commit": preserved["commit"],
                }
            transition.update(
                phase="activated", activated_at=time.time(), note_on_activate=note
            )
            card.setdefault("transition_history", []).append(transition)
            card["transition"] = None
            card["proposal"] = None
            return self._record(
                db,
                card,
                "transition_activated",
                actor,
                {
                    "note": note,
                    "transition_id": transition["transition_id"],
                    "proposal_id": proposal["proposal_id"],
                    "plan_revision": card["plan_revision"],
                    "continuation": transition["continuation"],
                },
            )

    def transition_withdraw(
        self,
        work_id,
        *,
        note,
        transition_id=None,
        proposal_id=None,
        expected_revision=None,
        operation_id=None,
        actor="operator",
    ) -> dict:
        self._require_operator(actor)
        if not note:
            raise ValueError("Withdraw requires a note")
        fingerprint = self._transition_request(
            "withdraw",
            work_id=work_id,
            note=note,
            transition_id=transition_id,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
        )
        with self._transaction() as db:
            card = self._load(db, work_id)
            replay = self._transition_operation(card, operation_id, fingerprint)
            if replay is not None:
                return {**self._view(db, card), "replayed_operation": replay}
            self._check_expected_revision(card, expected_revision)
            self._require_open(card)
            proposal = card.get("proposal")
            transition = card.get("transition")
            open_transition = bool(
                transition and transition["phase"] in TRANSITION_OPEN
            )
            if not proposal and not open_transition:
                raise ValueError("no_pending_proposal")
            if open_transition:
                # After begin the frozen transition is the identity being withdrawn.
                if not transition_id or transition_id != transition["transition_id"]:
                    raise ValueError("transition_mismatch")
            elif not proposal_id or proposal_id != proposal["proposal_id"]:
                # Before begin only the exact pending proposal can be withdrawn; a
                # retained command must not discard whatever replaced it.
                raise ValueError("proposal_mismatch")
            self._record_transition_operation(
                card,
                operation_id,
                "withdraw",
                fingerprint,
                {
                    "proposal_id": (proposal or {}).get("proposal_id"),
                    "transition_id": transition["transition_id"]
                    if open_transition
                    else None,
                    "sticky_quiescence": open_transition,
                },
            )
            details = {"note": note, "proposal_id": (proposal or {}).get("proposal_id")}
            if transition and transition["phase"] in TRANSITION_OPEN:
                # Quiescence stays sticky: fenced attempts keep needing reconciliation.
                transition.update(phase="withdrawn", withdrawn_at=time.time())
                card.setdefault("transition_history", []).append(transition)
                card["transition"] = None
                details["transition_id"] = transition["transition_id"]
                details["sticky_quiescence"] = True
            card["proposal"] = None
            return self._record(db, card, "proposal_withdrawn", actor, details)

    def _recovery_target(self, db, card, step):
        """Refusal code for recovering the step's claim, or (None, attempt)."""
        attempt = self._attempt(db, step["attempt"]) if step.get("attempt") else None
        if attempt is None or attempt["state"] in {
            "succeeded",
            "blocked",
            "reconciled",
        }:
            return "recovery_claim_completed", attempt
        if attempt["autonomous"]:
            return "recovery_reconcile_required", attempt
        if attempt["state"] == "recovery_required" or card["status"] == "paused":
            return "recovery_claim_fenced", attempt
        if attempt["deadline"] <= time.time():
            return "recovery_claim_expired", attempt
        if attempt["plan_revision"] != card["plan_revision"]:
            return "recovery_context_changed", attempt
        if not attempt.get("binding"):
            return "recovery_legacy_unbound", attempt
        return None, attempt

    def _recovery_authorization(self, attempt, actor, origin, operation_id):
        """Direct host-level recovery or one exact consumed/consumable successor."""
        binding = attempt["binding"]
        origin = origin or {}
        host = origin.get("host_owner")
        if (
            host is not None
            and host == binding.get("host_owner")
            and actor == attempt["actor"]
            and binding.get("task_id") is None
        ):
            return None
        for entry in binding.get("successors") or []:
            consumed = entry.get("consumed")
            if entry["host_owner"] != host or entry["principal"] != actor:
                continue
            if consumed and consumed["operation_id"] != operation_id:
                continue
            return entry
        raise ValueError("recovery_not_authorized")

    def recovery_descriptor(self, db, card, step, *, bound=None):
        """Token-free recovery state for a step; the same stage projection applies."""
        code, attempt = self._recovery_target(db, card, step)
        if attempt is None:
            return None
        binding = attempt.get("binding") or {}
        public = _public_attempt(attempt)
        if independent_stage(bound):
            public = _withhold(public) if attempt["kind"] == "implement" else public
            public["submission"] = _withhold(public.get("submission"))
        return {
            "attempt_id": attempt["attempt_id"],
            "step_id": step["id"],
            "kind": attempt["kind"],
            "actor": attempt["actor"],
            "state": attempt["state"],
            "mode": "managed" if attempt["autonomous"] else "manual",
            "stage": attempt.get("review_stage"),
            "protocol": attempt.get("protocol"),
            "submission": public.get("submission"),
            "deadline": attempt["deadline"],
            "origin": {
                "host_owner_bound": binding.get("host_owner") is not None,
                "task_id": binding.get("task_id"),
                "conversation_id": binding.get("conversation_id"),
                "settled": binding.get("origin_settled"),
            }
            if binding
            else None,
            "requires_stop_confirmation": bool(
                binding.get("task_id") and not binding.get("origin_settled")
            ),
            "successors": [
                {
                    key: entry[key]
                    for key in ("successor_id", "principal", "scope", "authorized_at")
                }
                | {"consumed": bool(entry.get("consumed"))}
                for entry in binding.get("successors") or []
            ],
            "recoveries": _public_binding(binding)["recoveries"] if binding else [],
            "refusal": code,
            "allowed_actions": sorted(RECOVERY_ACTIONS - {"get", "history"}),
            "path": "administrative closure by an authorized successor host; ordinary continuation is a separate explicit claim",
        }

    def _recovery_replay(self, db, command, caller, origin, response, attempt):
        binding = attempt.get("binding") or {}
        record = next(
            (
                item
                for item in binding.get("recoveries") or []
                if item["operation_id"] == command.operation_id
            ),
            None,
        )
        host = (origin or {}).get("host_owner")
        if (
            record is None
            or record.get("host_owner") != host
            or record["principal"] != caller
        ):
            raise ValueError("recovery_not_authorized")
        card = self._load(db, attempt["work_id"])
        step = self._step(card, attempt["step_id"])
        code, _ = self._recovery_target(db, card, step)
        if code:
            raise ValueError(code)
        if _recovery_context(attempt) != record.get("context"):
            raise ValueError("recovery_context_changed")
        self._authenticate(db, attempt["token"])
        response["claim"]["token"] = attempt["token"]
        response["recovery"] = self.recovery_descriptor(db, card, step, bound=attempt)
        return response

    def _recover(self, db, card, command, actor, origin):
        if actor not in {"claude", "omp"}:
            raise ValueError("recovery_not_authorized")
        step = self._step(card, command.step_id)
        code, attempt = self._recovery_target(db, card, step)
        if code:
            raise ValueError(code)
        submission = attempt.get("submission") or {}
        if (
            command.submission_id is not None
            and command.submission_id != submission.get("submission_id")
        ) or (
            command.commit is not None and command.commit != submission.get("commit")
        ):
            # The request names another submission or commit than the claim's.
            raise ValueError("recovery_context_changed")
        successor = self._recovery_authorization(
            attempt, actor, origin, command.operation_id
        )
        binding = attempt["binding"]
        if binding.get("task_id") and not (binding.get("origin_settled") or {}).get(
            "teardown_confirmed"
        ):
            raise ValueError("recovery_stop_unconfirmed")
        if successor is not None:
            if successor["context"] != _recovery_context(attempt):
                raise ValueError("recovery_context_changed")
            successor["consumed"] = successor.get("consumed") or {
                "operation_id": command.operation_id,
                "at": time.time(),
                "host_owner": (origin or {}).get("host_owner"),
                "principal": actor,
                "task_id": (origin or {}).get("task_id"),
            }
        binding.setdefault("recoveries", []).append(
            {
                "operation_id": command.operation_id,
                "at": time.time(),
                "principal": actor,
                "host_owner": (origin or {}).get("host_owner"),
                "successor_id": successor["successor_id"] if successor else None,
                "context": _recovery_context(attempt),
            }
        )
        self._save_attempt(db, attempt)
        result = self._record(
            db,
            card,
            "claim_recovered",
            attempt["actor"],
            {
                "attempt_id": attempt["attempt_id"],
                "step_id": step["id"],
                "principal": actor,
                "successor_id": successor["successor_id"] if successor else None,
                "scope": successor["scope"] if successor else "own_claim",
            },
        )
        result["claim"] = {**_public_attempt(attempt), "token": attempt["token"]}
        # The recovered credential's own stage projection applies to its descriptor.
        result["recovery"] = self.recovery_descriptor(db, card, step, bound=attempt)
        return result

    def authorize_successor(
        self, attempt_id, *, host_owner, principal, note, actor="operator"
    ) -> dict:
        """Operator-only handoff of reporting authority for one exact live claim."""
        if actor != "operator":
            raise ValueError("Only the operator authorizes a recovery successor")
        if principal not in {"claude", "omp"} or not host_owner or not note:
            raise ValueError("Successor requires host owner, principal and note")
        with self._transaction() as db:
            attempt = self._attempt(db, attempt_id)
            card = self._load(db, attempt["work_id"])
            self._require_open(card)
            step = self._step(card, attempt["step_id"])
            code, _ = self._recovery_target(db, card, step)
            if code and code != "recovery_not_authorized":
                raise ValueError(code)
            entry = {
                "successor_id": str(uuid4()),
                "host_owner": host_owner,
                "principal": principal,
                "scope": "report_only",
                "context": _recovery_context(attempt),
                "authorized_at": time.time(),
                "authorized_by": actor,
                "note": note,
                "consumed": None,
            }
            attempt["binding"]["successors"].append(entry)
            self._save_attempt(db, attempt)
            view = self._record(
                db,
                card,
                "successor_authorized",
                actor,
                {
                    "attempt_id": attempt_id,
                    "step_id": step["id"],
                    "successor_id": entry["successor_id"],
                    "principal": principal,
                    "scope": entry["scope"],
                    "note": note,
                },
            )
            return {
                **view,
                "successor": {k: v for k, v in entry.items() if k != "host_owner"},
            }

    def origin_settled(self, task_id, *, status, teardown_confirmed) -> list[str]:
        """Lifecycle evidence from the native boundary: the origin turn is over."""
        with self._transaction() as db:
            settled = []
            cards = {}
            for attempt in self._attempts(db):
                binding = attempt.get("binding") or {}
                if binding.get("task_id") != task_id or binding.get("origin_settled"):
                    continue
                binding["origin_settled"] = {
                    "at": time.time(),
                    "task_status": status,
                    "teardown_confirmed": bool(teardown_confirmed),
                    "source": "native_worker.lifecycle",
                }
                self._save_attempt(db, attempt)
                settled.append(attempt["attempt_id"])
                cards.setdefault(attempt["work_id"], []).append(attempt["attempt_id"])
            for work_id, identifiers in cards.items():
                self._record(
                    db,
                    self._load(db, work_id),
                    "origin_settled",
                    "supervisor",
                    {
                        "task_id": task_id,
                        "attempt_ids": identifiers,
                        "task_status": status,
                    },
                )
            return settled

    def recovery_scope(self, token, actor, host=None):
        """report_only when this caller wields the credential as a successor."""
        with self._read_connection() as db:
            attempt = self._credential(db, token)
        delegation = self._delegation(attempt, actor, host)
        return delegation["scope"] if delegation else None

    def record_application(self, work_id, record: dict, *, actor="operator") -> dict:
        """Append one explicit apply receipt or Git assessment; never inferred."""
        if actor != "operator":
            raise ValueError("Only the operator records application observations")
        if not isinstance(record, dict) or record.get("kind") not in {
            "apply_receipt",
            "git_assessment",
        }:
            raise ValueError(
                "Application record kind must be apply_receipt or git_assessment"
            )
        with self._transaction() as db:
            card = self._load(db, work_id)
            final = self._step(card, self._final_step_id(card))["submission"]
            entry = {
                **record,
                "record_id": str(uuid4()),
                "recorded_at": time.time(),
                "recorded_by": actor,
                "plan_revision": card["plan_revision"],
                "card_status": card["status"],
                "final_submission_id": final["submission_id"] if final else None,
                "final_commit": final["commit"] if final else None,
            }
            card.setdefault("application", []).append(entry)
            return self._record(db, card, "application_observed", actor, entry)

    def _final_step_id(self, card):
        referenced = {
            dependency for step in card["steps"] for dependency in step["depends_on"]
        }
        return next(
            step["id"] for step in card["steps"] if step["id"] not in referenced
        )

    def recover_owner(self, owner_id) -> list[dict]:
        with self._transaction() as db:
            affected = {}
            for attempt in self._attempts(db):
                if attempt["owner_id"] == owner_id and attempt["state"] in _ACTIVE:
                    card = affected.setdefault(
                        attempt["work_id"], self._load(db, attempt["work_id"])
                    )
                    attempt.update(
                        state="recovery_required",
                        error="Previous owner disappeared; work may have launched and must not be replayed",
                        fenced_at=time.time(),
                    )
                    self._save_attempt(db, attempt)
                    self._step(card, attempt["step_id"])["state"] = "recovery_required"
            return [
                self._record(
                    db, card, "owner_recovered", "supervisor", {"owner_id": owner_id}
                )
                for card in affected.values()
            ]

    def events(self, after: int = 0, limit: int = 100) -> list[dict]:
        """Global durable invalidation cursor; event hints convey no authority."""
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "Events require a nonnegative cursor and limit between 1 and 1000"
            )
        with self._read_connection() as db:
            rows = db.execute(
                "SELECT rowid AS event_id, work_id, revision, event FROM work_events WHERE rowid>? ORDER BY rowid LIMIT ?",
                (after, limit),
            )
            return [
                {
                    "event_id": row["event_id"],
                    "work_id": row["work_id"],
                    "revision": row["revision"],
                    "kind": json.loads(row["event"])["kind"],
                    "actor": json.loads(row["event"])["actor"],
                }
                for row in rows
            ]

    def event_head(self) -> int:
        with self._read_connection() as db:
            return db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM work_events"
            ).fetchone()[0]
