"""Validated collaboration declarations, not filesystem access controls."""

from __future__ import annotations

import hashlib
import json
from pathlib import PureWindowsPath
from typing import Annotated, Literal, Self
from uuid import UUID

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
    "AcceptanceItem",
    "AcceptanceObligation",
    "AcceptanceRef",
    "AcceptanceSet",
    "ArtifactInfo",
    "BlockedOutcome",
    "CheckRun",
    "CheckScope",
    "ContextOptions",
    "ConversationHandoff",
    "PartialOutcome",
    "PassedCheck",
    "RuleReference",
    "SuccessOutcome",
    "TaskCheck",
    "TaskContract",
    "TaskOutcome",
    "TaskRequirements",
    "TaskScope",
    "TurnContract",
    "VerificationCheck",
    "VerificationPlan",
    "WorkPolicy",
    "acceptance_revision",
    "assess_checks",
    "check_revision",
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


_AcceptanceId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
]
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_NonBlank16000 = Annotated[_NonBlank, StringConstraints(max_length=16000)]
_EnvironmentKey = Annotated[str, StringConstraints(max_length=60, pattern=r"\S")]
_EnvironmentValue = Annotated[str, StringConstraints(max_length=200)]

# One acceptance set expands to at most this many evidence units. An oversized
# declaration is refused rather than truncated: a silently shortened denominator
# would report coverage of requirements nobody ever counted.
MAX_EVIDENCE_UNITS = 1000


def _digest(domain: str, value) -> str:
    """Content identity of a declaration, tagged so two kinds never collide."""
    payload = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def acceptance_revision(items) -> str:
    """The revision is the content, so an edited criterion cannot keep its evidence."""
    return _digest(
        "acceptance-set-v1",
        [
            {
                "id": item["id"],
                "text": item["text"],
                "obligations": [
                    {
                        "id": duty["id"],
                        "text": duty["text"],
                        "environment": duty.get("environment") or {},
                    }
                    for duty in item.get("obligations") or []
                ],
            }
            for item in items
        ],
    )


