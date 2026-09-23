"""Policy hard boundaries and durable shadow observations, without providers."""

import copy
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from omp_tandem.jev_client import MODEL, canonical_json
from omp_tandem.model_routing_state import (
    RoutingInput,
    RoutingJournal,
    RoutingPolicy,
    eligible_routes,
    load_routing_policy,
    prepare_routing,
)


def policy_data():
    return {
        "allow_external_summary": True,
        "routes": [
            {
                "id": "first",
                "model": "native/first",
                "description": "Careful reasoning",
                "tool_calling": True,
            },
            {
                "id": "second",
                "model": "native/second",
                "description": "Quick analysis",
                "tool_calling": True,
            },
        ],
    }


def input_data():
    return {
        "summary": "Review the proposed change",
        "allow_external_summary": True,
        "input_token_estimate": 1000,
        "output_token_estimate": 200,
    }


def catalog():
    return [
        {
            "provider": "native",
            "id": name,
            "contextWindow": 8000,
            "maxTokens": 2000,
            "input": ["text", "image"],
            "reasoning": True,
            "thinking": {"efforts": ["low", "medium", "high"], "requiresEffort": False},
            "cost": {"input": 2, "output": 4},
            "headers": {"Authorization": "private-token"},
            "baseUrl": "https://private-host/token",
            "compat": {"arbitrary_private_metadata": "secret"},
        }
        for name in ("first", "second")
    ]


