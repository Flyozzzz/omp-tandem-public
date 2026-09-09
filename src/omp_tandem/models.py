"""Validated collaboration declarations, not filesystem access controls."""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

__all__ = [
    "ArtifactInfo",
    "RuleReference",
    "TaskCheck",
    "TaskContract",
    "TaskOutcome",
    "TaskScope",
    "TurnContract",
    "WorkPolicy",
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
    name: _NonBlank = Field(max_length=200)
    command: str | None = Field(default=None, max_length=2000)
    result: Literal["passed", "failed", "not_run"]
    detail: str = Field(default="", max_length=4000)


class TaskOutcome(_ContractModel):
    outcome: Literal["success", "partial", "blocked"]
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
    checks: list[TaskCheck] = Field(default_factory=list, max_length=50)
    blockers: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=list, max_length=50
    )
    artifact_ids: list[_ArtifactId] = Field(default_factory=list, max_length=32)
    rule_references: list[RuleReference] = Field(default_factory=list, max_length=50)
    decision_references: list[
        Annotated[_NonBlank, StringConstraints(max_length=100)]
    ] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome == "success" and (
            self.blockers or any(check.result != "passed" for check in self.checks)
        ):
            raise ValueError("success cannot include blockers or failed/not_run checks")
        if self.outcome == "blocked" and not any(
            blocker.strip() for blocker in self.blockers
        ):
            raise ValueError("blocked outcomes require blockers")
        return self


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
