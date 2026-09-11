"""Server-bound participant identity; shared task data never grants execution authority."""

import os
import stat
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .work_items import WorkCommand
from .work_workspace import WorkWorkspace


class WorkToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: WorkCommand
    wait_seconds: int = Field(default=0, ge=0, le=25)


def read_work_token(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Work token must be a private regular file")
        if info.st_uid != os.getuid():
            raise ValueError("Work token must belong to this OS user")
        data = source.read(4097)
    if len(data) > 4096:
        raise ValueError("Work token file is too large")
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("Work token is empty")
    return token


def perform_work(store, request, *, actor, attempt_token=None, claims=None):
    command = WorkCommand.model_validate(request)
    key = (command.work_id, command.step_id)
    token = attempt_token or (
        claims.get(key) if claims is not None and command.action != "claim" else None
    )
    inferred = False
    if token is None and claims and command.action in {"get", "history"}:
        # A reader that holds exactly one claim on this work reads through it, so
        # the store can apply the review-stage visibility policy to its view.
        held = [
            value
            for (work_id, _step), value in claims.items()
            if work_id == command.work_id
        ]
        if len(held) == 1:
            token, inferred = held[0], True
    bound, output = None, None
    if token:
        # Exact receipts may still be read with a retired credential; new effects
        # require an active one and are checked again by the domain transaction.
        with suppress(ValueError):
            bound = store.authenticate(token)
    if bound and not bound["autonomous"] and command.action == "submit":
        view = store.perform(
            {"action": "get", "work_id": bound["work_id"]}, actor=actor
        )
        # Validate external source BEFORE recording immutable submission intent.
        output = WorkWorkspace(store.scope).adopt_submission(
            bound, view["plan"], command.commit
        )
    if inferred:
        try:
            store.authenticate(token)
        except ValueError:
            # Only a retired inferred credential falls back to an unbound read;
            # a refusal of the bound read itself must not fail open.
            token = None
    result = store.perform(command, actor=actor, attempt_token=token)
    if claims is not None and result.get("claim", {}).get("token"):
        claim = result["claim"]
        claims[(result["work_id"], claim["step_id"])] = claim["token"]
    if output is not None:
        store.started(bound["attempt_id"], workspace=output["workspace"])
        store.finish_attempt(
            bound["attempt_id"],
            outcome="success",
            answer=command.note or "",
            evidence=command.evidence,
            output=output,
            cost_usd=None,
        )
        result = store.perform(
            {"action": "get", "work_id": bound["work_id"]}, actor=actor
        )
    result["participant"] = actor
    if bound:
        result["bound_attempt"] = {
            field: bound[field]
            for field in ("attempt_id", "work_id", "step_id", "kind")
        }
    return result
