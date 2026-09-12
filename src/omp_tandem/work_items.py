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
import sqlite3
import subprocess
import time
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .task_store import initialize_database
from .workspace import ProjectScope

_NonBlank = Annotated[str, StringConstraints(pattern=r"\S", max_length=16000)]
_Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")
]
_Actor = Literal["claude", "omp"]
_ACTIVE = {"reserved", "running"}

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


def grant_preview(grant: dict) -> dict:
    """Operator-facing summary of what an authorization actually permits."""
    budget = attempt_budget(grant)
    return {
        **budget,
        "reserve_policy": (
            "Each launch reserves min(max_attempt_cost_usd, max_cost_usd - used - "
            "active reserves); concurrent ready steps share the unreserved remainder "
            "in launch order; unknown reported cost stops new launches."
        ),
        "permissions": {
            "read": True,
            "edit_write": grant.get("allow_work") is True,
            "shell": shell_permission(grant),
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

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        if self.owner == self.reviewer:
            raise ValueError("Step owner and reviewer must be distinct principals")
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
    """V2 omits only the new empty read-context default; V1 receipts keep their hash."""
    payload = command.model_dump()
    for step in (payload.get("plan") or {}).get("steps", []):
        if step.get("review_context_paths") == []:
            step.pop("review_context_paths")
    digest = hashlib.sha256(
        _json(
            {"command": payload, "attempt_id": bound["attempt_id"] if bound else None}
        ).encode()
    ).hexdigest()
    return digest if version == 1 else f"v2:{digest}"


def _public_attempt(attempt):
    return {
        key: value
        for key, value in attempt.items()
        if key not in {"token", "token_hash"}
    }


class WorkPresentation(_Model):
    """Transport options, deliberately outside exact operation identity."""

    view: Literal["summary", "plan", "step", "full"] = "summary"
    format: Literal["json", "markdown"] = "json"
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    include_snapshots: bool = False


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
        if len(items) > limit:
            result["continuation"] = {
                "section": "items",
                "remaining_work_ids": [item["work_id"] for item in items[limit:]],
                "action": "get",
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
                        "remaining_ids": [
                            item["blocker_id"] for item in unresolved[limit:]
                        ],
                        "view": "full",
                    }
                )
            if len(result["steps"]) > limit:
                result.setdefault("continuation", []).append(
                    {
                        "section": "steps",
                        "remaining_ids": [
                            item["id"] for item in result["steps"][limit:]
                        ],
                        "view": "step",
                        "required_fields": ["step_id"],
                    }
                )
                result["steps"] = result["steps"][:limit]
            if len(_json(result).encode()) > 16384:
                # Keep identifiers and explicit section pointers when material must
                # be read separately. Never cut a reason or acceptance string.
                result["blockers"] = [
                    {
                        "blocker_id": item["blocker_id"],
                        "origin": item["origin"],
                        "detail": {"view": "full"},
                    }
                    for item in unresolved
                ]
                result["continuation"] = [
                    *result.get("continuation", []),
                    {
                        "section": "summary",
                        "reason": "size_limit",
                        "view": "full",
                        "limit_bytes": 16384,
                    },
                ]
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
    if format == "markdown":
        # Render exactly the selected material, without a second JSON copy.
        return {
            "markdown": "```json\n"
            + json.dumps(result, ensure_ascii=False, indent=2)
            + "\n```"
        }
    return result


class WorkStore:
    def __init__(self, database: Path, scope: ProjectScope):
        self.database = Path(database)
        self.scope = scope
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
            due = any(
                attempt["state"] in _ACTIVE and attempt["deadline"] <= time.time()
                for attempt in self._attempts(db)
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

    def _attempts(self, db, work_id=None):
        rows = db.execute(
            "SELECT attempt FROM work_attempts"
            + (" WHERE work_id=?" if work_id else ""),
            (work_id,) if work_id else (),
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
        for step in view["steps"]:
            step["attempt"] = (
                _public_attempt(self._attempt(db, step["attempt"]))
                if step["attempt"]
                else None
            )
            if step["attempt"] and step["attempt"].get("kind") == "review":
                # Truthful protocol label: attempts created before independent-first
                # stages existed disclosed author material from the start.
                step["attempt"].setdefault("protocol", "legacy_disclosure")
            if (
                step["attempt"]
                and step["attempt"]["state"] in _ACTIVE
                and step["attempt"]["deadline"] <= time.time()
            ):
                step["state"] = "recovery_required"
                step["attempt"]["state"] = "recovery_required"
        view["events"] = {"revision": card["revision"], "cursor": card["revision"]}
        view["next_action"] = (
            "Resume explicitly; fenced attempts require operator reconciliation."
            if card["status"] == "paused"
            else "Both principals must agree to this exact plan revision."
            if not self._agreed(card)
            else "All steps independently accepted; output is not merged into the user's branch."
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
        view = self._redact(db, view)
        view["markdown"] = self._markdown(view)
        return view

    @staticmethod
    def _markdown(view):
        plan = view["plan"]
        lines = [
            f"# {plan['title']}",
            "",
            f"Work `{view['work_id']}` · revision {view['revision']} · plan {view['plan_revision']} · {view['status']}",
            "",
            plan["goal"],
        ]
        if plan["context"]:
            lines.extend(["", "## Context", plan["context"]])
        for title, items in (
            ("Constraints", plan["constraints"]),
            ("Global acceptance", plan["acceptance"]),
        ):
            lines.extend(["", "## " + title, *("- " + item for item in items)])
        if view["blockers"]:
            lines.extend(["", "## Card blockers (apply to every step)"])
            lines.extend(
                "- " + WorkStore._blocker_markdown(blocker)
                for blocker in view["blockers"]
                if blocker["resolved_at"] is None
            )
        lines.extend(["", "## Checklist"])
        for step in view["steps"]:
            spec = next(item for item in plan["steps"] if item["id"] == step["id"])
            lines.extend(
                [
                    f"- [{'x' if step['state'] == 'accepted' else ' '}] {step['id']}: {spec['title']} — {step['state']} (owner {step['owner']}; reviewer {step['reviewer']})",
                    "  Goal: " + spec["goal"],
                    "  Dependencies: " + (", ".join(step["depends_on"]) or "none"),
                    "  Owned files: " + (", ".join(spec["owned_files"]) or "none"),
                ]
            )
            lines.extend(
                "  - Acceptance: " + criterion for criterion in spec["acceptance"]
            )
            lines.extend(
                "  - " + WorkStore._blocker_markdown(blocker)
                for blocker in step["blockers"]
                if blocker["resolved_at"] is None
            )
            if step["submission"]:
                lines.append(
                    "  Submission: "
                    + step["submission"]["submission_id"]
                    + " @ "
                    + step["submission"]["commit"]
                )
            if step.get("acceptance"):
                lines.extend(
                    "  - Evidence: " + item for item in step["acceptance"]["evidence"]
                )
        lines.extend(
            [
                "",
                "Acceptance records are attributed attestations; the store does not execute or certify checks.",
                "",
                view["next_action"],
            ]
        )
        return "\n".join(lines)

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
            "details": self._redact(db, details or {}),
            "snapshot": view,
        }
        db.execute(
            "INSERT INTO work_events VALUES (?,?,?)",
            (card["work_id"], card["revision"], _json(event)),
        )
        return view

    def _redact(self, db, value):
        encoded = _json(value)
        for attempt in self._attempts(db):
            for key in ("token", "token_hash"):
                encoded = encoded.replace(attempt[key], "[REDACTED]")
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
        if actor is not None and actor != attempt["actor"]:
            raise ValueError("Attempt belongs to a different principal")
        return attempt

    def _authenticate(self, db, token, actor=None):
        attempt = self._credential(db, token, actor)
        if attempt["state"] not in _ACTIVE or attempt["deadline"] <= time.time():
            raise ValueError("Attempt credential is fenced or expired")
        card = self._load(db, attempt["work_id"])
        if (
            card["status"] == "paused"
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
        binding = {
            "work_id": current["work_id"],
            "as_of_revision": current["revision"],
            "actor": actor,
            "visibility": current.get("visibility", "full"),
            "bound_attempt": current.get("bound_attempt"),
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
                "SELECT event FROM work_events WHERE work_id=? AND revision>? AND revision<=? ORDER BY revision LIMIT ?",
                (current["work_id"], after, revision, limit + 1),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows[:limit]]
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

    def perform(
        self,
        command: WorkCommand | dict,
        *,
        actor: str,
        attempt_token: str | None = None,
    ) -> dict:
        command = WorkCommand.model_validate(command)
        if actor not in {"claude", "omp", "operator"}:
            raise ValueError("Invalid work principal")
        # Opportunistic expiry is separate from CAS; reads never wait for its write.
        self._maintenance()
        source_commit = self._head() if command.action == "claim" else None
        connection = (
            self._read_connection
            if command.action in {"get", "list", "history"}
            else self._transaction
        )
        with connection() as db:
            bound = (
                self._credential(db, attempt_token, actor) if attempt_token else None
            )
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
                (actor, command.operation_id),
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
                if command.action == "claim":
                    attempt = self._attempt(db, response["claim"]["attempt_id"])
                    if attempt["state"] in _ACTIVE:
                        response["claim"]["token"] = attempt["token"]
                    return _project_claim(response, attempt)
                return redact_author(response, bound)
            if bound:
                self._authenticate(db, attempt_token, actor)
            if command.action == "create":
                plan = command.plan.model_dump()
                card = {
                    "work_id": str(uuid4()),
                    "revision": 0,
                    "plan_revision": 1,
                    "status": "draft",
                    "plan": plan,
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
                result = self._perform(db, card, command, actor, bound, source_commit)
            if command.action == "claim":
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
                (actor, command.operation_id, fingerprint, _json(saved)),
            )
            if bound and command.action != "compare":
                # Re-read the credential: report/compare change what may be shown.
                bound = self._credential(db, attempt_token, actor)
            return redact_author(result, bound)

    def _perform(self, db, card, command, actor, bound, source_commit=None):
        action = command.action
        if action == "propose":
            if command.plan is None:
                raise ValueError("Propose requires a complete plan")
            plan = command.plan.model_dump()
            if plan != WorkPlan.model_validate(card["plan"]).model_dump():
                identifiers = {step["id"] for step in plan["steps"]}
                if any(
                    attempt["step_id"] not in identifiers
                    and attempt["state"] in _ACTIVE | {"recovery_required"}
                    for attempt in self._attempts(db, card["work_id"])
                ):
                    raise WorkConflict(
                        "Reconcile unresolved attempts before removing or renaming their steps"
                    )
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
                        (target["blockers"] if target else card["blockers"]).extend(
                            blockers
                        )
                    if target and old["state"] == "recovery_required":
                        target.update(state="recovery_required", attempt=old["attempt"])
                if card["authorization"]:
                    card["authorization"]["revoked_at"] = time.time()
            return self._record(db, card, "proposed", actor, {"note": command.note})
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
                if any(
                    self._step(card, dependency)["state"] != "accepted"
                    for dependency in step["depends_on"]
                ):
                    raise WorkConflict("Dependencies are no longer accepted")
                verdict = {
                    "verdict": action,
                    "actor": actor,
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
        return self._record(
            db,
            card,
            action,
            actor,
            {"note": command.note, "evidence": command.evidence},
        )

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
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.scope.root),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError(
                "Work execution requires a Git repository with an immutable HEAD commit"
            )
        return commit

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
        grant = {
            "budget_seconds": budget_seconds,
            "max_launches": max_launches,
            "max_cost_usd": max_cost_usd,
            "allow_work": allow_work,
            "allow_shell": allow_shell,
            "max_attempt_cost_usd": ceiling,
            "attempt_cost_policy": policy,
            **selection,
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
            if card["authorization"]:
                card["authorization"]["revoked_at"] = time.time()
            self._fence(db, card, "Operator revoked execution authorization")
            return self._record(db, card, "revoked", "operator")

    def _claim_reason(self, card, step, attempts):
        """The reservation gate, shared by execution and participant hints."""
        if card["status"] == "paused":
            return "paused"
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
        attempts = self._attempts(db, card["work_id"])
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

    def next_actions(self, current, *, actor, attempt_token=None, claims=None):
        """Hints share the reservation/stage gates; they confer no authority."""
        if "plan" not in current or actor not in {"claude", "omp"}:
            return []
        with self._read_connection() as db:
            card = self._load(db, current["work_id"])
            attempts = self._attempts(db, card["work_id"])
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
                        continue
                if not credential:
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
                    continue
                reason = None
                if credential["step_id"] != step["id"] or credential["kind"] != kind:
                    reason = "foreign_attempt"
                elif self._unresolved(card, step):
                    reason = "blocker_open"
                if kind == "implement":
                    if credential.get("submission_intent"):
                        reason = "submission_already_recorded"
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
            return actions

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
                ordered.append(parent["submission"])

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
        grant = card["authorization"]
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
            if kind == "review" and shell_permission(grant):
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
            "autonomous": autonomous,
            "authorization_id": grant["authorization_id"] if autonomous else None,
            "source_commit": grant["source_commit"] if autonomous else source_commit,
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
                    # Arbitrary shell for a reviewer would bypass the snapshot reader;
                    # no stage-scoped execution path exists yet, so it is blocked here.
                    "shell_check_policy": "blocked_no_stage_scoped_execution"
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
            grant = card["authorization"]
            if not grant or not shell_permission(grant):
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
        source_commit = None if autonomous else self._head()
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

    def block_review_context(self, db, task_id, request):
        """Close clarification within the caller's task/question transaction."""
        row = db.execute(
            "SELECT attempt FROM work_attempts WHERE native_task_id=?", (task_id,)
        ).fetchone()
        attempt = json.loads(row["attempt"]) if row else None
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
