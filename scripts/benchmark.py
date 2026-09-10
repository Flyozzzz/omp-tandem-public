"""Validate collected benchmark JSONL against a frozen plan; never run agents."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = Path(__file__).resolve().parents[1] / "config/benchmark-result.schema.json"
ARMS = ("one_agent", "self_review", "naive_handoff", "tandem_independent")
REGIMES = ("natural", "equal_compute")
SHARED = (
    "experiment_id",
    "protocol_digest",
    "dataset_digest",
    "experiment_digest",
    "versions",
)
USAGE_METRICS = ("cost_usd", "input_tokens", "output_tokens")


def reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite numeric value")
    if isinstance(value, dict):
        for child in value.values():
            finite(child)
    elif isinstance(value, list):
        for child in value:
            finite(child)


def decode(text):
    value = json.loads(
        text, parse_constant=reject_constant, object_pairs_hook=unique_object
    )
    finite(value)
    return value


def validate(value, schema, definition, label):
    finite(value)
    validator = Draft202012Validator(
        {**schema, "$ref": f"#/$defs/{definition}"}, format_checker=FormatChecker()
    )
    error = next(validator.iter_errors(value), None)
    if error:
        location = ".".join(map(str, error.absolute_path)) or "<root>"
        raise ValueError(f"{label}: {location}: {error.message}")


def arm_order(plan, task_id, repetition, regime):
    """Frozen SHA-256 permutation, independent of Python PRNG implementation."""

    def rank(arm):
        payload = [plan["randomization_seed"], task_id, repetition, regime, arm]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        ).digest()

    return sorted(ARMS, key=rank)


def record_key(record):
    return (
        record["task_id"],
        record["repetition"],
        record["budget"]["regime"],
        record["arm"],
    )


def validate_plan(plan, schema):
    validate(plan, schema, "plan", "plan")
    tasks = plan["tasks"]
    if len({task["task_id"] for task in tasks}) != len(tasks):
        raise ValueError("plan: duplicate task_id")
    if {task["stratum"] for task in tasks} != {
        "bug",
        "small_feature",
        "architectural_ambiguity",
        "review",
    }:
        raise ValueError("plan: all four task strata are required")
    if {budget["regime"] for budget in plan["budgets"]} != set(REGIMES):
        raise ValueError(
            "plan: exactly one natural and one equal_compute budget required"
        )


def validate_record(record, plan, schema):
    validate(record, schema, "record", "record")
    for field in SHARED:
        if record[field] != plan[field]:
            raise ValueError(f"record {record['record_id']}: incompatible {field}")
    task = next(
        (task for task in plan["tasks"] if task["task_id"] == record["task_id"]), None
    )
    if task is None or any(
        record[field] != task[field] for field in ("stratum", "input_digest")
    ):
        raise ValueError("record: task/input digest/stratum differs from frozen plan")
    regime = record["budget"]["regime"]
    expected_budget = next(
        budget for budget in plan["budgets"] if budget["regime"] == regime
    )
    if record["budget"] != expected_budget:
        raise ValueError(
            "record: incompatible budget; do not pair different limits or policies"
        )
    if record["repetition"] > plan["repetitions"]:
        raise ValueError("record: unplanned repetition")
    arm = record["arm"]
    if record["arm_config_digest"] != plan["arm_config_digests"][arm]:
        raise ValueError("record: incompatible arm configuration")
    order = arm_order(plan, record["task_id"], record["repetition"], regime)
    if record["order"] != order.index(arm) + 1:
        raise ValueError("record: order differs from frozen randomization")
    provenance = record["provenance"]
    start = datetime.fromisoformat(provenance["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(provenance["finished_at"].replace("Z", "+00:00"))
    if end < start:
        raise ValueError("record: finished_at precedes started_at")
    has_peer = arm in ("naive_handoff", "tandem_independent")
    for role in ("coordinator", "peer"):
        usage = record["usage"][role]
        applicable = role == "coordinator" or has_peer
        for name in USAGE_METRICS:
            metric = usage[name]
            if (metric["status"] != "not_applicable") != applicable:
                raise ValueError(f"record: invalid {role} applicability")
            if (
                name.endswith("tokens")
                and metric["value"] is not None
                and metric["value"] % 1
            ):
                raise ValueError("record: token counts must be integers")
        if (
            any(
                usage[name]["status"] in ("complete", "partial")
                for name in USAGE_METRICS
            )
            and not usage["evidence"]
        ):
            raise ValueError(f"record: measured {role} usage requires evidence")
    if record["human_seconds"]["status"] == "not_applicable":
        raise ValueError(
            "record: human time is measured (possibly zero) or missing, not inapplicable"
        )
    if not has_peer and record["outcome"]["peer_disagreements"] is not None:
        raise ValueError("record: peer disagreements must be null without a peer")
    judgment = record["judgment"]
    if judgment and judgment["rubric_digest"] != plan["rubric_digest"]:
        raise ValueError("record: incompatible grading rubric")
    outcome = record["outcome"]
    cost = total_usage(record, "cost_usd")
    exceeded = record["wall_time_seconds"] > expected_budget["wall_limit_seconds"]
    limit = expected_budget["compute_limit_usd"]
    exceeded = exceeded or (
        limit is not None and cost["value"] is not None and cost["value"] > limit
    )
    if exceeded and outcome["budget_compliance"] != "exceeded":
        raise ValueError("record: observed budget overrun must be marked exceeded")
    if (
        limit is not None
        and cost["status"] != "complete"
        and outcome["budget_compliance"] == "within"
    ):
        raise ValueError(
            "record: incomplete total cost cannot establish equal-compute compliance"
        )


def total_usage(record, name):
    applicable = [
        record["usage"][role][name]
        for role in ("coordinator", "peer")
        if record["usage"][role][name]["status"] != "not_applicable"
    ]
    known = [metric["value"] for metric in applicable if metric["value"] is not None]
    if all(metric["status"] == "complete" for metric in applicable):
        return {"status": "complete", "value": sum(known)}
    return {
        "status": "partial" if known else "missing",
        "value": sum(known) if known else None,
    }


def metrics(record):
    result = {f"total_{name}": total_usage(record, name) for name in USAGE_METRICS}
    for role in ("coordinator", "peer"):
        for name in USAGE_METRICS:
            result[f"{role}_{name}"] = record["usage"][role][name]
    result["wall_time_seconds"] = {
        "status": "complete",
        "value": record["wall_time_seconds"],
    }
    result["human_seconds"] = record["human_seconds"]
    judgment = record["judgment"]
    for name in (
        "quality_score",
        "false_positives",
        "new_regressions",
        "disagreements",
    ):
        available = judgment is not None and (
            name == "disagreements" or judgment["adjudication"] != "unresolved"
        )
        result[name] = {
            "status": "complete" if available else "missing",
            "value": judgment[name] if available else None,
        }
    disagreements = record["outcome"]["peer_disagreements"]
    status = "complete" if disagreements is not None else "missing"
    if record["arm"] in ("one_agent", "self_review"):
        status = "not_applicable"
    result["peer_disagreements"] = {"status": status, "value": disagreements}
    return result


def summarize(values, planned):
    statuses = Counter(value["status"] for value in values)
    complete = [value["value"] for value in values if value["status"] == "complete"]
    known = [value["value"] for value in values if value["value"] is not None]
    return {
        "planned": planned,
        "records": len(values),
        "absent_records": planned - len(values),
        "coverage": {
            status: statuses[status]
            for status in ("complete", "partial", "missing", "not_applicable")
        },
        "complete_sum": sum(complete) if complete else None,
        "complete_mean": sum(complete) / len(complete) if complete else None,
        "known_lower_bound_sum": sum(known) if known else None,
    }


def analyze(plan, records, schema):
    validate_plan(plan, schema)
    indexed = {}
    ids = set()
    for record in records:
        validate_record(record, plan, schema)
        key = record_key(record)
        if key in indexed or record["record_id"] in ids:
            raise ValueError(
                f"duplicate record or planned arm slot: {record['record_id']}"
            )
        ids.add(record["record_id"])
        indexed[key] = record
    metric_names = [
        f"{role}_{name}"
        for role in ("total", "coordinator", "peer")
        for name in USAGE_METRICS
    ]
    metric_names += [
        "wall_time_seconds",
        "human_seconds",
        "quality_score",
        "false_positives",
        "new_regressions",
        "disagreements",
        "peer_disagreements",
    ]
    measured = {key: metrics(record) for key, record in indexed.items()}
    report = {
        "schema_version": 1,
        "kind": "collected_measurement_summary",
        "experiment_id": plan["experiment_id"],
        "record_count": len(records),
        "arms": [],
        "paired": [],
        "missing_slots": [],
    }
    # Overall and per-stratum summaries keep heterogeneous task mixes visible.
    for regime in REGIMES:
        for stratum in (
            None,
            "bug",
            "small_feature",
            "architectural_ambiguity",
            "review",
        ):
            tasks = [
                task
                for task in plan["tasks"]
                if stratum is None or task["stratum"] == stratum
            ]
            pairs = [
                (task["task_id"], repeat, regime)
                for task in tasks
                for repeat in range(1, plan["repetitions"] + 1)
            ]
            for arm in ARMS:
                keys = [(*pair, arm) for pair in pairs if (*pair, arm) in indexed]
                report["arms"].append(
                    {
                        "arm": arm,
                        "regime": regime,
                        "stratum": stratum or "all",
                        "planned": len(pairs),
                        "records": len(keys),
                        "outcomes": dict(
                            Counter(indexed[key]["outcome"]["status"] for key in keys)
                        ),
                        "budget_compliance": dict(
                            Counter(
                                indexed[key]["outcome"]["budget_compliance"]
                                for key in keys
                            )
                        ),
                        "adjudication": dict(
                            Counter(
                                indexed[key]["judgment"]["adjudication"]
                                if indexed[key]["judgment"]
                                else "ungraded"
                                for key in keys
                            )
                        ),
                        "metrics": {
                            name: summarize(
                                [measured[key][name] for key in keys], len(pairs)
                            )
                            for name in metric_names
                        },
                    }
                )
            for left, right in itertools.combinations(ARMS, 2):
                present = [
                    pair
                    for pair in pairs
                    if (*pair, left) in indexed and (*pair, right) in indexed
                ]
                within = [
                    pair
                    for pair in present
                    if all(
                        indexed[(*pair, arm)]["outcome"]["budget_compliance"]
                        == "within"
                        for arm in (left, right)
                    )
                ]
                deltas = {}
                for name in metric_names:
                    eligible = [
                        pair
                        for pair in within
                        if all(
                            measured[(*pair, arm)][name]["status"] == "complete"
                            for arm in (left, right)
                        )
                    ]
                    values = [
                        measured[(*pair, right)][name]["value"]
                        - measured[(*pair, left)][name]["value"]
                        for pair in eligible
                    ]
                    deltas[name] = {
                        "complete_pairs": len(values),
                        "unavailable_pairs": len(pairs) - len(values),
                        "mean_right_minus_left": sum(values) / len(values)
                        if values
                        else None,
                    }
                report["paired"].append(
                    {
                        "left": left,
                        "right": right,
                        "regime": regime,
                        "stratum": stratum or "all",
                        "planned_pairs": len(pairs),
                        "present_pairs": len(present),
                        "within_budget_pairs": len(within),
                        "metrics": deltas,
                    }
                )
        for task in plan["tasks"]:
            for repeat in range(1, plan["repetitions"] + 1):
                for arm in ARMS:
                    if (task["task_id"], repeat, regime, arm) not in indexed:
                        report["missing_slots"].append(
                            {
                                "task_id": task["task_id"],
                                "repetition": repeat,
                                "regime": regime,
                                "arm": arm,
                            }
                        )
    finite(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "records", type=Path, help="Actual measurements, one JSON object per line"
    )
    parser.add_argument(
        "--plan", required=True, type=Path, help="Approved frozen benchmark_plan JSON"
    )
    args = parser.parse_args(argv)
    try:
        schema = decode(SCHEMA.read_text(encoding="utf-8"))
        plan = decode(args.plan.read_text(encoding="utf-8"))
        records = []
        with args.records.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    records.append(decode(line))
                except ValueError as error:
                    raise ValueError(
                        f"{args.records}:{line_number}: {error}"
                    ) from error
        report = analyze(plan, records, schema)
        print(json.dumps(report, indent=2, allow_nan=False))
    except (OSError, ValueError, OverflowError) as error:
        print(f"benchmark: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