class RoutingPolicyTests(unittest.TestCase):
    def test_policy_rejects_aliases_duplicates_and_non_shadow_settings(self):
        variants = []
        for field, value in (
            ("id", " first"),
            ("model", "first"),
            ("model", " native/first"),
            ("tool_calling", "true"),
        ):
            data = policy_data()
            data["routes"][0][field] = value
            variants.append(data)
        for field in ("id", "model"):
            data = policy_data()
            data["routes"][1][field] = data["routes"][0][field]
            variants.append(data)
        variants.extend(
            [
                {**policy_data(), "mode": "active"},
                {**policy_data(), "routes": policy_data()["routes"][:1]},
                {**policy_data(), "default_model": "native/first"},
                {**policy_data(), "budget_seconds": float("nan")},
            ]
        )
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                RoutingPolicy.model_validate(data)

    def test_request_limits_are_strict_and_mutated_instances_are_revalidated(self):
        for changes in (
            {"input_token_estimate": True},
            {"output_token_estimate": 0},
            {"input_modalities": []},
            {"input_modalities": ["image", "image"]},
            {"max_candidate_cost_usd": float("inf")},
            {"summary": "  "},
            {"allow_external_summary": 1},
            {"api_key": "private-token"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RoutingInput.model_validate(input_data() | changes)
        request = RoutingInput.model_validate(input_data())
        request.input_token_estimate = -1
        with self.assertRaises(ValidationError):
            prepare_routing(
                RoutingPolicy.model_validate(policy_data()),
                {},
                {"requested": {"routing": request}},
            )

    def test_loader_bounds_regular_utf8_json_and_sanitizes_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(canonical_json(policy_data()))
            self.assertEqual(load_routing_policy(path).routes[0].model, "native/first")
            for raw in (
                b'{"private-token":',
                b'{"allow_external_summary":true,"allow_external_summary":false}',
                b'"' + b"private-token" * 3000 + b'"',
                b"\xffprivate-token",
                b'{"budget_seconds":NaN}',
                b'{"routes": "private-token"}',
            ):
                path.write_bytes(raw)
                with self.assertRaises(ValueError) as error:
                    load_routing_policy(path)
                self.assertNotIn("private-token", str(error.exception))
                self.assertNotIn(str(path), str(error.exception))
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                load_routing_policy(path)

    def test_each_export_gate_is_independent_and_no_inherited_input_is_used(self):
        policy = RoutingPolicy.model_validate(policy_data())
        settings = {"requested": {"routing": input_data()}}
        prepared = prepare_routing(policy, {"task_id": "temporary"}, settings)
        self.assertEqual(prepared["state"], "prepared")
        self.assertEqual(
            prepared, prepare_routing(policy, {"task_id": "final"}, settings)
        )
        request_denied = copy.deepcopy(settings)
        request_denied["requested"]["routing"]["allow_external_summary"] = False
        self.assertEqual(
            prepare_routing(policy, {}, request_denied)["reason"],
            "request_export_not_allowed",
        )
        denied = RoutingPolicy.model_validate(
            policy_data() | {"allow_external_summary": False}
        )
        self.assertEqual(
            prepare_routing(denied, {}, settings)["reason"],
            "operator_export_not_allowed",
        )
        self.assertEqual(
            prepare_routing(policy, {}, {"effective": settings["requested"]})["reason"],
            "missing_input",
        )
        self.assertIsNone(
            prepare_routing(None, {}, {"effective": settings["requested"]})
        )
        self.assertEqual(prepare_routing(None, {}, settings)["reason"], "disabled")
        for task, options, requested, reason in (
            ({}, {}, {"model": "native/first"}, "explicit_model"),
            ({}, {"process_model": "native/first"}, {}, "configured_model"),
            ({"previous_task_id": "prior"}, {}, {}, "continuation"),
            ({"review_run_id": "snapshot"}, {}, {}, "snapshot_review"),
            ({}, {"managed": True}, {}, "managed_attempt"),
        ):
            result = prepare_routing(
                policy,
                task,
                {"requested": settings["requested"] | requested},
                **options,
            )
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["state"], "terminal")
        policy.routes[0].description = "changed after admission"
        settings["requested"]["routing"]["summary"] = "changed after admission"
        self.assertEqual(
            prepared["policy"]["routes"][0]["description"], "Careful reasoning"
        )
        self.assertEqual(prepared["input"]["summary"], "Review the proposed change")


class EligibilityTests(unittest.TestCase):
    def choose(self, raw=None, *, policy=None, request=None, thinking="medium"):
        return eligible_routes(
            RoutingPolicy.model_validate(policy or policy_data()),
            RoutingInput.model_validate(request or input_data()),
            catalog() if raw is None else raw,
            thinking,
        )

    def test_raw_unknown_prices_cannot_pass_ceiling_but_explicit_zero_can(self):
        raw = catalog()
        raw[0].pop("cost")
        raw[1]["cost"] = {"input": 0, "output": 0}
        unconstrained = self.choose(raw)
        self.assertIsNone(unconstrained["candidates"][0]["cost"]["estimated_usd"])
        constrained = self.choose(
            raw, request=input_data() | {"max_candidate_cost_usd": 0}
        )
        self.assertEqual([row["id"] for row in constrained["candidates"]], ["second"])
        self.assertIn("cost_unknown", constrained["excluded"][0]["reasons"])
        self.assertEqual(
            constrained["candidates"][0]["cost"]["source"], "native_catalog"
        )
        self.assertEqual(constrained["candidates"][0]["cost"]["estimated_usd"], 0)
        for cost in (
            {"input": False, "output": 0},
            {"input": "0", "output": 0},
            {"input": -1, "output": 0},
            {"input": 0, "output": float("inf")},
        ):
            raw[0]["cost"] = cost
            self.assertIn(
                "cost_unknown",
                self.choose(raw, request=input_data() | {"max_candidate_cost_usd": 1})[
                    "excluded"
                ][0]["reasons"],
            )

    def test_capacity_modalities_tools_and_cost_are_hard_candidate_constraints(self):
        for patch, reason in (
            ({"contextWindow": 1199}, "context_exceeded"),
            ({"contextWindow": None}, "context_unknown"),
            ({"maxTokens": 199}, "output_capacity_exceeded"),
            ({"maxTokens": True}, "output_capacity_unknown"),
            ({"input": None}, "modalities_unknown"),
            ({"input": ["image"]}, "modalities_incompatible"),
            ({"toolCalling": False}, "tools_incompatible"),
        ):
            raw = catalog()
            raw[0].update(patch)
            with self.subTest(reason=reason):
                self.assertIn(reason, self.choose(raw)["excluded"][0]["reasons"])
        policy = policy_data()
        policy["routes"][0]["tool_calling"] = False
        self.assertIn(
            "tools_not_declared", self.choose(policy=policy)["excluded"][0]["reasons"]
        )
        self.assertEqual(
            self.choose(request=input_data() | {"max_candidate_cost_usd": 0.0028})[
                "excluded"
            ],
            [],
        )
        self.assertEqual(
            len(
                self.choose(request=input_data() | {"max_candidate_cost_usd": 0.0027})[
                    "excluded"
                ]
            ),
            2,
        )

    def test_thinking_declarations_fill_unknowns_but_never_override_native_contradictions(
        self,
    ):
        policy = policy_data()
        policy["routes"][0]["thinking_levels"] = ["medium", "off"]
        raw = catalog()
        raw[0].pop("thinking")
        raw[0].pop("reasoning")
        self.assertEqual(
            self.choose(raw, policy=policy)["candidates"][0]["thinking"]["source"],
            "operator_declared",
        )
        self.assertIn("thinking_unknown", self.choose(raw)["excluded"][0]["reasons"])
        raw[0]["reasoning"] = False
        self.assertIn(
            "thinking_incompatible",
            self.choose(raw, policy=policy)["excluded"][0]["reasons"],
        )
        raw[0].update(reasoning=True, thinking={"efforts": ["high"]})
        self.assertIn(
            "thinking_incompatible",
            self.choose(raw, policy=policy)["excluded"][0]["reasons"],
        )
        raw[0]["thinking"]["requiresEffort"] = True
        self.assertIn(
            "thinking_incompatible",
            self.choose(raw, policy=policy, thinking="off")["excluded"][0]["reasons"],
        )

    def test_catalog_identity_is_exact_ambiguous_rows_fail_and_secrets_do_not_escape(
        self,
    ):
        raw = catalog()
        safe = self.choose(raw)
        encoded = canonical_json(safe)
        for secret in (
            "private-token",
            "private-host",
            "baseUrl",
            "headers",
            "compat",
            "secret",
        ):
            self.assertNotIn(secret, encoded)
        raw.append(copy.deepcopy(raw[0]))
        self.assertIn(
            "ambiguous_catalog_identity", self.choose(raw)["excluded"][0]["reasons"]
        )
        raw = catalog()
        raw[0]["provider"] = "NATIVE"
        self.assertIn("not_in_catalog", self.choose(raw)["excluded"][0]["reasons"])
        raw[0]["provider"] = "native/"
        self.assertIn("not_in_catalog", self.choose(raw)["excluded"][0]["reasons"])


class RoutingJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.tasks = SimpleNamespace(connect=self.connect)
        self.journal = RoutingJournal(self.tasks)
        self.task = {"task_id": "stable-task", "status": "running"}
        self.policy = RoutingPolicy.model_validate(policy_data())
        self.request = RoutingInput.model_validate(input_data())
        self.prepared = prepare_routing(
            self.policy, {}, {"requested": {"routing": self.request}}
        )
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE task_model_routing(task_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO task_model_routing VALUES (?, ?)",
                (self.task["task_id"], canonical_json(self.prepared)),
            )
        self.eligibility = eligible_routes(
            self.policy, self.request, catalog(), "medium"
        )
        self.baseline = {
            "model": "outside/original",
            "thinking": "medium",
            "source": "native_get_state",
        }
        self.payload = {
            "model": MODEL,
            "state": {
                "summary": self.request.summary,
                "requirements": {
                    "thinking": "medium",
                    "input_modalities": ["text"],
                    "input_token_estimate": 1000,
                    "output_token_estimate": 200,
                },
            },
            "questions": {
                "routing": {
                    "type": "choice",
                    "instructions": "Choose a model without executing it.",
                    "criteria": {
                        **{
                            f"route_{index}": canonical_json(candidate)
                            for index, candidate in enumerate(
                                self.eligibility["candidates"]
                            )
                        },
                        "none": "No suitable candidate.",
                        "unclear": "Insufficient information.",
                    },
                }
            },
        }

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def reserve(self, attempt):
        return self.journal.reserve(
            self.task["task_id"],
            attempt,
            payload=self.payload,
            input_sha256="a" * 64,
            eligibility=self.eligibility,
            baseline=self.baseline,
        )

    def test_concurrent_claims_have_one_owner_and_restart_never_replays(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _: RoutingJournal(self.tasks).claim(self.task["task_id"]),
                    range(4),
                )
            )
        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertIsNone(RoutingJournal(self.tasks).claim(self.task["task_id"]))
        view = self.journal.view(self.task | {"status": "interrupted"})
        self.assertEqual(
            (view["status"], view["send_state"]), ("unresolved", "not_sent")
        )
        self.assertIsNone(self.journal.view({"task_id": "other", "status": "running"}))
        with self.assertRaises(ValueError):
            self.reserve("different-attempt")
        self.reserve(claimed[0]["attempt_id"])
        self.assertIsNone(RoutingJournal(self.tasks).claim(self.task["task_id"]))
        with self.assertRaises(ValueError):
            self.reserve(claimed[0]["attempt_id"])
        view = self.journal.view(self.task | {"status": "cancelled"}, details=True)
        self.assertEqual(
            (view["status"], view["send_state"]), ("unresolved", "may_have_been_sent")
        )
        self.assertEqual(view["baseline_startup"]["model"], "outside/original")
        self.assertNotIn(self.request.summary, canonical_json(view))
        self.assertTrue(
            {"payload", "policy", "input", "attempt_id", "outcome"}.isdisjoint(view)
        )

    def test_terminal_outcomes_are_exactly_idempotent_and_cannot_rewrite_reservation(
        self,
    ):
        attempt = self.journal.claim(self.task["task_id"])["attempt_id"]
        reserved = self.reserve(attempt)
        self.assertEqual(reserved["input_sha256"], self.prepared["input_sha256"])
        outcome = {
            "status": "completed",
            "proposal": {
                "id": "second",
                "model": "native/second",
                "thinking": "medium",
            },
            "baseline_startup": self.baseline,
            "elapsed_seconds": 0.1,
            "overrun": False,
        }
        with self.assertRaises(ValueError):
            self.journal.finish(
                self.task["task_id"],
                attempt,
                outcome
                | {"baseline_startup": self.baseline | {"model": "native/second"}},
            )
        final = self.journal.finish(self.task["task_id"], attempt, outcome)
        self.assertEqual(
            RoutingJournal(self.tasks).finish(self.task["task_id"], attempt, outcome),
            final,
        )
        with self.assertRaises(ValueError):
            self.journal.finish(
                self.task["task_id"],
                attempt,
                outcome | {"status": "none", "proposal": None},
            )
        with self.assertRaises(ValueError):
            self.journal.finish(self.task["task_id"], "wrong-owner", outcome)
        view = self.journal.view(self.task | {"status": "completed"})
        self.assertEqual(view["proposal"]["model"], "native/second")
        self.assertEqual(view["baseline_startup"]["model"], "outside/original")
        self.assertFalse(view["applied"])
        self.assertNotIn("eligibility", view)

    def test_outcomes_cannot_propose_unreserved_models_or_capture_raw_metadata(self):
        attempt = self.journal.claim(self.task["task_id"])["attempt_id"]
        with self.assertRaises(ValueError):
            self.journal.finish(
                self.task["task_id"],
                attempt,
                {
                    "status": "completed",
                    "proposal": {
                        "id": "first",
                        "model": "native/first",
                        "thinking": "medium",
                    },
                },
            )
        bad = copy.deepcopy(self.eligibility)
        bad["candidates"][0]["headers"] = {"Authorization": "private-token"}
        with self.assertRaises(ValueError) as error:
            self.journal.reserve(
                self.task["task_id"],
                attempt,
                payload={},
                input_sha256="a" * 64,
                eligibility=bad,
                baseline=self.baseline,
            )
        self.assertNotIn("private-token", str(error.exception))
        for payload in (
            self.payload | {"headers": {"Authorization": "private-token"}},
            self.payload
            | {
                "state": {
                    "summary": "unapproved prompt",
                    "requirements": self.payload["state"]["requirements"],
                }
            },
        ):
            with self.assertRaises(ValueError):
                self.journal.reserve(
                    self.task["task_id"],
                    attempt,
                    payload=payload,
                    input_sha256="a" * 64,
                    eligibility=self.eligibility,
                    baseline=self.baseline,
                )
        self.reserve(attempt)
        for changes in ({"id": "other", "model": "native/other"}, {"thinking": "high"}):
            with self.assertRaises(ValueError):
                self.journal.finish(
                    self.task["task_id"],
                    attempt,
                    {
                        "status": "completed",
                        "proposal": {
                            "id": "first",
                            "model": "native/first",
                            "thinking": "medium",
                        }
                        | changes,
                    },
                )
        with self.assertRaises(ValueError):
            self.journal.finish(
                self.task["task_id"],
                attempt,
                {"status": "unavailable", "error": "private-token"},
            )
        self.journal.finish(
            self.task["task_id"],
            attempt,
            {"status": "unavailable", "reason": "transport_error"},
        )
        self.assertEqual(self.journal.view(self.task)["status"], "unavailable")
