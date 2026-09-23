"""Frozen shadow-routing policy, native eligibility evidence, and task-owned journal."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal, get_args
from uuid import uuid4

from omp_rpc import ThinkingLevel
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .jev_client import MAX_REQUEST_BYTES, MODEL, canonical_json
from .runtime_models import ACTIVE

_Label = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*$"
    ),
]
_Selector = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]*/[A-Za-z0-9][A-Za-z0-9._:\-]*(/[A-Za-z0-9][A-Za-z0-9._:\-]*)*$",
    ),
]
_Description = Annotated[str, StringConstraints(max_length=2000, pattern=r"\S")]
_Number = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Identifier = Annotated[
    str,
    StringConstraints(
        max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+(/[A-Za-z0-9_.:\-]+)*$"
    ),
]
_Reason = Annotated[
    str, StringConstraints(max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
]
_LEVELS = frozenset(get_args(ThinkingLevel))


class _Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", revalidate_instances="always", strict=True
    )


class RoutingInput(_Strict):
    summary: Annotated[str, StringConstraints(max_length=4000, pattern=r"\S")]
    allow_external_summary: bool = False
    input_token_estimate: int = Field(ge=1, le=2_000_000)
    output_token_estimate: int = Field(ge=1, le=2_000_000)
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"], min_length=1, max_length=2
    )
    max_candidate_cost_usd: _Number | None = None

    @model_validator(mode="after")
    def unique_modalities(self):
        if len(self.input_modalities) != len(set(self.input_modalities)):
            raise ValueError("Input modalities must be unique")
        return self


class RoutingModel(_Strict):
    id: _Label
    model: _Selector
    description: _Description
    tool_calling: bool
    thinking_levels: list[ThinkingLevel] | None = None

    @model_validator(mode="after")
    def unique_levels(self):
        if self.thinking_levels is not None and len(self.thinking_levels) != len(
            set(self.thinking_levels)
        ):
            raise ValueError("Thinking levels must be unique")
        return self


class RoutingPolicy(_Strict):
    mode: Literal["shadow"] = "shadow"
    allow_external_summary: bool = False
    budget_seconds: float = Field(default=10, ge=1, le=30, allow_inf_nan=False)
    routes: list[RoutingModel] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def unique_routes(self):
        if len({route.id for route in self.routes}) != len(self.routes) or len(
            {route.model for route in self.routes}
        ) != len(self.routes):
            raise ValueError("Route IDs and model selectors must be unique")
        return self


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate policy key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite policy number")


def load_routing_policy(path: Path) -> RoutingPolicy:
    """Bound the actual read, reject special files, and never echo policy contents."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_REQUEST_BYTES
            ):
                raise ValueError("Invalid policy file")
            raw = stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Policy too large")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_constant,
        )
        return RoutingPolicy.model_validate(data)
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(
            "Routing policy must be a regular UTF-8 JSON file of at most 32000 bytes with a valid shadow policy"
        ) from None


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def prepare_routing(
    policy: RoutingPolicy | None,
    task: dict,
    settings: dict,
    *,
    process_model: str | None = None,
    managed: bool = False,
) -> dict | None:
    requested = settings.get("requested") or {}
    raw_input = requested.get("routing")
    if policy is None and raw_input is None:
        return None
    frozen_policy = (
        RoutingPolicy.model_validate(policy).model_dump(mode="json")
        if policy is not None
        else None
    )
    request = RoutingInput.model_validate(raw_input) if raw_input is not None else None
    frozen_input = request.model_dump(mode="json") if request is not None else None
    reason = next(
        (
            reason
            for bypass, reason in (
                (policy is None, "disabled"),
                (requested.get("model") is not None, "explicit_model"),
                (process_model is not None, "configured_model"),
                (bool(task.get("previous_task_id")), "continuation"),
                (
                    bool(task.get("review_id") or task.get("review_run_id")),
                    "snapshot_review",
                ),
                (managed, "managed_attempt"),
                (request is None, "missing_input"),
                (
                    frozen_policy is not None
                    and not frozen_policy["allow_external_summary"],
                    "operator_export_not_allowed",
                ),
                (
                    request is not None and not request.allow_external_summary,
                    "request_export_not_allowed",
                ),
            )
            if bypass
        ),
        None,
    )
    return {
        "mode": "shadow",
        "state": "terminal" if reason else "prepared",
        "status": "bypassed" if reason else "pending",
        "applied": False,
        "reason": reason,
        "policy": frozen_policy,
        "policy_sha256": _digest(frozen_policy) if frozen_policy is not None else None,
        "input": frozen_input,
        "input_sha256": _digest(frozen_input) if frozen_input is not None else None,
        "attempt_id": None,
        "proposal": None,
    }


