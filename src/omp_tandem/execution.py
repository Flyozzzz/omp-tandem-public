"""Computation settings and turn-local native accounting (never session totals)."""

import hashlib
import json
import math
import threading
from collections import Counter
from typing import Literal

from omp_rpc import ThinkingLevel
from pydantic import BaseModel, ConfigDict, Field

PROFILES = {
    "quick": {"thinking": "low", "timeout_seconds": 600},
    "balanced": {"thinking": "high", "timeout_seconds": 1800},
    "deep": {"thinking": "high", "timeout_seconds": 3600},
}
TOKEN_FIELDS = {
    "input": "input",
    "output": "output",
    "cache_read": "cacheRead",
    "cache_write": "cacheWrite",
    "total": "totalTokens",
}


def profile_catalog() -> dict:
    """Return independent descriptions of profile defaults, not effective settings."""
    return {
        name: {
            **settings,
            "description": (
                f"Defaults to {settings['thinking']} thinking with a "
                f"{settings['timeout_seconds']}-second deadline."
                + (
                    " Uses the same thinking level as balanced with a longer "
                    "deadline, not higher reasoning."
                    if name == "deep"
                    else ""
                )
            ),
        }
        for name, settings in PROFILES.items()
    }


class ExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["quick", "balanced", "deep"] = Field(
        default="balanced",
        description=(
            "Profile defaults: "
            + " ".join(
                f"{name}: {settings['description']}"
                for name, settings in profile_catalog().items()
            )
            + " These are default choices; overrides, effective settings, and "
            "actual settings are reported separately."
        ),
    )
    model: str | None = Field(default=None, min_length=1, pattern=r"\S")
    thinking: ThinkingLevel | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=7200, strict=True)


def resolve_execution(options=None, *, previous=None, model=None, timeout_seconds=None):
    """Explicit timeout > option override > selected profile > inherited settings."""
    options = ExecutionOptions.model_validate(options or {})
    requested = options.model_dump(exclude_unset=True, exclude_none=True)
    effective = {
        "profile": "balanced",
        "model": model or None,
        **PROFILES["balanced"],
    }
    if previous:
        effective.update(previous)
    if "profile" in requested:
        effective.update(profile=options.profile, **PROFILES[options.profile])
    effective.update(
        {key: value for key, value in requested.items() if key != "profile"}
    )
    if timeout_seconds is not None:
        explicit = ExecutionOptions(timeout_seconds=timeout_seconds).timeout_seconds
        effective["timeout_seconds"] = explicit
        requested["api_timeout_seconds"] = explicit
    return {"requested": requested, "effective": effective}


def _number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def _metric(values):
    known = [value for value in values if value is not None]
    complete = bool(values) and len(known) == len(values)
    return {
        "value": sum(known) if complete else None,
        "known_subtotal": sum(known) if known else None,
        "status": "complete" if complete else "partial" if known else "unknown",
    }


def empty_usage():
    return {
        "response_count": None,
        "models": [],
        "tokens": {key: _metric([]) for key in TOKEN_FIELDS},
        "cost": _metric([]),
        "coverage": "unknown",
        "provenance": "unavailable",
    }