def check_revision(check) -> str:
    """Identity of a check declaration including what it claims to exercise."""
    if not isinstance(check, dict):
        check = check.model_dump()
    return _digest(
        "verification-check-v1",
        {
            "id": check["id"],
            "criterion": check["criterion"],
            "phase": check.get("phase", "targeted"),
            "command": check.get("command"),
            "requires_shell": bool(check.get("requires_shell")),
            "scope": check.get("scope"),
            "acceptance_refs": sorted(
                (
                    ref if isinstance(ref, dict) else ref.model_dump()
                    for ref in check.get("acceptance_refs") or []
                ),
                key=lambda ref: (
                    ref["set_id"],
                    ref["revision"],
                    ref["criterion_id"],
                    ref["obligation_id"] or "",
                ),
            ),
        },
    )


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcceptanceObligation(_ContractModel):
    """One clause of a criterion, with the exact environment it is owed in."""

    id: _AcceptanceId
    text: _NonBlank16000
    environment: dict[_EnvironmentKey, _EnvironmentValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_environment(self) -> Self:
        if len(self.environment) > 24:
            raise ValueError("environment carries at most 24 entries")
        return self


class AcceptanceItem(_ContractModel):
    id: _AcceptanceId
    text: _NonBlank16000
    obligations: list[AcceptanceObligation] = Field(
        default_factory=list,
        max_length=100,
        description="Declared clauses. Naming none keeps the criterion one undecomposed unit; naming some means a mapping of the parent covers none of them.",
    )

    @model_validator(mode="after")
    def unique_obligations(self) -> Self:
        if len({duty.id for duty in self.obligations}) != len(self.obligations):
            raise ValueError("Obligation IDs must be unique within a criterion")
        return self


class AcceptanceSet(_ContractModel):
    """The declared denominator. Its revision is its content, not a version number."""

    schema_version: Literal[1] = 1
    set_id: _ArtifactId
    revision: _Sha256
    items: list[AcceptanceItem] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def content_identity(self) -> Self:
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("Acceptance criterion IDs must be unique within a set")
        units = sum(len(item.obligations) or 1 for item in self.items)
        if units > MAX_EVIDENCE_UNITS:
            raise ValueError(
                f"Acceptance set expands to {units} evidence units; the limit is {MAX_EVIDENCE_UNITS}"
            )
        computed = acceptance_revision([item.model_dump() for item in self.items])
        if self.revision != computed:
            raise ValueError(
                "Acceptance revision does not match its content; recompute it rather than editing it"
            )
        return self


class AcceptanceRef(_ContractModel):
    """Points at one criterion or one of its obligations, at an exact revision."""

    set_id: _ArtifactId
    revision: _Sha256
    criterion_id: _AcceptanceId
    obligation_id: _AcceptanceId | None = None


class TaskScope(_ContractModel):
    """Declared file ownership for coordination; NOT a filesystem sandbox."""

    owned_files: list[_OwnedFile] = Field(default_factory=list, max_length=100)


class ContextOptions(_ContractModel):
    """Presentation of pinned product data, never a reduction of required policy."""

    delivery: Literal["capsule", "full"] = "capsule"
    advisory_rule_ids: list[Annotated[_NonBlank, StringConstraints(max_length=100)]] = (
        Field(default_factory=list, max_length=50)
    )
    decision_ids: list[Annotated[_NonBlank, StringConstraints(max_length=100)]] = Field(
        default_factory=list, max_length=100
    )


class TaskRequirements(_ContractModel):
    """Declared execution needs; a live-path claim remains attributed evidence."""

    requires_shell: bool = False
    requires_write: bool = False
    entry_paths: list[_OwnedFile] = Field(default_factory=list, max_length=100)
    boundary_paths: list[_OwnedFile] = Field(default_factory=list, max_length=100)
    live_path_evidence: list[_ArtifactId] = Field(default_factory=list, max_length=32)


class CheckScope(_ContractModel):
    """Which bytes a run observed. Equal digests of the same kind are the same bytes."""

    kind: Literal["tree", "commit", "archive", "content"]
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,128}$")]
    boundaries: list[_NonBlank2000] = Field(
        default_factory=list,
        max_length=20,
        description="What the run did not exercise (external services, other platforms, unselected files).",
    )


class VerificationCheck(_ContractModel):
    id: Annotated[_NonBlank, StringConstraints(max_length=100)]
    criterion: _NonBlank2000
    phase: Literal["targeted", "candidate", "integration"] = "targeted"
    command: str | None = Field(default=None, max_length=2000)
    requires_shell: bool = False
    estimated_seconds: int | None = Field(default=None, ge=1, le=7200)
    scope: CheckScope | None = Field(
        default=None,
        description="Explicit current input identity. History from other bytes remains recorded but is not a current result; omitted scope keeps ambiguous failures unresolved.",
    )
    acceptance_refs: list[AcceptanceRef] = Field(
        default_factory=list,
        max_length=200,
        description="Which declared criteria or obligations this check is intended to exercise. An assertion of intent, never of execution or sufficiency.",
    )

    @model_validator(mode="after")
    def unique_refs(self) -> Self:
        seen = [
            (ref.set_id, ref.revision, ref.criterion_id, ref.obligation_id)
            for ref in self.acceptance_refs
        ]
        if len(set(seen)) != len(seen):
            raise ValueError("Duplicate acceptance reference on one check")
        return self


class VerificationPlan(_ContractModel):
    """Explicit check ladder; later phases include earlier required checks."""

    stage: Literal["targeted", "candidate", "integration"] = "targeted"
    checks: list[VerificationCheck] = Field(default_factory=list, max_length=50)
    preparation_seconds: int = Field(default=0, ge=0, le=7200)
    acceptance_coverage: Literal["report_only", "require_current_evidence"] = Field(
        default="report_only",
        description="report_only surfaces gaps and refuses nothing. require_current_evidence additionally refuses a success report while a declared evidence unit lacks applicable current passing evidence.",
    )
    coverage_scope: CheckScope | None = Field(
        default=None,
        description="The output bytes this coverage assessment is about. A declaration, not an observation of the filesystem.",
    )

    @model_validator(mode="after")
    def unique_checks(self) -> Self:
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("Verification check IDs must be unique")
        return self


