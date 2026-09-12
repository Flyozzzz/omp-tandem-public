"""Validated collaboration declarations, not filesystem access controls."""

from __future__ import annotations

import json
from pathlib import PureWindowsPath
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .findings import FindingDraft, FindingUpdate

__all__ = [
    "OUTCOME_ADAPTER",
    "ArtifactInfo",
    "BlockedOutcome",
    "CheckRun",
    "CheckScope",
    "PartialOutcome",
    "PassedCheck",
    "RuleReference",
    "SuccessOutcome",
    "TaskCheck",
    "TaskContract",
    "TaskOutcome",
    "TaskScope",
    "TurnContract",
    "WorkPolicy",
    "assess_checks",
    "decode_outcome",
    "outcome_contract_error",
    "outcome_refusal_message",
    "outcome_schema",
    "parse_outcome",
    "run_applies",
]

_NonBlank = Annotated[str, StringConstraints(pattern=r"\S")]
_NonBlank2000 = Annotated[_NonBlank, StringConstraints(max_length=2000)]


def _validate_owned_file(value: str) -> str:
    # Check both path dialects even when this process runs on a POSIX host.
    components = value.replace("\\", "/").split("/")
    if (
        not value.strip()
        or PureWindowsPath(value).anchor
        or value.startswith("/")
        or value.endswith(("/", "\\"))
        or any(character in value for character in "*?[]{}")
        or ".." in components
        or components[-1] == "."
    ):
        raise ValueError(
            "owned_files must contain explicit relative file paths without traversal or globs"
        )
    return value


def _validate_artifact_id(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("artifact IDs must be canonical lowercase hyphenated UUIDs")
    return value


_OwnedFile = Annotated[str, AfterValidator(_validate_owned_file)]
_ArtifactId = Annotated[str, AfterValidator(_validate_artifact_id)]


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskScope(_ContractModel):
    """Declared file ownership for coordination; NOT a filesystem sandbox."""

    owned_files: list[_OwnedFile] = Field(default_factory=list, max_length=100)


class TaskContract(_ContractModel):
    goal: _NonBlank = Field(max_length=8000)
    context: str = Field(default="", max_length=60000)
    scope: TaskScope = Field(default_factory=TaskScope)
    constraints: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    acceptance: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)


class WorkPolicy(_ContractModel):
    """Persistent execution constraints; product context cannot override these."""

    scope: TaskScope = Field(default_factory=TaskScope)
    constraints: list[_NonBlank2000] = Field(default_factory=list, max_length=50)


class TurnContract(_ContractModel):
    """A new turn's goal and criteria, without permission or ownership changes."""

    goal: _NonBlank = Field(max_length=8000)
    context: str = Field(default="", max_length=60000)
    constraints: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    acceptance: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)


class RuleReference(_ContractModel):
    rule_id: _NonBlank = Field(max_length=100)
    assessment: Literal["preserved", "violated", "uncertain", "not_applicable"]
    explanation: _NonBlank2000


class TaskCheck(_ContractModel):
    """A current check claim in a final report; history lives in check runs."""

    name: _NonBlank = Field(max_length=200)
    command: str | None = Field(default=None, max_length=2000)
    result: Literal["passed", "failed", "not_run"]
    detail: str = Field(default="", max_length=4000)
    run_id: _ArtifactId | None = Field(
        default=None,
        description="Optional check run this claim refers to; the run record carries the bytes, environment and provenance.",
    )


class PassedCheck(TaskCheck):
    """Only shape a success report may carry: every current check passed."""

    result: Literal["passed"]


class CheckScope(_ContractModel):
    """Which bytes a run observed. Equal digests of the same kind are the same bytes."""

    kind: Literal["tree", "commit", "archive", "content"]
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,128}$")]
    boundaries: list[_NonBlank2000] = Field(
        default_factory=list,
        max_length=20,
        description="What the run did not exercise (external services, other platforms, unselected files).",
    )


_Role = Literal["author", "reviewer", "integrator", "operator"]
_Provenance = Literal["participant_reported", "machine_observed"]


class CheckRun(_ContractModel):
    """One append-only execution record of a check against identified bytes.

    A run never changes a previous run: a later pass is a new record, and a
    failure stays visible. The hash of the record makes it immutable; it does
    not prove the command ran, which is why provenance stays participant_reported
    unless a code-owned runner observed the execution itself.
    """

    check_id: _NonBlank = Field(max_length=200)
    run_id: _ArtifactId = Field(default_factory=lambda: str(uuid4()))
    criterion: _NonBlank2000
    role: _Role
    command: str | None = Field(default=None, max_length=2000)
    environment: dict[
        Annotated[str, StringConstraints(max_length=60, pattern=r"\S")],
        Annotated[str, StringConstraints(max_length=200)],
    ] = Field(default_factory=dict)
    started_at: float | None = None
    ended_at: float | None = None
    scope: CheckScope
    result: Literal["passed", "failed", "not_run"]
    output_artifact_id: _ArtifactId | None = None
    provenance: _Provenance = "participant_reported"
    supersedes: _ArtifactId | None = Field(
        default=None,
        description="Earlier run_id this run replaces; honoured only for the same criterion, role, bytes, command and environment.",
    )
    note: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if len(self.environment) > 24:
            raise ValueError("environment carries at most 24 entries")
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.ended_at < self.started_at
        ):
            raise ValueError("ended_at precedes started_at")
        if self.supersedes == self.run_id:
            raise ValueError("a run cannot supersede itself")
        return self