def _number(value) -> bool:
    return (
        type(value) in (int, float)
        and value >= 0
        and (type(value) is int or math.isfinite(value))
    )


def _thinking_source(
    raw: dict, route: RoutingModel, thinking: str
) -> tuple[str | None, str | None]:
    config = raw.get("thinking")
    config = config if isinstance(config, dict) else {}
    efforts = config.get("efforts")
    known_efforts = isinstance(efforts, list) and all(
        isinstance(level, str) and level in _LEVELS - {"off"} for level in efforts
    )
    if thinking == "off":
        if config.get("requiresEffort") is True:
            return None, "thinking_incompatible"
        if raw.get("reasoning") is False or config.get("requiresEffort") is False:
            return "native_catalog", None
    else:
        if raw.get("reasoning") is False or (known_efforts and thinking not in efforts):
            return None, "thinking_incompatible"
        if raw.get("reasoning") is True and known_efforts and thinking in efforts:
            return "native_catalog", None
    if route.thinking_levels is not None and thinking in route.thinking_levels:
        return "operator_declared", None
    return None, "thinking_unknown"


def eligible_routes(
    policy: RoutingPolicy, request: RoutingInput, raw_catalog: list[dict], thinking: str
) -> dict:
    policy = RoutingPolicy.model_validate(policy)
    request = RoutingInput.model_validate(request)
    catalog: dict[str, list[dict]] = {}
    for raw in raw_catalog:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("provider"), str)
            or not isinstance(raw.get("id"), str)
        ):
            continue
        # Compare the two raw identity components, not a normalized SDK ModelInfo.
        provider, model_id = raw["provider"], raw["id"]
        if "/" in provider or not provider or not model_id:
            continue
        catalog.setdefault(provider + "/" + model_id, []).append(raw)
    candidates, excluded = [], []
    for route in policy.routes:
        reasons = []
        matches = catalog.get(route.model, [])
        if len(matches) != 1:
            excluded.append(
                {
                    "id": route.id,
                    "model": route.model,
                    "reasons": [
                        "ambiguous_catalog_identity" if matches else "not_in_catalog"
                    ],
                }
            )
            continue
        raw = matches[0]
        if not route.tool_calling:
            reasons.append("tools_not_declared")
        if raw.get("toolCalling") is False or raw.get("tool_calling") is False:
            reasons.append("tools_incompatible")
        context, output = raw.get("contextWindow"), raw.get("maxTokens")
        for value, estimate, unknown, small in (
            (
                context,
                request.input_token_estimate + request.output_token_estimate,
                "context_unknown",
                "context_exceeded",
            ),
            (
                output,
                request.output_token_estimate,
                "output_capacity_unknown",
                "output_capacity_exceeded",
            ),
        ):
            if type(value) is not int or value <= 0:
                reasons.append(unknown)
            elif value < estimate:
                reasons.append(small)
        modalities = raw.get("input")
        if (
            not isinstance(modalities, list)
            or not modalities
            or not all(isinstance(item, str) for item in modalities)
        ):
            reasons.append("modalities_unknown")
        elif not set(request.input_modalities).issubset(modalities):
            reasons.append("modalities_incompatible")
        source, thinking_reason = (
            _thinking_source(raw, route, thinking)
            if thinking in _LEVELS
            else (None, "thinking_unknown")
        )
        if thinking_reason:
            reasons.append(thinking_reason)
        cost = raw.get("cost")
        cost = cost if isinstance(cost, dict) else {}
        input_price = cost.get("input") if _number(cost.get("input")) else None
        output_price = cost.get("output") if _number(cost.get("output")) else None
        estimate = None
        if input_price is not None and output_price is not None:
            try:
                estimate = (
                    request.input_token_estimate * input_price
                    + request.output_token_estimate * output_price
                ) / 1_000_000
                if not math.isfinite(estimate):
                    estimate = None
            except OverflowError:
                estimate = None
        if request.max_candidate_cost_usd is not None:
            if estimate is None:
                reasons.append("cost_unknown")
            elif estimate > request.max_candidate_cost_usd:
                reasons.append("cost_ceiling_exceeded")
        if reasons:
            excluded.append({"id": route.id, "model": route.model, "reasons": reasons})
            continue
        candidates.append(
            {
                "id": route.id,
                "model": route.model,
                "description": route.description,
                "availability": "native_catalog",
                "tool_calling": {"supported": True, "source": "operator_declared"},
                "thinking": {"level": thinking, "source": source},
                "input_modalities": {
                    "values": [
                        item for item in ("text", "image") if item in modalities
                    ],
                    "source": "native_catalog",
                },
                "context_window": {"value": context, "source": "native_catalog"},
                "max_output_tokens": {"value": output, "source": "native_catalog"},
                "cost": {
                    "input_per_million": input_price,
                    "output_per_million": output_price,
                    "estimated_usd": estimate,
                    "source": "native_catalog" if estimate is not None else "unknown",
                },
            }
        )
    return {"candidates": candidates, "excluded": excluded}