class ConversationHandoff(_ContractModel):
    """Explicit data for a fresh conversation; no execution capability is transferred."""

    reason: _NonBlank2000
    summary: Annotated[_NonBlank, StringConstraints(max_length=8000)]
    evidence_artifact_ids: list[_ArtifactId] = Field(
        default_factory=list, max_length=32
    )
    remaining_goals: list[_NonBlank2000] = Field(default_factory=list, max_length=20)
    invalidated_assumptions: list[_NonBlank2000] = Field(
        default_factory=list, max_length=20
    )


class TaskContract(_ContractModel):
    goal: _NonBlank = Field(max_length=8000)
    context: str = Field(default="", max_length=60000)
    scope: TaskScope = Field(default_factory=TaskScope)
    constraints: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    acceptance: list[_NonBlank2000] = Field(default_factory=list, max_length=50)
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)
    context_options: ContextOptions = Field(default_factory=ContextOptions)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    verification: VerificationPlan | None = None
    acceptance_set: AcceptanceSet | None = Field(
        default=None,
        description="Optional structured form of the same acceptance list: its texts must repeat acceptance exactly, so there is never a second source of truth.",
    )

    @model_validator(mode="after")
    def acceptance_set_matches(self) -> Self:
        if self.acceptance_set is not None and [
            item.text for item in self.acceptance_set.items
        ] != list(self.acceptance):
            raise ValueError(
                "acceptance_set items must repeat the acceptance list exactly, in order"
            )
        return self


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
    context_options: ContextOptions = Field(default_factory=ContextOptions)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    verification: VerificationPlan | None = None
    acceptance_set: AcceptanceSet | None = Field(
        default=None,
        description="Optional structured form of the same acceptance list: its texts must repeat acceptance exactly, so there is never a second source of truth.",
    )

    @model_validator(mode="after")
    def acceptance_set_matches(self) -> Self:
        if self.acceptance_set is not None and [
            item.text for item in self.acceptance_set.items
        ] != list(self.acceptance):
            raise ValueError(
                "acceptance_set items must repeat the acceptance list exactly, in order"
            )
        return self


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
    check_id: Annotated[_NonBlank, StringConstraints(max_length=100)] | None = None


class PassedCheck(TaskCheck):
    """Only shape a success report may carry: every current check passed."""

    result: Literal["passed"]


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
    run_id: _ArtifactId = Field(
        description="Stable identity chosen by the recorder; exact report retries must repeat it.",
    )
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
    check_revision: _Sha256 | None = Field(
        default=None,
        description="Identity of the check declaration this run observed. A later edit to that declaration leaves this run as history rather than current credit.",
    )
    acceptance_refs: list[AcceptanceRef] = Field(
        default_factory=list,
        max_length=200,
        description="What this observation claims to exercise. Recorded with the run, never attached to it afterwards.",
    )
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
        seen = [
            (ref.set_id, ref.revision, ref.criterion_id, ref.obligation_id)
            for ref in self.acceptance_refs
        ]
        if len(set(seen)) != len(seen):
            raise ValueError("Duplicate acceptance reference on one run")
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
        max_length=200,
        description="Up to 200 executed check runs recorded append-only alongside this report, including failed attempts and retries. Participant reports are recorded as participant_reported.",
    )
    verification_scope: CheckScope | None = Field(
        default=None,
        description="The output bytes this report's evidence is about, when they were not known at launch. A declaration by the reporter, not proof that these are the current bytes.",
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
            elif run.supersedes and run.result == "passed":
                # Only a successful replacement discharges the earlier run; a
                # not_run or failed successor changes nothing about the failure.
                valid_supersedes.add(run.supersedes)
        # A failure is answered only by a later pass of the same bytes with the
        # same command and environment (or a passing valid supersedes, which
        # implies the same). A pass under different inputs is a different
        # observation and leaves the failure open; it still cannot rewrite history.
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
