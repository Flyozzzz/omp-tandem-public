"""Bounded team plans over existing task contracts; declarations grant no sandbox."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from omp_rpc import ThinkingLevel
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .execution import ExecutionOptions
from .models import TaskContract
from .runtime_models import Mode

TEAM_MODEL_PROVIDER = "openai-codex"

StepId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
]
Text = Annotated[str, StringConstraints(pattern=r"\S", max_length=2000)]
SubscriptionModel = Annotated[
    str,
    StringConstraints(
        pattern=rf"^{TEAM_MODEL_PROVIDER}/[A-Za-z0-9][A-Za-z0-9._:\-]*(/[A-Za-z0-9][A-Za-z0-9._:\-]*)*$",
        max_length=200,
    ),
]


class TeamExecution(ExecutionOptions):
    model: SubscriptionModel | None = None


class TeamStep(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    id: StepId
    title: Text
    role: Text | None = None
    mode: Mode = "analyze"
    contract: TaskContract
    depends_on: list[StepId] = Field(default_factory=list, max_length=15)
    dependency_policy: Literal["success", "reported_evidence"] = Field(
        default="success",
        description="Success-only by default. reported_evidence is restricted to the final think synthesis of a wholly read-only team and accepts completed structured success/partial evidence, never failed or unreported work.",
    )
    execution: TeamExecution | None = None

    @model_validator(mode="after")
    def valid_requirements(self) -> Self:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("Team dependencies must be unique")
        if self.mode != "work" and (
            self.contract.requirements.requires_write
            or self.contract.requirements.requires_shell
            or (
                self.contract.verification is not None
                and any(
                    check.requires_shell for check in self.contract.verification.checks
                )
            )
        ):
            raise ValueError("Write and shell requirements need an explicit work step")
        if (
            self.contract.requirements.requires_write
            and not self.contract.scope.owned_files
        ):
            raise ValueError("Team writes require explicit owned_files")
        return self


class TeamPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    goal: Annotated[str, StringConstraints(pattern=r"\S", max_length=8000)]
    steps: list[TeamStep] = Field(min_length=2, max_length=16)
    cwd: str | None = Field(default=None, max_length=4096)
    project_context_id: str | None = None
    max_parallel: int = Field(default=3, ge=1, le=4, strict=True)
    budget_seconds: int = Field(default=1800, ge=10, le=7200, strict=True)
    allow_work: bool = Field(
        default=False,
        description="Explicitly permits work-mode steps with the existing edit/shell capabilities. Not an OS sandbox or a detached-execution grant.",
    )
    model_selection: Literal["auto", "coordinator"] = Field(
        default="auto",
        description="Auto uses configured Jev on goal/title summaries and public model facts. Coordinator sends no team summaries to Jev and agrees explicit assignments from the OMP catalog.",
    )
    execution: TeamExecution | None = None

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        steps = {step.id: step for step in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("Team step IDs must be unique")
        if not self.allow_work and any(step.mode == "work" for step in self.steps):
            raise ValueError("Work steps require allow_work=true on the team plan")
        visiting, visited = set(), set()

        def visit(identifier):
            if identifier not in steps:
                raise ValueError(f"Unknown team dependency: {identifier}")
            if identifier in visiting:
                raise ValueError("Team dependencies must be acyclic")
            if identifier in visited:
                return
            visiting.add(identifier)
            for dependency in steps[identifier].depends_on:
                visit(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in steps:
            visit(identifier)
        prerequisites = {
            dependency for step in self.steps for dependency in step.depends_on
        }
        if len(set(steps) - prerequisites) != 1:
            raise ValueError(
                "Team needs one final integration step depending transitively on every other step"
            )
        integration = next(iter(set(steps) - prerequisites))
        for step in self.steps:
            if step.dependency_policy == "reported_evidence" and (
                step.id != integration
                or step.mode != "think"
                or any(item.mode == "work" for item in self.steps)
            ):
                raise ValueError(
                    "reported_evidence requires the final think step of a wholly read-only team"
                )
        return self

    @property
    def integration_step(self) -> str:
        prerequisites = {
            dependency for step in self.steps for dependency in step.depends_on
        }
        return next(step.id for step in self.steps if step.id not in prerequisites)


class TeamAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: SubscriptionModel
    thinking: ThinkingLevel
    reason: Text


def team_dependency_ready(
    policy, *, step_status, native_status, outcome, report_outcome
):
    """A settled process alone never grants permission to consume its evidence."""
    if (
        step_status not in {"completed", "partial", "blocked"}
        or native_status != "completed"
        or outcome != report_outcome
    ):
        return False
    if policy == "reported_evidence":
        return outcome in {"success", "partial"}
    return policy == "success" and step_status == "completed" and outcome == "success"