class _Capacity(_Strict):
    value: int = Field(gt=0)
    source: Literal["native_catalog"]


class _Modalities(_Strict):
    values: list[Literal["text", "image"]]
    source: Literal["native_catalog"]


class _Tools(_Strict):
    supported: Literal[True]
    source: Literal["operator_declared"]


class _Thinking(_Strict):
    level: ThinkingLevel
    source: Literal["native_catalog", "operator_declared"]


class _Cost(_Strict):
    input_per_million: _Number | None
    output_per_million: _Number | None
    estimated_usd: _Number | None
    source: Literal["native_catalog", "unknown"]


class _Candidate(_Strict):
    id: _Label
    model: _Selector
    description: _Description
    availability: Literal["native_catalog"]
    tool_calling: _Tools
    thinking: _Thinking
    input_modalities: _Modalities
    context_window: _Capacity
    max_output_tokens: _Capacity
    cost: _Cost


class _Excluded(_Strict):
    id: _Label
    model: _Selector
    reasons: list[_Reason] = Field(min_length=1)


class _Eligibility(_Strict):
    candidates: list[_Candidate] = Field(max_length=3)
    excluded: list[_Excluded] = Field(max_length=3)


class _Baseline(_Strict):
    # This is an observed native identity, not a selectable pool entry.
    # Existing native selection accepts opaque strings outside _Selector.
    model: str | None
    thinking: ThinkingLevel | None
    source: Literal["native_get_state"]


class _Proposal(_Strict):
    id: _Label
    model: _Selector
    thinking: ThinkingLevel


class _Probe(_Strict):
    attempted: bool
    stop_returned: bool


class _JevModel(_Strict):
    requested: _Identifier
    observed: _Identifier | None


class _Usage(_Strict):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: _Number | None = None


class _Jev(_Strict):
    model: _JevModel | None = None
    provider: _Identifier | None = None
    response_id: _Identifier | None = None
    usage: _Usage | None = None
    input_sha256: _Digest | None = None
    request_bytes: int | None = Field(default=None, ge=0, le=MAX_REQUEST_BYTES)
    http_status: int | None = Field(default=None, ge=100, le=599)


