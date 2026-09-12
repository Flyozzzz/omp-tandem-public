"""Shared task states and host-tool request contracts."""

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# Keep the user's diagnostic buffer; completion is event-driven.
MAX_EVENT_HISTORY = 200_000
Mode = Literal["think", "analyze", "work"]
ACTIVE = ("starting", "running", "waiting_input", "cancelling")
ACTIVE_SQL = "(" + ",".join(f"'{status}'" for status in ACTIVE) + ")"
Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[Nonempty, Field(max_length=4000)]
    context: str = Field(default="", max_length=8000)
    options: list[Annotated[Nonempty, Field(max_length=1000)]] = Field(
        default_factory=list, max_length=8
    )


RESERVED_ARTIFACT_PREFIX = "tandem:"


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        min_length=1,
        max_length=120,
        description="Artifact name; names starting with 'tandem:' are recorded only by the server.",
    )
    content: str = Field(max_length=4 * 1024 * 1024)
    media_type: Literal["text/plain", "text/markdown", "application/json"] = (
        "text/plain"
    )

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.strip().casefold().startswith(RESERVED_ARTIFACT_PREFIX):
            raise ValueError(
                "Artifact names starting with 'tandem:' are reserved for server-recorded records"
            )
        return value


class ArtifactReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=50000)


class ReviewReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: Literal[
        "manifest",
        "requirements",
        "criteria",
        "diff",
        "selected",
        "base",
        "staged",
        "checks",
        "author",
    ] = "manifest"
    path: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=50000)


class TaskSummary(TypedDict):
    task_id: str
    conversation_id: str
    status: str
    outcome: str | None
    summary: str
    next_action: str


class Cancelled(Exception):
    pass
