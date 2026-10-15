"""Team development through MCP, native RPC, and observable workspace results."""

import asyncio
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.jev_client import JevConfig
from omp_tandem.team_runs import ACTIVE_TEAM
from tests.test_jev_audit import LocalDecisions
from tests.test_team_selection import response


def development_plan(**changes):
    return {
        "goal": "Prepare two independent outputs and integrate their actual contents",
        "allow_work": True,
        "max_parallel": 2,
        "budget_seconds": 60,
        "steps": [
            {
                "id": name,
                "title": f"Write {name}",
                "mode": "work",
                "contract": {
                    "goal": f"write {name}",
                    "scope": {"owned_files": [f"{name}.txt"]},
                    "requirements": {"requires_write": True},
                    "acceptance": [f"{name}.txt contains {name}"],
                },
            }
            for name in ("alpha", "beta")
        ]
        + [
            {
                "id": "integrate",
                "title": "Integrate and verify both files",
                "mode": "analyze",
                "depends_on": ["alpha", "beta"],
                "contract": {
                    "goal": "integrate files",
                    "acceptance": [
                        "Read both files and return their combined contents"
                    ],
                },
            }
        ],
        **changes,
    }


class TeamApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = Path(__file__).parent / "fixtures" / "team_peer.py"
        peer = self.root / "peer"
        peer.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(source.resolve()))} "$@"\n'
        )
        peer.chmod(0o700)
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "local-only-key",
                "OMP_TANDEM_JEV": "off",
                "OMP_TANDEM_JEV_AUDIT": "1",
                "OMP_TANDEM_JEV_RECOMMEND": "1",
            },
        ):
            self.bridge = Bridge(
                self.root / "state",
                str(peer),
                None,
                project_root=self.root,
                channel_enabled=False,
                webhook_enabled=False,
            )
        self.client = Client(build_server(self.bridge))
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)

    async def call(self, tool="tandem_team", **arguments):
        response = await self.client.call_tool(tool, arguments)
        if tool == "tandem_team":
            for content in response.content:
                if content.type == "text":
                    self.assertLessEqual(len(content.text.encode("utf-8")), 16384)
                    json.loads(content.text)
        return to_jsonable_python(response.data)

    async def read_section(self, request):
        chunks = []
        expected = None
        offset = 0
        while request:
            page = await self.call(**request)
            self.assertLessEqual(len(json.dumps(page).encode("utf-8")), 16384)
            self.assertEqual(page["offset_bytes"], offset)
            content = page["content"].encode("utf-8")
            self.assertEqual(page["returned_bytes"], len(content))
            expected = expected or page["sha256"]
            self.assertEqual(page["sha256"], expected)
            chunks.append(content)
            offset += len(content)
            request = page["next_request"]
        data = b"".join(chunks)
        self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        self.assertEqual(len(data), page["total_bytes"])
        return json.loads(data)

    async def settled(self, run_id, *, waiting=False):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = await self.call(
                action="status", run_id=run_id, wait_seconds=1, details=True
            )
            if result["status"] not in ACTIVE_TEAM or (
                waiting and result["status"] == "waiting_models"
            ):
                return result
        self.fail("Local team did not reach its expected state")

    def events(self):
        path = self.root / "team-peer-events.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    async def test_without_jev_peer_consultation_and_agreement_execute_development(
        self,
    ):
        started = await self.call(
            action="start", request_key="development", plan=development_plan()
        )
        waiting = await self.settled(started["run_id"], waiting=True)
        self.assertEqual(waiting["status"], "waiting_models")
        self.assertFalse((self.root / "alpha.txt").exists())
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))
        selection = await self.read_section(waiting["readers"]["selection"])
        self.assertEqual(selection["jev"]["reason"], "classifier_disabled")
        consult = await self.call(
            "tandem_start",
            mode="think",
            cwd=str(self.root),
            timeout_seconds=10,
            execution={"model": "openai-codex/baseline"},
            prompt="Independently recommend models for this team: "
            + json.dumps(selection["catalog"]),
        )
        advice = await self.call(
            "tandem_result", task_id=consult["task_id"], wait_seconds=10
        )
        self.assertEqual(advice["outcome"], "success")
        choices = {
            "alpha": "openai-codex/baseline",
            "beta": "openai-codex/alternative",
            "integrate": "openai-codex/alternative",
        }
        result = waiting
        for _ in range(3):
            if result["status"] not in ACTIVE_TEAM:
                break
            self.assertEqual(result["next_action"], "agree_models")
            await self.call(
                action="assign",
                run_id=started["run_id"],
                assignments={
                    identifier: {
                        "model": choices[identifier],
                        "thinking": "high",
                        "reason": "Coordinator compared the peer recommendation against the available catalog.",
                    }
                    for identifier in result["pending_models"]
                },
            )
            result = await self.settled(started["run_id"], waiting=True)
        self.assertEqual(
            (result["status"], result["outcome"], result["answer"]),
            ("completed", "success", "alpha|beta"),
        )
        self.assertEqual((self.root / "alpha.txt").read_text(), "alpha")
        self.assertEqual((self.root / "beta.txt").read_text(), "beta")
        self.assertEqual(
            {
                step["id"]: step["execution"]["actual"]["model"]
                for step in result["steps"]
            },
            choices,
        )
        self.assertEqual(
            [event["goal"] for event in self.events() if event["kind"] == "prompt"][-1],
            "integrate files",
        )

    async def test_explicit_plan_model_survives_partial_step_execution_override(self):
        plan = development_plan(
            execution={
                "model": "openai-codex/baseline",
                "profile": "deep",
                "thinking": "high",
            }
        )
        plan["steps"][0]["execution"] = {"thinking": "low"}
        plan["steps"][1]["execution"] = {"model": "openai-codex/alternative"}
        started = await self.call(action="start", request_key="explicit", plan=plan)
        result = await self.settled(started["run_id"])
        self.assertEqual(
            (result["status"], result["answer"]), ("completed", "alpha|beta")
        )
        steps = {step["id"]: step for step in result["steps"]}
        self.assertEqual(
            steps["alpha"]["execution"]["actual"],
            {"model": "openai-codex/baseline", "thinking": "low"},
        )
        self.assertEqual(steps["alpha"]["execution"]["effective"]["profile"], "deep")
        self.assertEqual(
            steps["beta"]["execution"]["actual"]["model"], "openai-codex/alternative"
        )

    async def test_native_model_substitution_is_refused_before_any_file_effect(self):
        (self.root / "team-peer-control.json").write_text(
            json.dumps({"substitute_model": "openai-codex/alternative"})
        )
        plan = development_plan(
            execution={"model": "openai-codex/baseline", "thinking": "high"}
        )
        started = await self.call(action="start", request_key="substitution", plan=plan)
        result = await self.settled(started["run_id"])
        self.assertNotEqual(result["status"], "completed")
        self.assertFalse((self.root / "alpha.txt").exists())
        self.assertFalse((self.root / "beta.txt").exists())
        self.assertFalse(any(event["kind"] == "prompt" for event in self.events()))

    async def test_key_auto_enables_jev_and_applies_selected_models_without_exporting_context(
        self,
    ):
        provider = LocalDecisions()
        self.bridge.team_selection.endpoint = await provider.start()
        self.addAsyncCleanup(provider.close)
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "local-only-key", "OMP_TANDEM_JEV": "auto"},
        ):
            self.bridge.team_selection.config = JevConfig.from_environment()
        provider.release.clear()
        plan = development_plan()
        plan["steps"][0]["contract"]["context"] = (
            "PRIVATE_SOURCE_NOT_FOR_CLASSIFICATION"
        )
        started = await self.call(
            action="start", request_key="automatic-jev", plan=plan
        )
        try:
            await asyncio.wait_for(provider.received.wait(), 5)
            provider.body = response(provider.requests[-1]["questions"], thinking="low")
        finally:
            provider.release.set()
        result = await self.settled(started["run_id"])
        self.assertEqual(
            (result["status"], result["answer"]), ("completed", "alpha|beta")
        )
        self.assertEqual(
            {step["execution"]["actual"]["model"] for step in result["steps"]},
            {"openai-codex/alternative"},
        )
        self.assertEqual(
            {step["execution"]["actual"]["thinking"] for step in result["steps"]},
            {"low"},
        )
        self.assertEqual(len(provider.requests), 1)
        self.assertNotIn(
            "PRIVATE_SOURCE_NOT_FOR_CLASSIFICATION", json.dumps(provider.requests)
        )

    async def test_openrouter_is_rejected_before_catalog_or_worker_start(self):
        with self.assertRaises(ToolError):
            await self.call(
                action="start",
                request_key="forbidden-provider",
                plan=development_plan(execution={"model": "openrouter/openai/gpt-5"}),
            )
        self.assertEqual(self.events(), [])
        self.assertFalse((self.root / "alpha.txt").exists())

    async def test_mid_turn_provider_drift_cannot_be_accepted_or_unlock_integration(
        self,
    ):
        (self.root / "team-peer-control.json").write_text(
            json.dumps({"model_after_prompt": "openrouter/forbidden-fixture"})
        )
        started = await self.call(
            action="start",
            request_key="mid-turn-drift",
            plan=development_plan(
                execution={"model": "openai-codex/baseline", "thinking": "high"}
            ),
        )
        result = await self.settled(started["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["steps"][-1]["task_id"])
        self.assertTrue(
            any(
                (step.get("execution") or {}).get("actual", {}).get("model")
                == "openrouter/forbidden-fixture"
                for step in result["steps"]
            )
        )

    async def test_catalog_cursor_pages_recover_models_without_another_probe(self):
        catalog = await self.read_section({"action": "models", "max_bytes": 64})
        self.assertEqual(
            {entry["model"] for entry in catalog["models"]},
            {"openai-codex/baseline", "openai-codex/alternative"},
        )
        self.assertEqual(
            [event["kind"] for event in self.events()],
            ["get_available_models"],
        )

    async def test_status_wait_reason_follows_final_question_and_preserves_model_action(
        self,
    ):
        run_id, task_id, question_id = (str(uuid4()) for _ in range(3))
        early = {
            "status": "running",
            "pending_models": ["final"],
            "questions": [],
            "next_action": "agree_models",
        }
        final = {
            "run_id": run_id,
            "status": "running",
            "pending_models": ["final"],
            "integration_step": "final",
            "next_action": "reply",
            "actions": [
                {
                    "action": "reply",
                    "task_id": task_id,
                    "question_id": question_id,
                    "deadline": time.time() + 60,
                }
            ],
            "steps": [
                {
                    "id": "source",
                    "task_id": task_id,
                    "status": "waiting_input",
                    "question": {"question_id": question_id},
                }
            ],
        }
        with (
            patch.object(self.bridge.team_runs, "observation", return_value=early),
            patch.object(self.bridge.team_runs, "view", return_value=final),
        ):
            result = await asyncio.wait_for(
                self.call(action="status", run_id=run_id, wait_seconds=25), 5
            )
        self.assertEqual(result["wait"]["reason"], "question")
        self.assertEqual(result["next_action"], "reply")
        self.assertEqual(
            [action["action"] for action in result["actions"]], ["reply", "assign"]
        )
        self.assertEqual(result["pending_models"], ["final"])
        self.assertNotIn("pending", result)