class _OutcomeBase(_ContractModel):
    answer: _NonBlank = Field(
        max_length=60000,
        description="The actual requested answer or deliverable text, not an acknowledgment or a statement that it was provided. Preserve the requested language and format.",
    )
    summary: _NonBlank = Field(
        max_length=4000,
        description="Brief work-status summary, separate from the actual answer.",
    )
    changed_files: list[Annotated[str, StringConstraints(max_length=1000)]] = Field(
        default_factory=list, max_length=100
    )
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)
    rule_references: list[RuleReference] = Field(default_factory=list, max_length=50)
    decision_references: list[
        Annotated[_NonBlank, StringConstraints(max_length=100)]
    ] = Field(default_factory=list, max_length=100)
    findings: list[FindingDraft] = Field(default_factory=list, max_length=100)
    finding_updates: list[FindingUpdate] = Field(default_factory=list, max_length=100)
    check_runs: list[CheckRun] = Field(
        default_factory=list,
        max_length=50,
        description="Executed check runs to record append-only alongside this report. Participant reports are recorded as participant_reported.",
    )

    @model_validator(mode="after")
    def validate_reported_runs(self) -> Self:
        if any(run.provenance != "participant_reported" for run in self.check_runs):
            raise ValueError(
                "reported check runs are participant_reported; machine_observed provenance is set only by a code-owned runner"
            )
        return self


class SuccessOutcome(_OutcomeBase):
    """Every current check passed and nothing blocks the deliverable."""

    outcome: Literal["success"]
    checks: list[PassedCheck] = Field(default_factory=list, max_length=50)
    blockers: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=list, max_length=0
    )


class PartialOutcome(_OutcomeBase):
    """Delivered with open checks or unfinished parts, all listed."""

    outcome: Literal["partial"]
    checks: list[TaskCheck] = Field(default_factory=list, max_length=50)
    blockers: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=list, max_length=50
    )


class BlockedOutcome(_OutcomeBase):
    """Cannot proceed; at least one substantive reason is required."""

    outcome: Literal["blocked"]
    checks: list[TaskCheck] = Field(default_factory=list, max_length=50)
    blockers: list[_NonBlank2000] = Field(min_length=1, max_length=50)


# One structural union: each variant is a complete object selected by its
# literal `outcome`, expressed as `anyOf` with no conditional keywords, so the
# registered host schema and the runtime decoder come from the same definitions.
TaskOutcome = SuccessOutcome | PartialOutcome | BlockedOutcome
_OUTCOMES = {
    "success": SuccessOutcome,
    "partial": PartialOutcome,
    "blocked": BlockedOutcome,
}
OUTCOME_ADAPTER = TypeAdapter(TaskOutcome)


def outcome_schema() -> dict:
    """The exact parameter schema registered for the finish tool.

    Structural variants under `anyOf`, plus a root `type: object` so hosts that
    require an object parameter schema accept it; no `if/then`, `oneOf` or `not`.
    """
    schema = OUTCOME_ADAPTER.json_schema()
    return {
        "type": "object",
        "title": "TaskOutcome",
        "description": "Final report: one complete variant selected by its literal outcome (success, partial, blocked).",
        **schema,
    }


def parse_outcome(data) -> SuccessOutcome | PartialOutcome | BlockedOutcome:
    """Validate a report against its declared variant; never rewrite the outcome.

    Dispatching on the literal first keeps validation errors attached to the
    variant the author chose instead of listing every union branch.
    """
    if isinstance(data, _OutcomeBase):
        return data
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, dict) and data.get("outcome") in _OUTCOMES:
        return _OUTCOMES[data["outcome"]].model_validate(data)
    return OUTCOME_ADAPTER.validate_python(data)


def outcome_refusal_message(error: ValidationError) -> str:
    """Human-readable refusal that also carries the machine-readable envelope."""
    envelope = outcome_contract_error(error)
    return (
        "Final report refused ("
        + envelope["code"]
        + "): fields "
        + ", ".join(envelope["fields"])
        + "; "
        + "; ".join(envelope["reasons"])
        + ". Allowed fix: "
        + envelope["allowed_fix"]
        + " "
        + json.dumps(envelope, ensure_ascii=False)
    )


def decode_outcome(data):
    """Host-tool decoder: the same parser, refusing with the field envelope."""
    try:
        return parse_outcome(data)
    except ValidationError as exc:
        raise ValueError(outcome_refusal_message(exc)) from None


