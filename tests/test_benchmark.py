"""Synthetic analyzer regressions; these fixtures are not benchmark evidence."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts import benchmark


def metric(status="complete", value=1):
    return {"status": status, "value": value}


def frozen_plan():
    artifact = {"uri": "synthetic-fixture-only", "sha256": "a" * 64}
    strata = ("bug", "small_feature", "architectural_ambiguity", "review")
    return {
        "schema_version": 1,
        "kind": "benchmark_plan",
        "experiment_id": "synthetic-test",
        "protocol_digest": "a" * 64,
        "dataset_digest": "b" * 64,
        "experiment_digest": "c" * 64,
        "rubric_digest": "d" * 64,
        "versions": {
            "python": "synthetic",
            "tandem": "synthetic",
            "omp": "synthetic",
            "omp_rpc": "synthetic",
            "coordinator_model": "synthetic",
            "peer_model": "synthetic",
            "environment_digest": "e" * 64,
        },
        "arm_config_digests": dict.fromkeys(benchmark.ARMS, "f" * 64),
        "tasks": [
            {
                "task_id": f"synthetic-{index}",
                "stratum": strata[index % 4],
                "input_digest": "a" * 64,
            }
            for index in range(20)
        ],
        "repetitions": 1,
        "budgets": [
            {
                "regime": regime,
                "policy_digest": "b" * 64,
                "wall_limit_seconds": 100,
                "compute_limit_usd": None if regime == "natural" else 10,
            }
            for regime in benchmark.REGIMES
        ],
        "randomization_seed": 13,
        "approval": artifact,
    }


def measurement(plan, arm="one_agent", regime="natural", task_index=0):
    artifact = {"uri": "synthetic-fixture-only", "sha256": "a" * 64}
    has_peer = arm in ("naive_handoff", "tandem_independent")
    task = plan["tasks"][task_index]
    return {
        "schema_version": 1,
        "kind": "measurement",
        "record_id": f"{task['task_id']}-{arm}-{regime}",
        **{field: copy.deepcopy(plan[field]) for field in benchmark.SHARED},
        **task,
        "arm": arm,
        "arm_config_digest": plan["arm_config_digests"][arm],
        "repetition": 1,
        "order": benchmark.arm_order(plan, task["task_id"], 1, regime).index(arm) + 1,
        "budget": copy.deepcopy(
            next(budget for budget in plan["budgets"] if budget["regime"] == regime)
        ),
        "provenance": {
            "collector": "synthetic unit test",
            "started_at": "2000-01-01T00:00:00Z",
            "finished_at": "2000-01-01T00:00:04Z",
            "clock": "monotonic_whole_process",
            "artifacts": [artifact],
            "notes": "Invented unit-test input, never a measured result.",
        },
        "outcome": {
            "status": "completed",
            "reason": "synthetic",
            "budget_compliance": "within",
            "peer_disagreements": 0 if has_peer else None,
        },
        "wall_time_seconds": 4,
        "human_seconds": metric(),
        "usage": {
            "coordinator": {
                **{name: metric() for name in benchmark.USAGE_METRICS},
                "evidence": [artifact],
                "note": "synthetic",
            },
            "peer": {
                **{
                    name: metric() if has_peer else metric("not_applicable", None)
                    for name in benchmark.USAGE_METRICS
                },
                "evidence": [artifact] if has_peer else [],
                "note": "synthetic",
            },
        },
        "judgment": {
            "blind": True,
            "rubric_digest": plan["rubric_digest"],
            "evidence": artifact,
            "quality_score": 50,
            "false_positives": 0,
            "new_regressions": 0,
            "disagreements": 0,
            "adjudication": "agreed",
        },
    }


class BenchmarkAccountingTests(unittest.TestCase):
    def setUp(self):
        self.plan = frozen_plan()
        self.schema = json.loads(benchmark.SCHEMA.read_text(encoding="utf-8"))

    def report(self, *records):
        return benchmark.analyze(self.plan, list(records), self.schema)

    def arm(self, report, arm, regime="natural"):
        return next(
            row
            for row in report["arms"]
            if row["arm"] == arm and row["regime"] == regime and row["stratum"] == "all"
        )

    def pair(self, report, left, right, regime="natural"):
        return next(
            row
            for row in report["paired"]
            if row["left"] == left
            and row["right"] == right
            and row["regime"] == regime
            and row["stratum"] == "all"
        )

    def test_missing_coordinator_cost_is_not_free_peer_only_total(self):
        baseline = measurement(self.plan)
        tandem = measurement(self.plan, "tandem_independent")
        tandem["usage"]["coordinator"]["cost_usd"] = metric("missing", None)
        tandem["usage"]["peer"]["cost_usd"] = metric("complete", 3)
        report = self.report(baseline, tandem)
        total = self.arm(report, "tandem_independent")["metrics"]["total_cost_usd"]
        self.assertIsNone(total["complete_sum"])
        self.assertIsNone(total["complete_mean"])
        self.assertEqual(total["known_lower_bound_sum"], 3)
        self.assertEqual(total["coverage"]["partial"], 1)
        paired = self.pair(report, "one_agent", "tandem_independent")["metrics"]
        self.assertEqual(paired["total_cost_usd"]["complete_pairs"], 0)
        self.assertIsNone(paired["total_cost_usd"]["mean_right_minus_left"])
        self.assertEqual(paired["wall_time_seconds"]["complete_pairs"], 1)
        baseline_usage = self.arm(report, "one_agent")["metrics"]
        self.assertEqual(baseline_usage["total_cost_usd"]["complete_sum"], 1)
        self.assertEqual(
            baseline_usage["peer_cost_usd"]["coverage"]["not_applicable"], 1
        )
        self.assertIsNone(baseline_usage["peer_cost_usd"]["complete_sum"])

    def test_partial_amount_does_not_pollute_complete_mean(self):
        partial = measurement(self.plan)
        partial["usage"]["coordinator"]["cost_usd"] = metric("partial", 7)
        complete = measurement(self.plan, task_index=1)
        complete["usage"]["coordinator"]["cost_usd"] = metric("complete", 2)
        total = self.arm(self.report(partial, complete), "one_agent")["metrics"][
            "total_cost_usd"
        ]
        self.assertEqual(total["complete_mean"], 2)
        self.assertEqual(total["known_lower_bound_sum"], 9)
        self.assertEqual(total["absent_records"], 18)
        self.assertEqual(total["coverage"]["partial"], 1)

    def test_pairs_cannot_cross_tasks_or_budget_regimes(self):
        baseline = measurement(self.plan)
        different_task = measurement(self.plan, "self_review", task_index=1)
        other_regime = measurement(self.plan, "self_review", regime="equal_compute")
        report = self.report(baseline, different_task, other_regime)
        for regime in benchmark.REGIMES:
            pair = self.pair(report, "one_agent", "self_review", regime)
            self.assertEqual(pair["present_pairs"], 0)
            self.assertIsNone(pair["metrics"]["quality_score"]["mean_right_minus_left"])
        for changed in ("wall_limit_seconds", "compute_limit_usd", "policy_digest"):
            incompatible = measurement(self.plan, "self_review", regime="equal_compute")
            incompatible["budget"][changed] = (
                "c" * 64 if changed == "policy_digest" else 99
            )
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.report(incompatible)

    def test_noncompliance_and_unresolved_grades_remain_visible_not_paired(self):
        baseline = measurement(self.plan, regime="equal_compute")
        tandem = measurement(self.plan, "tandem_independent", regime="equal_compute")
        tandem["outcome"]["status"] = "timeout"
        tandem["outcome"]["budget_compliance"] = "exceeded"
        tandem["wall_time_seconds"] = 101
        report = self.report(baseline, tandem)
        self.assertEqual(
            self.arm(report, "tandem_independent", "equal_compute")["outcomes"],
            {"timeout": 1},
        )
        self.assertEqual(
            self.pair(report, "one_agent", "tandem_independent", "equal_compute")[
                "within_budget_pairs"
            ],
            0,
        )
        tandem = measurement(self.plan, "tandem_independent", regime="equal_compute")
        tandem["judgment"]["adjudication"] = "unresolved"
        tandem["judgment"]["disagreements"] = 2
        pair = self.pair(
            self.report(baseline, tandem),
            "one_agent",
            "tandem_independent",
            "equal_compute",
        )
        self.assertIsNone(pair["metrics"]["quality_score"]["mean_right_minus_left"])
        self.assertEqual(pair["metrics"]["disagreements"]["mean_right_minus_left"], 2)
        tandem["usage"]["coordinator"]["cost_usd"] = metric("missing", None)
        with self.assertRaises(ValueError):
            self.report(tandem)

    def test_absent_blocks_are_counted_against_the_plan(self):
        report = self.report()
        self.assertEqual(len(report["missing_slots"]), 20 * 4 * 2)
        total = self.arm(report, "one_agent")["metrics"]["total_cost_usd"]
        self.assertEqual(total["absent_records"], 20)
        self.assertIsNone(total["known_lower_bound_sum"])
        self.assertIsNone(total["complete_sum"])

    def test_duplicate_slots_ids_and_incompatible_inputs_fail(self):
        original = measurement(self.plan)
        duplicate_slot = copy.deepcopy(original)
        duplicate_slot["record_id"] = "different-id-same-slot"
        duplicate_id = measurement(self.plan, "self_review")
        duplicate_id["record_id"] = original["record_id"]
        for duplicate in (duplicate_slot, duplicate_id):
            with (
                self.subTest(duplicate=duplicate["arm"]),
                self.assertRaises(ValueError),
            ):
                self.report(original, duplicate)
        changed = copy.deepcopy(original)
        changed["input_digest"] = "f" * 64
        with self.assertRaises(ValueError):
            self.report(changed)
        changed = copy.deepcopy(original)
        changed["versions"]["coordinator_model"] = "different-synthetic-model"
        with self.assertRaises(ValueError):
            self.report(changed)

    def test_cli_rejects_bad_numbers_duplicate_keys_and_bad_records_without_output(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = root / "plan.json"
            records_path = root / "records.jsonl"
            plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
            valid = json.dumps(measurement(self.plan))
            bad_inputs = [
                valid.replace(
                    '"wall_time_seconds": 4', f'"wall_time_seconds": {number}'
                )
                for number in ("NaN", "Infinity", "-Infinity", "1e999")
            ]
            bad_inputs += [
                '{"kind":"measurement","kind":"measurement"}',
                "{}",
                valid + "\n" + valid,
            ]
            for payload in bad_inputs:
                records_path.write_text(payload, encoding="utf-8")
                output, errors = io.StringIO(), io.StringIO()
                with (
                    self.subTest(payload=payload[:60]),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    code = benchmark.main(["--plan", str(plan_path), str(records_path)])
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertTrue(errors.getvalue().startswith("benchmark:"))
            records_path.write_text(valid, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = benchmark.main(["--plan", str(plan_path), str(records_path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["record_count"], 1)


if __name__ == "__main__":
    unittest.main()