class _Uncertainty(_Strict):
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[
        Annotated[str, StringConstraints(pattern=r"^(route_[0-2]|none|unclear)$")],
        Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)],
    ]


class _Outcome(_Strict):
    status: Literal[
        "bypassed",
        "unavailable",
        "no_comparison",
        "completed",
        "none",
        "unclear",
        "host_lifecycle_failure",
    ]
    reason: _Reason | None = None
    proposal: _Proposal | None = None
    baseline_startup: _Baseline | None = None
    probe: _Probe | None = None
    elapsed_seconds: _Number | None = None
    overrun: bool = False
    eligibility: _Eligibility | None = None
    jev: _Jev | None = None
    uncertainty: _Uncertainty | None = None


def _safe(model, value) -> dict:
    try:
        return model.model_validate(value).model_dump(mode="json", exclude_unset=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid routing evidence") from None


def _check_payload(record, payload, eligibility, baseline):
    """Bind export to the admitted summary and the exact safe comparison."""
    request = record["input"]
    requirements = {
        key: request[key]
        for key in (
            "input_modalities",
            "input_token_estimate",
            "output_token_estimate",
        )
    }
    requirements["thinking"] = baseline["thinking"]
    try:
        if set(payload) != {"model", "state", "questions"} or payload["model"] != MODEL:
            raise ValueError
        if payload["state"] != {
            "summary": request["summary"],
            "requirements": requirements,
        }:
            raise ValueError
        if set(payload["questions"]) != {"routing"}:
            raise ValueError
        question = payload["questions"]["routing"]
        if (
            set(question) != {"type", "instructions", "criteria"}
            or question["type"] != "choice"
        ):
            raise ValueError
        criteria = question["criteria"]
        keys = {f"route_{index}" for index in range(len(eligibility["candidates"]))}
        if set(criteria) != keys | {"none", "unclear"}:
            raise ValueError
        for value in (question["instructions"], criteria["none"], criteria["unclear"]):
            if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise ValueError
        for index, candidate in enumerate(eligibility["candidates"]):
            if json.loads(criteria[f"route_{index}"]) != candidate:
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError(
            "Routing payload must contain only the admitted summary and eligible comparison"
        ) from None


class RoutingJournal:
    def __init__(self, tasks):
        self.tasks = tasks

    @staticmethod
    def _read(db, task_id):
        row = db.execute(
            "SELECT record_json FROM task_model_routing WHERE task_id=?", (task_id,)
        ).fetchone()
        return json.loads(row["record_json"]) if row is not None else None

    @staticmethod
    def _write(db, task_id, record):
        db.execute(
            "UPDATE task_model_routing SET record_json=? WHERE task_id=?",
            (canonical_json(record), task_id),
        )

    def claim(self, task_id: str) -> dict | None:
        with closing(self.tasks.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, task_id)
            if record is None or record["state"] != "prepared":
                return None
            record.update(
                state="observing", attempt_id=str(uuid4()), claimed_at=time.time()
            )
            self._write(db, task_id, record)
        return record

    def reserve(
        self,
        task_id: str,
        attempt_id: str,
        *,
        payload: dict,
        input_sha256: str,
        eligibility: dict,
        baseline: dict,
    ) -> dict:
        safe_eligibility = _safe(_Eligibility, eligibility)
        safe_baseline = _safe(_Baseline, baseline)
        if (
            not isinstance(input_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None
        ):
            raise ValueError("Invalid routing request digest")
        encoded = canonical_json(payload)
        if (
            not isinstance(payload, dict)
            or len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES
        ):
            raise ValueError("Invalid routing request payload")
        with closing(self.tasks.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, task_id)
            if (
                record is None
                or record["state"] != "observing"
                or record["attempt_id"] != attempt_id
            ):
                raise ValueError("Routing observation is not reservable")
            self._check_candidates(record, safe_eligibility)
            if len(safe_eligibility["candidates"]) < 2 or any(
                candidate["thinking"]["level"] != safe_baseline["thinking"]
                for candidate in safe_eligibility["candidates"]
            ):
                raise ValueError(
                    "Routing comparison must preserve baseline thinking for at least two candidates"
                )
            _check_payload(record, payload, safe_eligibility, safe_baseline)
            record.update(
                state="reserved",
                reserved_at=time.time(),
                payload=json.loads(encoded),
                request_sha256=input_sha256,
                eligibility=safe_eligibility,
                baseline_startup=safe_baseline,
            )
            self._write(db, task_id, record)
        return record

    @staticmethod
    def _check_candidates(record, eligibility):
        policy = RoutingPolicy.model_validate(record["policy"])
        pool = {route.id: route for route in policy.routes}
        seen = set()
        for candidate in eligibility["candidates"] + eligibility["excluded"]:
            route = pool.get(candidate["id"])
            if (
                route is None
                or candidate["model"] != route.model
                or candidate["id"] in seen
            ):
                raise ValueError("Routing evidence does not match frozen pool")
            if (
                "description" in candidate
                and candidate["description"] != route.description
            ):
                raise ValueError("Routing description does not match frozen pool")
            seen.add(candidate["id"])

    def finish(self, task_id: str, attempt_id: str, outcome: dict) -> dict:
        safe = _safe(_Outcome, outcome)
        with closing(self.tasks.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            record = self._read(db, task_id)
            if record is None or record["attempt_id"] != attempt_id:
                raise ValueError("Routing observation does not belong to this attempt")
            if record["state"] == "terminal":
                if record.get("outcome") == safe:
                    return record
                raise ValueError("Routing outcome is immutable")
            if record["state"] not in ("observing", "reserved"):
                raise ValueError("Routing observation is not finishable")
            for field in ("eligibility", "baseline_startup"):
                if (
                    field in safe
                    and record.get(field) is not None
                    and record[field] != safe[field]
                ):
                    raise ValueError("Reserved routing evidence is immutable")
            if safe.get("eligibility") is not None:
                self._check_candidates(record, safe["eligibility"])
            proposal = safe.get("proposal")
            if (
                safe["status"] in ("completed", "none", "unclear")
                and record["state"] != "reserved"
            ):
                raise ValueError("Routing choice requires a send reservation")
            if safe["status"] == "completed" and proposal is None:
                raise ValueError("Completed routing choice requires a proposal")
            if proposal is not None:
                eligible = record.get("eligibility", {}).get("candidates", [])
                if (
                    record["state"] != "reserved"
                    or safe["status"] != "completed"
                    or not any(
                        candidate["id"] == proposal["id"]
                        and candidate["model"] == proposal["model"]
                        and candidate["thinking"]["level"] == proposal["thinking"]
                        for candidate in eligible
                    )
                ):
                    raise ValueError(
                        "Routing proposal must name a reserved eligible candidate"
                    )
            record.update(safe)
            record.update(
                state="terminal", applied=False, finished_at=time.time(), outcome=safe
            )
            self._write(db, task_id, record)
        return record

    def view(self, task: dict, details: bool = False) -> dict | None:
        with closing(self.tasks.connect()) as db:
            record = self._read(db, task["task_id"])
        if record is None:
            return None
        result = {
            key: record.get(key)
            for key in (
                "mode",
                "state",
                "status",
                "reason",
                "proposal",
                "baseline_startup",
                "policy_sha256",
                "elapsed_seconds",
                "overrun",
                "jev",
                "uncertainty",
                "probe",
            )
        }
        result["applied"] = False
        result["send_state"] = (
            "may_have_been_sent"
            if record.get("reserved_at") is not None
            else "not_sent"
        )
        if (
            record["state"] in ("observing", "reserved")
            and task.get("status") is not None
            and task["status"] not in ACTIVE
        ):
            result.update(status="unresolved", reason="observation_interrupted")
        if details:
            result["eligibility"] = record.get("eligibility")
        return result
