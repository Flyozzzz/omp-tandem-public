"""Team selection against real localhost Choice HTTP and metadata-only RPC peers."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from omp_rpc import RpcClient

from omp_tandem.jev_client import JevConfig
from omp_tandem.team_selection import TeamSelection
from omp_tandem.workspace import resolve_scope
from tests.test_jev_audit import LocalDecisions


def plan(**changes):
    return {
        "goal": "Investigate a defect and integrate the findings.",
        "steps": [
            {
                "id": "research",
                "title": "Investigate the defect",
                "mode": "analyze",
                "contract": {
                    "goal": "Locate the defect",
                    "context": "PRIVATE_CONTRACT_CONTEXT",
                    "constraints": ["PRIVATE_CONSTRAINT"],
                },
            },
            {
                "id": "final",
                "title": "Integrate the findings",
                "mode": "think",
                "depends_on": ["research"],
                "contract": {
                    "goal": "Explain the defect",
                    "context": "PRIVATE_INTEGRATION_CONTEXT",
                },
            },
        ],
        **changes,
    }


def response(questions, *, choice=None, confidence=0.9, thinking="high"):
    answers = {}
    for key, question in questions.items():
        criteria = question["criteria"]
        if key.startswith("role_"):
            selected = "research" if key == "role_0" else "integration"
        elif choice is not None:
            selected = choice
        else:
            selected = next(
                label
                for label, encoded in criteria.items()
                if label.startswith("candidate_")
                and json.loads(encoded)["model"] == "openai-codex/alternative"
                and json.loads(encoded)["thinking"] == thinking
            )
        answers[key] = {
            "type": "choice",
            "choice": selected,
            "confidence": confidence,
            "probabilities": {
                name: 0.9 if name == selected else 0.1 / (len(criteria) - 1)
                for name in criteria
            },
        }
    return {
        "model": "typesafe/jev-1.13-local",
        "answers": answers,
        "usage": {"input_tokens": 37, "output_tokens": 9, "cost": 0.001},
    }


class TeamSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        source = Path(__file__).parent / "fixtures" / "model_routing_peer.py"
        self.peer = self.root / "native-peer"
        self.peer.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source.resolve()))} "$@"\n'
        )
        self.peer.chmod(0o700)
        self.scope = resolve_scope(self.root / "state", self.root)
        self.provider = LocalDecisions()
        self.endpoint = await self.provider.start()
        self.addAsyncCleanup(self.provider.close)
        self.selection = TeamSelection(
            self.scope, str(self.peer), endpoint=self.endpoint
        )
        self.controls()

    def enable_jev(self):
        self.selection.config = JevConfig(
            classifier_enabled=True, api_key="synthetic-localhost-key"
        )

    def controls(self, **values):
        (self.root / "routing-peer-control.json").write_text(
            json.dumps({"baseline_selector": "openai-codex/baseline", **values})
        )

    def events(self):
        path = self.root / "routing-peer-events.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    async def select(self, request=None, **kwargs):
        return await asyncio.to_thread(
            self.selection.select,
            request or plan(),
            deadline=time.monotonic() + 10,
            cancelled=kwargs.get("cancelled", lambda: False),
        )

    async def decide(self, request=None, **changes):
        self.provider.received.clear()
        self.provider.release.clear()
        pending = asyncio.create_task(self.select(request))
        try:
            await asyncio.wait_for(self.provider.received.wait(), 5)
            self.provider.body = response(
                self.provider.requests[-1]["questions"], **changes
            )
        finally:
            self.provider.release.set()
        return await pending

    async def test_choice_uses_real_catalog_and_sends_only_approved_summaries(self):
        self.enable_jev()
        selected = await self.decide()
        self.assertEqual(selected["status"], "ready")
        self.assertEqual(
            {entry["model"] for entry in selected["assignments"].values()},
            {"openai-codex/alternative"},
        )
        self.assertEqual(selected["assignments"]["research"]["role"], "research")
        self.assertEqual(selected["assignments"]["final"]["role"], "integration")
        self.assertEqual(len(self.provider.requests), 1)
        exported = json.dumps(self.provider.requests) + json.dumps(selected)
        for private in (
            "PRIVATE_CONTRACT_CONTEXT",
            "PRIVATE_INTEGRATION_CONTEXT",
            "PRIVATE_CONSTRAINT",
            "PRIVATE_BASE_URL",
            "PRIVATE_CATALOG_TOKEN",
            "baseUrl",
            "headers",
        ):
            self.assertNotIn(private, exported)
        self.assertEqual(selected["jev"]["usage"]["cost"], 0.001)
        self.assertEqual(len(selected["jev"]["input_sha256"]), 64)
        self.assertEqual(selected["catalog"]["current_model"], "openai-codex/baseline")
        self.assertTrue(any(event["kind"] == "stopped" for event in self.events()))
        self.assertFalse(
            any(
                event["kind"] in ("prompt", "set_host_tools", "set_model")
                for event in self.events()
            )
        )
        spawn = next(event for event in self.events() if event["kind"] == "spawn")
        for restricted in (
            "--no-session",
            "--no-skills",
            "--no-rules",
            "--no-extensions",
            "--no-lsp",
        ):
            self.assertIn(restricted, spawn["args"])

    async def test_explicit_step_plan_and_process_precedence_never_invokes_jev(self):
        self.enable_jev()
        self.selection.model = "openai-codex/baseline"
        request = plan(execution={"model": "openai-codex/alternative"})
        request["steps"][0]["execution"] = {"model": "openai-codex/baseline"}
        request["steps"][0]["role"] = "Exact operator role"
        selected = await self.select(request)
        self.assertEqual(selected["status"], "ready")
        self.assertEqual(
            selected["assignments"]["research"]["model"], "openai-codex/baseline"
        )
        self.assertEqual(
            selected["assignments"]["research"]["role"], "Exact operator role"
        )
        self.assertEqual(
            selected["assignments"]["final"]["model"], "openai-codex/alternative"
        )
        selected = await self.select()
        self.assertEqual(
            {value["model"] for value in selected["assignments"].values()},
            {"openai-codex/baseline"},
        )
        self.assertEqual(self.provider.requests, [])

    async def test_absent_jev_requires_complete_frozen_coordinator_agreement(self):
        self.selection.config = JevConfig(classifier_enabled=True)
        request = plan()
        selected = await self.select(request)
        self.assertEqual(selected["status"], "needs_assignment")
        self.assertEqual(selected["next_action"], "agree_models")
        self.assertEqual(selected["pending"], ["research", "final"])
        self.assertEqual(selected["assignments"], {})
        agreed = {
            key: {
                "model": "openai-codex/alternative",
                "thinking": "high",
                "reason": "OMP and coordinator agree this model suits the investigation.",
            }
            for key in selected["pending"]
        }
        with self.assertRaises(ValueError):
            self.selection.assign(request, selected, {"research": agreed["research"]})
        invalid = {
            **agreed,
            "final": {
                "model": "invented/model",
                "thinking": "high",
                "reason": "Not in the catalog",
            },
        }
        with self.assertRaises(ValueError):
            self.selection.assign(request, selected, invalid)
        before = self.events()
        self.controls(single_candidate=True)
        assigned = self.selection.assign(request, selected, agreed)
        self.assertEqual(assigned["status"], "ready")
        self.assertEqual(
            assigned["assignments"]["final"]["source"], "coordinator_agreement"
        )
        self.assertEqual(self.events(), before)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(selected["pending"], ["research", "final"])

    async def test_explicit_assignments_cannot_be_overwritten_during_agreement(self):
        request = plan()
        request["steps"][0]["execution"] = {"model": "openai-codex/baseline"}
        selected = await self.select(request)
        with self.assertRaises(ValueError):
            self.selection.assign(
                request,
                selected,
                {
                    key: {
                        "model": "openai-codex/alternative",
                        "thinking": "high",
                        "reason": "Replace all",
                    }
                    for key in ("research", "final")
                },
            )
        assigned = self.selection.assign(
            request,
            selected,
            {
                "final": {
                    "model": "openai-codex/alternative",
                    "thinking": "high",
                    "reason": "Agreed with OMP",
                }
            },
        )
        self.assertEqual(
            assigned["assignments"]["research"]["model"], "openai-codex/baseline"
        )

    async def test_coordinator_mode_exports_nothing_even_with_jev_configured(self):
        self.enable_jev()
        selected = await self.select(plan(model_selection="coordinator"))
        self.assertEqual(selected["status"], "needs_assignment")
        self.assertEqual(self.provider.requests, [])

    async def test_invalid_duplicate_and_known_unsupported_models_are_ineligible(self):
        class MixedCatalog(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    baseline, alternative = result["models"]
                    result["models"] = [
                        baseline,
                        baseline,
                        alternative,
                        {**baseline, "id": "audio-only", "input": ["audio"]},
                        {**baseline, "id": "no-reasoning", "reasoning": False},
                        {
                            **baseline,
                            "id": "low-only",
                            "thinking": {"efforts": ["low"]},
                        },
                        {**baseline, "id": "no-tools", "toolCalling": False},
                        {
                            **baseline,
                            "id": "url",
                            "provider": "https://private.invalid",
                        },
                        {**baseline, "id": "unsafe?api_key=PRIVATE_KEY"},
                    ]
                return result

        self.selection.probe_factory = MixedCatalog
        selected = await self.select(plan(execution={"thinking": "high"}))
        self.assertEqual(selected["status"], "ready")
        self.assertEqual(
            selected["eligible_models"]["research"], ["openai-codex/alternative"]
        )
        self.assertEqual(
            selected["assignments"]["research"]["source"], "only_candidate"
        )
        self.assertNotIn("PRIVATE_KEY", json.dumps(selected))
        self.assertEqual(self.provider.requests, [])

    async def test_unknown_capabilities_remain_unknown_not_verified(self):
        class UnknownCatalog(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    result["models"] = [
                        {
                            "id": "unknown",
                            "provider": "openai-codex",
                            "apiKey": "PRIVATE_KEY",
                            "name": "PRIVATE_NAME",
                        }
                    ]
                return result

        self.selection.probe_factory = UnknownCatalog
        selected = await self.select(plan(execution={"thinking": "high"}))
        self.assertEqual(
            selected["assignments"]["research"]["model"], "openai-codex/unknown"
        )
        observed = selected["catalog"]["models"][0]
        self.assertIsNone(observed["tool_calling"])
        self.assertIsNone(observed["reasoning"])
        self.assertIsNone(observed["input_modalities"])
        self.assertNotIn("PRIVATE_", json.dumps(selected))

    async def test_ineligible_explicit_model_is_never_silently_replaced(self):
        request = plan(execution={"model": "openai-codex/unavailable"})
        selected = await self.select(request)
        self.assertEqual(selected["status"], "unavailable")
        self.assertEqual(selected["assignments"], {})
        with self.assertRaises(ValueError):
            self.selection.assign(
                request,
                selected,
                {
                    step: {
                        "model": "openai-codex/baseline",
                        "thinking": "high",
                        "reason": "Try replacement",
                    }
                    for step in selected["pending"]
                },
            )

    async def test_low_confidence_none_unclear_and_backend_error_require_agreement(
        self,
    ):
        self.enable_jev()
        for choice, confidence, expected in (
            ("candidate_1", 0.4, "low_confidence"),
            ("none", 0.9, "none"),
            ("unclear", 0.9, "unclear"),
        ):
            with self.subTest(expected=expected):
                selected = await self.decide(choice=choice, confidence=confidence)
                self.assertEqual(selected["status"], "needs_assignment")
                self.assertEqual(selected["assignments"], {})
                self.assertEqual(selected["pending_reasons"]["research"], expected)
                self.assertEqual(
                    selected["jev"]["decisions"]["research"]["execution"]["choice"],
                    choice,
                )
        self.provider.status = 503
        selected = await self.select()
        self.assertEqual(selected["status"], "needs_assignment")
        self.assertEqual(selected["jev"]["reason"], "http_error")
        self.assertEqual(len(self.provider.requests), 4)

    async def test_oversized_summaries_do_not_send_or_choose_a_fallback(self):
        self.enable_jev()
        request = plan(goal="g" * 8000)
        for step in request["steps"]:
            step["title"] = "t" * 2000
            step["contract"]["goal"] = "x" * 8000
        # Expand the valid linear DAG until its public summaries exceed the Choice limit.
        for index in range(2, 5):
            request["steps"].append(
                {
                    "id": f"stage{index}",
                    "title": "t" * 2000,
                    "depends_on": [request["steps"][-1]["id"]],
                    "contract": {"goal": "x" * 8000},
                }
            )
        selected = await self.select(request)
        self.assertEqual(selected["status"], "needs_assignment")
        self.assertEqual(selected["jev"]["reason"], "input_too_large")
        self.assertEqual(selected["assignments"], {})
        self.assertEqual(self.provider.requests, [])

    async def test_oversized_catalog_stays_available_for_local_agreement(self):
        class LargeCatalog(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    baseline = result["models"][0]
                    result["models"] = [
                        {**baseline, "id": f"candidate-{index}"} for index in range(120)
                    ]
                return result

        self.enable_jev()
        self.selection.probe_factory = LargeCatalog
        request = plan()
        selected = await self.select(request)
        self.assertEqual(selected["status"], "needs_assignment")
        self.assertEqual(selected["jev"]["reason"], "input_too_large")
        self.assertEqual(self.provider.requests, [])
        assigned = self.selection.assign(
            request,
            selected,
            {
                step: {
                    "model": "openai-codex/candidate-119",
                    "thinking": "high",
                    "reason": "Agreed with OMP from the full frozen catalog",
                }
                for step in selected["pending"]
            },
        )
        self.assertEqual(assigned["status"], "ready")
        self.assertEqual(
            assigned["assignments"]["final"]["model"], "openai-codex/candidate-119"
        )

    async def test_catalog_stall_and_cancellation_teardown_without_http(self):
        self.enable_jev()
        self.controls(catalog_stall=True)
        cancelled = threading.Event()
        pending = asyncio.create_task(self.select(cancelled=cancelled.is_set))
        until = time.monotonic() + 4
        while not any(
            event["kind"] == "get_available_models" for event in self.events()
        ):
            if time.monotonic() >= until:
                self.fail("Metadata peer did not receive catalog request")
            await asyncio.sleep(0.01)
        cancelled.set()
        selected = await asyncio.wait_for(pending, 5)
        self.assertEqual(selected["status"], "unavailable")
        self.assertEqual(selected["catalog"]["reason"], "cancelled")
        self.assertTrue(selected["catalog"]["probe"]["stop_returned"])
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
        started = time.monotonic()
        selected = await self.select()
        self.assertEqual(selected["status"], "unavailable")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(selected["catalog"]["probe"]["stop_returned"])

    async def test_raised_stop_failure_quarantines_selection_without_leaking_errors(
        self,
    ):
        class FailingStop(RpcClient):
            def stop(self):
                super().stop()
                raise RuntimeError("PRIVATE_STOP_FAILURE")

        self.selection.probe_factory = FailingStop
        selected = await self.select()
        self.assertEqual(selected["catalog"]["reason"], "probe_teardown_failed")
        self.assertEqual(selected["status"], "unavailable")
        self.assertNotIn("PRIVATE_STOP_FAILURE", json.dumps(selected))
        before = self.events()
        selected = await self.select()
        self.assertEqual(selected["catalog"]["reason"], "probe_teardown_failed")
        self.assertEqual(self.events(), before)

    async def test_stalled_teardown_returns_distinct_failure_at_deadline(self):
        release = threading.Event()
        stopped = threading.Event()

        class StalledStop(RpcClient):
            def stop(self):
                super().stop()
                stopped.set()
                release.wait(10)

        self.selection.probe_factory = StalledStop
        started = time.monotonic()
        try:
            selected = await asyncio.to_thread(
                self.selection.catalog, deadline=started + 4.5
            )
            self.assertTrue(stopped.is_set())
            self.assertEqual(selected["reason"], "probe_teardown_failed")
            self.assertLess(time.monotonic() - started, 5.5)
            self.assertFalse(self.selection.status()["metadata_lifecycle_healthy"])
        finally:
            release.set()

    async def test_only_available_openai_subscription_models_reach_jev_and_agreement(
        self,
    ):
        class MixedProviders(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    baseline = result["models"][0]
                    result["models"] += [
                        {
                            **baseline,
                            "provider": "openrouter",
                            "id": "openai/private-route",
                        },
                        {**baseline, "provider": "openai", "id": "api-only"},
                        {**baseline, "provider": "zai", "id": "other-provider"},
                        {
                            **baseline,
                            "provider": "openai-codex-lookalike",
                            "id": "not-subscription",
                        },
                    ]
                return result

        self.selection.probe_factory = MixedProviders
        self.enable_jev()
        selected = await self.decide()
        self.assertEqual(
            {item["model"] for item in selected["catalog"]["models"]},
            {"openai-codex/baseline", "openai-codex/alternative"},
        )
        exported = json.dumps(self.provider.requests)
        for denied in (
            "openrouter/",
            "openai/api-only",
            "zai/",
            "openai-codex-lookalike/",
        ):
            self.assertNotIn(denied, exported)
        self.selection.config = JevConfig()
        pending = await self.select()
        with self.assertRaises(ValueError):
            self.selection.assign(
                plan(),
                pending,
                {
                    identifier: {
                        "model": "openrouter/openai/private-route",
                        "thinking": "high",
                        "reason": "Must be refused",
                    }
                    for identifier in pending["pending"]
                },
            )

    async def test_no_subscription_models_blocks_instead_of_using_an_api_provider(self):
        class ApiOnly(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    result["models"] = [
                        {**item, "provider": "openai"} for item in result["models"]
                    ]
                return result

        self.selection.probe_factory = ApiOnly
        self.enable_jev()
        selected = await self.select()
        self.assertEqual(selected["status"], "unavailable")
        self.assertEqual(
            selected["catalog"]["reason"], "no_available_subscription_models"
        )
        self.assertEqual(selected["assignments"], {})
        self.assertEqual(self.provider.requests, [])

    async def test_model_and_thinking_are_one_validated_choice(self):
        class ThinkingCatalog(RpcClient):
            def request_raw(self, command_type, **payload):
                result = super().request_raw(command_type, **payload)
                if command_type == "get_available_models":
                    result["models"][1]["thinking"]["efforts"] = ["low", "high"]
                return result

        self.selection.probe_factory = ThinkingCatalog
        self.enable_jev()
        selected = await self.decide(thinking="low")
        self.assertEqual(selected["assignments"]["research"]["thinking"], "low")
        self.assertEqual(
            selected["assignments"]["research"]["model"], "openai-codex/alternative"
        )
        self.selection.config = JevConfig()
        pending = await self.select()
        with self.assertRaises(ValueError):
            self.selection.assign(
                plan(),
                pending,
                {
                    identifier: {
                        "model": "openai-codex/baseline",
                        "thinking": "low",
                        "reason": "This combination is not supported",
                    }
                    for identifier in pending["pending"]
                },
            )
        explicit = await self.select(
            plan(execution={"model": "openai-codex/alternative", "thinking": "high"})
        )
        self.assertEqual(explicit["status"], "ready")
        self.assertEqual(
            {item["thinking"] for item in explicit["assignments"].values()}, {"high"}
        )