class TurnUsage:
    """Count message_end once; agent_end only fills missing message occurrences.

    Native agent_end repeats the current run's messages, not session history.
    Native SessionStats is cumulative and its SDK parser invents zero defaults;
    neither is used as an additive counter. Hashes retain no response content.
    """

    def __init__(self, persist):
        self.persist = persist
        self.lock = threading.Lock()
        self.ended = Counter()
        self.counted = Counter()
        self.count = 0
        self.models = set()
        self.sums = dict.fromkeys(TOKEN_FIELDS, 0)
        self.known = dict.fromkeys(TOKEN_FIELDS, 0)
        self.cost_sum = 0
        self.cost_known = 0
        self.terminal = False

    @staticmethod
    def _key(message):
        return hashlib.sha256(
            json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
        ).digest()

    def _add(self, message):
        self.count += 1
        model, provider = message.get("model"), message.get("provider")
        if model and provider:
            self.models.add(f"{provider}/{model}")
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        # Providers initialize absent usage to zero too; an all-zero response
        # cannot prove that no tokens were consumed, especially on failure.
        synthetic = not any(
            _number(usage.get(field)) for field in TOKEN_FIELDS.values()
        )
        for key, field in TOKEN_FIELDS.items():
            value = None if synthetic else _number(usage.get(field))
            if value is not None:
                self.sums[key] += value
                self.known[key] += 1
        cost = usage.get("cost")
        total = _number(cost.get("total")) if isinstance(cost, dict) else None
        # OMP's zero-price catalog/provider entries do not attest free billing.
        # Keep zero unknown rather than presenting a missing tariff as $0.
        if not synthetic and total is not None and total > 0:
            self.cost_sum += total
            self.cost_known += 1

    def message_end(self, event):
        message = event.message
        if message.get("role") != "assistant":
            return
        with self.lock:
            key = self._key(message)
            self.ended[key] += 1
            if self.ended[key] > self.counted[key]:
                self.counted[key] += 1
                self._add(message)
                self.persist(self._snapshot())

    def agent_end(self, event):
        with self.lock:
            occurrences = Counter()
            for message in event.messages:
                if message.get("role") != "assistant":
                    continue
                key = self._key(message)
                occurrences[key] += 1
                if occurrences[key] > self.counted[key]:
                    self.counted[key] += 1
                    self._add(message)
            if event.is_terminal is not False:
                self.terminal = True
            self.persist(self._snapshot())

    def _snapshot(self):
        def metric(total, known):
            return {
                "value": total if known and known == self.count else None,
                "known_subtotal": total if known else None,
                "status": "complete"
                if known and known == self.count
                else "partial"
                if known
                else "unknown",
            }

        return {
            "response_count": self.count,
            "models": sorted(self.models),
            "tokens": {
                key: metric(self.sums[key], self.known[key]) for key in TOKEN_FIELDS
            },
            "cost": metric(self.cost_sum, self.cost_known),
            "coverage": "complete"
            if self.terminal
            else "partial"
            if self.count
            else "unknown",
            "provenance": "omp_rpc.message_end+agent_end; current turn only",
            "cost_provenance": "native reported cost, not a billing invoice; zero/unavailable tariffs unknown",
        }

    def snapshot(self, *, interrupted=False):
        with self.lock:
            result = self._snapshot()
            if interrupted and result["coverage"] == "complete":
                result["coverage"] = "partial"
            return result


def task_usage(task):
    usage = (
        json.loads(task["accounting_json"])
        if task.get("accounting_json")
        else empty_usage()
    )
    if (
        task.get("status") in ("failed", "cancelled", "interrupted")
        and usage["coverage"] == "complete"
    ):
        usage["coverage"] = "partial"
    return {
        **usage,
        "started_at": task.get("started_at"),
        "ended_at": task.get("ended_at"),
        "duration_seconds": task.get("duration_seconds"),
        "duration_provenance": "worker monotonic elapsed"
        if task.get("duration_seconds") is not None
        else "unavailable",
    }


def conversation_usage(tasks):
    """Sum disjoint task turns, preserving unknown and partially known metrics."""
    usages = [task_usage(task) for task in tasks]

    def combine(metrics):
        result = _metric([metric["value"] for metric in metrics])
        subtotal = _metric([metric["known_subtotal"] for metric in metrics])[
            "known_subtotal"
        ]
        result["known_subtotal"] = subtotal
        if result["value"] is None and subtotal is not None:
            result["status"] = "partial"
        return result

    return {
        "task_count": len(usages),
        "response_count": _metric([usage["response_count"] for usage in usages]),
        "models": sorted({model for usage in usages for model in usage["models"]}),
        "tokens": {
            key: combine([usage["tokens"][key] for usage in usages])
            for key in TOKEN_FIELDS
        },
        "cost": combine([usage["cost"] for usage in usages]),
        "duration_seconds": _metric([usage["duration_seconds"] for usage in usages]),
        "coverage": "complete"
        if usages and all(usage["coverage"] == "complete" for usage in usages)
        else "partial"
        if any(usage["response_count"] is not None for usage in usages)
        else "unknown",
        "provenance": "sum of disjoint task turns; no cumulative session counters",
    }