def outcome_contract_error(error: ValidationError) -> dict:
    """Stable, field-addressed description of why a report was refused."""
    fields = sorted(
        {
            ".".join(str(part) for part in item["loc"]) or "<root>"
            for item in error.errors()
        }
    )
    reasons = sorted({item["msg"] for item in error.errors()})
    allowed_fix = (
        "Keep the declared outcome. For success, list only passed current checks "
        "and no blockers; for blocked, give at least one substantive blocker; "
        "otherwise report partial with the open checks listed. Historical or "
        "not-run checks belong in check_runs, not in the current checks list."
    )
    return {
        "code": "outcome_contract",
        "fields": fields,
        "reasons": reasons,
        "allowed_fix": allowed_fix,
    }


def _same_inputs(run: CheckRun, other: CheckRun) -> bool:
    return (
        run.criterion == other.criterion
        and run.role == other.role
        and run.command == other.command
        and run.environment == other.environment
    )


def run_applies(run: CheckRun, scope: CheckScope | dict | None) -> bool:
    """Whether a run's observed bytes are the bytes in question.

    Equal tree digests keep applicability across commits that only changed
    metadata; different digests or kinds never inherit an earlier result.
    """
    if scope is None:
        return True
    if isinstance(scope, dict):
        scope = CheckScope.model_validate(scope)
    return run.scope.kind == scope.kind and run.scope.digest == scope.digest


def assess_checks(runs: list[CheckRun | dict], scope=None) -> dict:
    """Current applicability of append-only check runs, with history intact.

    Groups runs by criterion and role. The newest applicable run in a group is
    its current result; a `supersedes` claim is honoured only when it names an
    earlier run of the same group with the same bytes, command and environment,
    otherwise it is listed under invalid_supersedes and changes nothing. A
    failure followed by a later pass of the same bytes stays visible as a known
    issue: the pass is a new observation, not an explanation of the failure.
    """
    records = [
        run if isinstance(run, CheckRun) else CheckRun.model_validate(run)
        for run in runs
    ]
    by_id = {run.run_id: run for run in records}
    ordered = sorted(
        enumerate(records),
        key=lambda item: (item[1].ended_at or item[1].started_at or 0.0, item[0]),
    )
    groups: dict[tuple[str, str], list[CheckRun]] = {}
    for _, run in ordered:
        groups.setdefault((run.criterion, run.role), []).append(run)
    criteria, invalid, known_issues = [], [], []
    for (criterion, role), members in groups.items():
        applicable = [run for run in members if run_applies(run, scope)]
        valid_supersedes = set()
        for run in members:
            target = by_id.get(run.supersedes) if run.supersedes else None
            if run.supersedes and (
                target is None
                or not _same_inputs(run, target)
                or target.scope != run.scope
                or (target.ended_at or 0.0) > (run.ended_at or 0.0)
            ):
                invalid.append(
                    {
                        "run_id": run.run_id,
                        "supersedes": run.supersedes,
                        "reason": "target missing or different criterion/role/bytes/command/environment",
                    }
                )
            elif run.supersedes:
                valid_supersedes.add(run.supersedes)
        # A failure is answered only by a later pass of the same bytes with the
        # same command and environment (or a valid supersedes, which implies the
        # same). A pass under different inputs is a different observation and
        # leaves the failure open; it still cannot rewrite history.
        open_failures = []
        for index, run in enumerate(applicable):
            if run.result != "failed":
                continue
            answered = run.run_id in valid_supersedes or any(
                later.result == "passed"
                and later.scope == run.scope
                and _same_inputs(later, run)
                for later in applicable[index + 1 :]
            )
            if answered:
                known_issues.append(
                    {
                        "criterion": criterion,
                        "role": role,
                        "failed_run_id": run.run_id,
                        "note": "failure followed by a later pass of the same bytes; cause not established",
                    }
                )
            else:
                open_failures.append(run.run_id)
        current = applicable[-1] if applicable else None
        status = "failed" if open_failures else current.result if current else "not_run"
        criteria.append(
            {
                "criterion": criterion,
                "role": role,
                "status": status,
                "current_run_id": current.run_id if current else None,
                "provenance": current.provenance if current else None,
                "run_ids": [run.run_id for run in members],
                "applicable_run_ids": [run.run_id for run in applicable],
                "open_failure_run_ids": open_failures,
            }
        )
    statuses = {item["status"] for item in criteria}
    if not criteria:
        status = "not_run"
    elif "failed" in statuses:
        status = "failed"
    elif "not_run" in statuses:
        status = "not_run"
    else:
        status = "passed"
    return {
        "status": status,
        "criteria": criteria,
        "invalid_supersedes": invalid,
        "known_issues": known_issues,
        "run_count": len(records),
    }


class ArtifactInfo(_ContractModel):
    artifact_id: str
    conversation_id: str | None
    task_id: str | None
    context_id: str | None = None
    name: str
    version: int
    sha256: str
    media_type: str
    characters: int
    created: float
