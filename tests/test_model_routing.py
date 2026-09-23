"""Shadow routing through the actual native worker, subprocess RPC, and localhost HTTP."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from omp_rpc import RpcClient

from omp_tandem.bridge import Bridge
from omp_tandem.jev_client import JevConfig
from omp_tandem.model_routing import ModelRouter
from omp_tandem.model_routing_state import RoutingPolicy
from tests.test_jev_audit import LocalDecisions


def policy(**changes):
    return RoutingPolicy.model_validate(
        {
            "allow_external_summary": True,
            "budget_seconds": 10.0,
            "routes": [
                {
                    "id": name,
                    "model": f"synthetic/{name}",
                    "description": f"Local {name}",
                    "tool_calling": True,
                }
                for name in ("baseline", "alternative")
            ],
            **changes,
        }
    )


def routing_input(**changes):
    return {
        "summary": "Compare models for an approved short explanation.",
        "allow_external_summary": True,
        "input_token_estimate": 1000,
        "output_token_estimate": 200,
        **changes,
    }


def decision(choice="route_1"):
    return {
        "model": "typesafe/jev-1.13-local",
        "provider": "TypeSafe",
        "id": "local-route",
        "answers": {
            "routing": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": {
                    key: 0.85 if key == choice else 0.05
                    for key in ("route_0", "route_1", "none", "unclear")
                },
            }
        },
        "usage": {"input_tokens": 45, "output_tokens": 5, "cost": 0.00001},
    }


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        source = Path(__file__).parent / "fixtures" / "model_routing_peer.py"
        self.peer = self.root / "native-peer"
        self.peer.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source.resolve()))} "$@"\n'
        )
        self.peer.chmod(0o700)
        self.provider = LocalDecisions()
        self.provider.body = decision()
        self.endpoint = await self.provider.start()
        self.addAsyncCleanup(self.provider.close)
        self.config = JevConfig(api_key="synthetic-localhost-key")
        self.bridge = Bridge(
            self.root / "state",
            str(self.peer),
            None,
            project_root=self.root,
            webhook_enabled=False,
            routing_policy=policy(),
            jev_config=self.config,
        )
        self.bridge.model_routing.endpoint = self.endpoint
        self.addAsyncCleanup(self.bridge.jev_recommend.close)
        self.addAsyncCleanup(self.bridge.jev.close)
        self.addAsyncCleanup(asyncio.to_thread, self.bridge.shutdown)

    def controls(self, **values):
        (self.root / "routing-peer-control.json").write_text(json.dumps(values))

    def events(self):
        path = self.root / "routing-peer-events.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def rows(self):
        with closing(self.bridge.tasks.connect()) as db:
            return {
                row["task_id"]: json.loads(row["record_json"])
                for row in db.execute("SELECT * FROM task_model_routing")
            }

    async def start(self, *, execution=None, **changes):
        arguments = {
            "prompt": "PRIVATE_FULL_PROMPT must remain only with execution",
            "cwd": str(self.root),
            "mode": "think",
            "timeout_seconds": 15,
            "execution": {"routing": routing_input()},
            **changes,
        }
        if execution is not None:
            arguments["execution"] = execution
        return await asyncio.to_thread(self.bridge.start, **arguments)

    async def completed(self, task_id):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            result = self.bridge.view(task_id, details=True, refresh=False)
            if result["status"] not in (
                "starting",
                "running",
                "waiting_input",
                "cancelling",
            ):
                return result
            await asyncio.sleep(0.02)
        self.fail("Synthetic task failed to settle within its local lifecycle bound")

    async def wait_event(self, role, kind):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            if any(
                item["role"] == role and item["kind"] == kind for item in self.events()
            ):
                return
            await asyncio.sleep(0.01)
        self.fail(f"Missing local peer event {role}/{kind}")

    async def replay(self, task_id, router=None):
        await asyncio.to_thread(
            (router or self.bridge.model_routing).observe,
            task_id,
            baseline={
                "model": "synthetic/baseline",
                "thinking": "high",
                "source": "native_get_state",
            },
            executable=str(self.peer),
            cwd=str(self.root),
            worker_config="unused",
            thinking="high",
            deadline=time.monotonic() + 15,
        )

    async def test_proposal_differs_but_real_execution_and_private_prompt_are_unchanged(
        self,
    ):
        started = await self.start()
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "completed", result)
        observed = result["execution"]["routing"]
        self.assertEqual(
            observed["proposal"],
            {"id": "alternative", "model": "synthetic/alternative", "thinking": "high"},
        )
        self.assertFalse(observed["applied"])
        self.assertEqual(observed["baseline_startup"]["model"], "synthetic/baseline")
        self.assertEqual(observed["jev"]["usage"]["input_tokens"], 45)
        task = self.bridge.tasks.get(started["task_id"], refresh=False)
        self.assertEqual(task["actual_model"], "synthetic/baseline")
        self.assertEqual(task["actual_thinking"], "high")
        events = self.events()
        prompt_index = next(
            index for index, event in enumerate(events) if event["kind"] == "prompt"
        )
        stopped_index = next(
            index
            for index, event in enumerate(events)
            if event["role"] == "probe" and event["kind"] == "stopped"
        )
        self.assertLess(stopped_index, prompt_index)
        self.assertIn("PRIVATE_FULL_PROMPT", events[prompt_index]["message"])
        self.assertEqual(
            [
                event["role"]
                for event in events
                if event["kind"] == "get_available_models"
            ],
            ["probe"],
        )
        self.assertEqual(
            [event["role"] for event in events if event["kind"] == "prompt"],
            ["execution"],
        )
        body = json.dumps(self.provider.requests)
        safe_view = json.dumps(observed)
        for forbidden in (
            "PRIVATE_FULL_PROMPT",
            "PRIVATE_BASE_URL",
            "PRIVATE_CATALOG_TOKEN",
            str(self.root),
            started["task_id"],
            started["conversation_id"],
        ):
            self.assertNotIn(forbidden, body)
        for forbidden in (
            "PRIVATE_BASE_URL",
            "PRIVATE_CATALOG_TOKEN",
            "synthetic-localhost-key",
        ):
            self.assertNotIn(forbidden, safe_view)
        self.assertNotIn("summary", observed)
        self.assertEqual(
            self.provider.requests[0]["state"]["summary"], routing_input()["summary"]
        )
        record = self.rows()[started["task_id"]]
        self.assertEqual(record["state"], "terminal")
        self.assertTrue(record["probe"]["stop_returned"])
        self.assertNotIn("confirmed_process_exit", record["probe"])
        previous_events = self.events()
        restarted = ModelRouter(
            self.bridge.tasks, None, self.config, endpoint=self.endpoint
        )
        await self.replay(started["task_id"], restarted)
        self.assertEqual(self.events(), previous_events)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(
            restarted.view(task, details=True)["proposal"], observed["proposal"]
        )

    async def test_http_starts_only_after_probe_stop_and_reservation(self):
        self.provider.release.clear()
        started = await self.start()
        await asyncio.wait_for(self.provider.received.wait(), 4)
        record = self.rows()[started["task_id"]]
        self.assertEqual(record["state"], "reserved")
        self.assertTrue(record["request_sha256"])
        self.assertTrue(
            any(
                event["role"] == "probe" and event["kind"] == "stopped"
                for event in self.events()
            )
        )
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
        # In-flight policy replacement cannot rewrite the admitted task's choice mapping.
        self.bridge.model_routing.policy = policy(
            routes=[
                {
                    "id": name,
                    "model": f"different/{name}",
                    "description": name,
                    "tool_calling": True,
                }
                for name in ("changed", "other")
            ]
        )
        self.provider.release.set()
        result = await self.completed(started["task_id"])
        self.assertEqual(
            result["execution"]["routing"]["proposal"]["model"], "synthetic/alternative"
        )

    async def test_probe_readiness_negotiation_and_catalog_stalls_do_not_poison_execution(
        self,
    ):
        for stage in ("startup_stall", "negotiation_stall", "catalog_stall"):
            with self.subTest(stage=stage):
                self.controls(**{stage: True})
                offset = len(self.events())
                started_at = time.monotonic()
                started = await self.start()
                result = await self.completed(started["task_id"])
                self.assertEqual(result["status"], "completed", result)
                self.assertLess(time.monotonic() - started_at, 7)
                self.assertEqual(
                    result["execution"]["routing"]["status"], "unavailable"
                )
                events = self.events()[offset:]
                if stage == "negotiation_stall":
                    self.assertTrue(
                        any(event["kind"] == "negotiate_protocol" for event in events)
                    )
                stop = next(
                    index
                    for index, event in enumerate(events)
                    if event["role"] == "probe" and event["kind"] == "stopped"
                )
                prompt = next(
                    index
                    for index, event in enumerate(events)
                    if event["kind"] == "prompt"
                )
                self.assertLess(stop, prompt)
        self.assertEqual(self.provider.requests, [])

    async def test_cancel_during_probe_sends_neither_http_nor_execution_prompt(self):
        self.controls(catalog_stall=True)
        started = await self.start()
        await self.wait_event("probe", "get_available_models")
        self.bridge.tasks.cancel(started["task_id"])
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
        self.assertTrue(self.rows()[started["task_id"]]["probe"]["stop_returned"])

    async def test_cancel_during_http_retains_unresolved_reservation_and_never_replays(
        self,
    ):
        self.provider.release.clear()
        started = await self.start()
        await asyncio.wait_for(self.provider.received.wait(), 4)
        task_id = started["task_id"]
        self.bridge.tasks.cancel(task_id)
        result = await self.completed(task_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["execution"]["routing"]["status"], "unresolved")
        self.assertEqual(
            result["execution"]["routing"]["send_state"], "may_have_been_sent"
        )
        self.assertEqual(self.rows()[task_id]["state"], "reserved")
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
        restarted = ModelRouter(
            self.bridge.tasks, policy(), self.config, endpoint=self.endpoint
        )
        events = self.events()
        await self.replay(task_id, restarted)
        self.assertEqual(self.events(), events)
        self.assertEqual(len(self.provider.requests), 1)
        self.provider.release.set()

    async def test_local_http_timeout_falls_back_without_poisoning_baseline(self):
        self.provider.release.clear()
        self.bridge.model_routing.config = JevConfig(
            api_key="synthetic-localhost-key", timeout_seconds=0.15
        )
        started = await self.start()
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["execution"]["routing"]["reason"], "timeout")
        self.assertIsNone(result["execution"]["routing"]["proposal"])
        self.assertEqual(len(self.provider.requests), 1)
        self.provider.release.set()

    async def test_missing_key_baseline_and_probe_budget_bypass_without_probe_or_http(
        self,
    ):
        for reason in (
            "missing_api_key",
            "missing_baseline",
            "insufficient_probe_budget",
        ):
            with self.subTest(reason=reason):
                self.controls(missing_baseline=reason == "missing_baseline")
                self.bridge.model_routing.config = JevConfig(
                    api_key=None
                    if reason == "missing_api_key"
                    else "synthetic-localhost-key"
                )
                self.bridge.model_routing.policy = policy(
                    budget_seconds=1.0
                    if reason == "insufficient_probe_budget"
                    else 10.0
                )
                started = await self.start()
                result = await self.completed(started["task_id"])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["execution"]["routing"]["reason"], reason)
        self.assertFalse(any(event["role"] == "probe" for event in self.events()))
        self.assertEqual(self.provider.requests, [])

    async def test_admission_bypasses_and_preflight_have_no_external_side_effects(self):
        await self.start(dry_run=True)
        self.assertEqual(self.rows(), {})
        self.assertEqual(self.events(), [])
        for execution, reason in (
            (
                {"model": "synthetic/baseline", "routing": routing_input()},
                "explicit_model",
            ),
            (
                {"routing": routing_input(allow_external_summary=False)},
                "request_export_not_allowed",
            ),
            ({}, "missing_input"),
        ):
            with self.subTest(reason=reason):
                started = await self.start(execution=execution)
                result = await self.completed(started["task_id"])
                self.assertEqual(result["status"], "completed", result)
                self.assertEqual(result["execution"]["routing"]["reason"], reason)
        self.assertFalse(any(event["role"] == "probe" for event in self.events()))
        self.assertEqual(self.provider.requests, [])

    async def test_none_unclear_and_invalid_decisions_remain_distinct(self):
        for choice in ("none", "unclear", "invalid"):
            with self.subTest(choice=choice):
                self.provider.body = decision(choice)
                started = await self.start()
                result = await self.completed(started["task_id"])
                routing = result["execution"]["routing"]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    routing["status"], "unavailable" if choice == "invalid" else choice
                )
                self.assertIsNone(routing["proposal"])

    async def test_single_eligible_candidate_is_not_a_jev_comparison(self):
        self.controls(single_candidate=True)
        started = await self.start()
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["execution"]["routing"]["status"], "no_comparison")
        self.assertEqual(self.provider.requests, [])

    async def test_opaque_native_identity_does_not_break_shadow_bypass(self):
        baseline = "custom/model@revision-" + "x" * 220
        self.controls(baseline_selector=baseline)
        self.bridge.model_routing.config = JevConfig()
        started = await self.start()
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["execution"]["actual"]["model"], baseline)
        routing = result["execution"]["routing"]
        self.assertEqual(routing["status"], "bypassed")
        self.assertEqual(routing["reason"], "missing_api_key")
        self.assertEqual(routing["baseline_startup"]["model"], baseline)
        self.assertFalse(any(event["role"] == "probe" for event in self.events()))
        self.assertEqual(self.provider.requests, [])

    async def test_raised_probe_teardown_is_host_failure_not_healthy_fallback(self):
        class FailingStop(RpcClient):
            def stop(self):
                super().stop()
                raise TypeError("PRIVATE_TEARDOWN_DIAGNOSTIC")

        self.bridge.model_routing.probe_factory = FailingStop
        started = await self.start()
        result = await self.completed(started["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["execution"]["routing"]["status"], "host_lifecycle_failure"
        )
        self.assertNotIn("PRIVATE_TEARDOWN_DIAGNOSTIC", json.dumps(result))
        self.assertEqual(self.provider.requests, [])
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
