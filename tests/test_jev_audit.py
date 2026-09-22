"""Exercise opt-in evidence auditing through local RPC and real localhost HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from fastmcp import Client

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.jev_audit import JevAudit, JevConfig
from omp_tandem.models import acceptance_revision
from omp_tandem.task_interaction import CHECK_RUN_ARTIFACT
from tests.helpers import RpcHarness, make_peer


def decision_response(keys):
    return {
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "id": "gen-dec-local-test",
        "answers": {
            key: {
                "type": "choice",
                "choice": "partial_or_missing",
                "confidence": 0.75,
                "probabilities": {
                    "no_obvious_mismatch": 0.1,
                    "partial_or_missing": 0.8,
                    "contradiction": 0.05,
                    "unclear": 0.05,
                },
            }
            for key in keys
        },
        "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.000019992},
    }


class LocalDecisions:
    """A real HTTP endpoint; a gate makes in-flight/cancellation races deterministic."""

    def __init__(self):
        self.requests = []
        self.raw_requests = []
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.status = 200
        self.body = None
        self.handlers = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/api/alpha/decisions"

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            length = next(
                int(line.partition(":")[2])
                for line in headers.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            raw_request = await reader.readexactly(length)
            self.raw_requests.append(raw_request)
            request = json.loads(raw_request)
            self.requests.append(request)
            self.received.set()
            await self.release.wait()
            body = self.body
            if body is None:
                body = decision_response(request["questions"])
            if not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
            writer.write(
                f"HTTP/1.1 {self.status} Local\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.handlers:
            await asyncio.gather(*tuple(self.handlers))


class JevAuditTests(RpcHarness):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.peer = make_peer(self.root)
        self.provider = LocalDecisions()
        self.endpoint = await self.provider.start()
        self.addAsyncCleanup(self.provider.close)
        self.config = JevConfig(enabled=True, api_key="localhost-test-key")
        self.bridge = Bridge(
            self.root / "state",
            str(self.peer),
            "unused",
            project_root=self.root,
            jev_config=self.config,
        )
        await self.bridge.jev.close()
        self.bridge.jev = self.service()
        self.client = Client(build_server(self.bridge))
        await self.client.__aenter__()

    def service(self, *, bridge=None, config=None):
        bridge = bridge or self.bridge
        service = JevAudit(
            bridge.tasks,
            bridge.artifacts,
            bridge.results,
            config if config is not None else self.config,
            endpoint=self.endpoint,
        )
        self.addAsyncCleanup(service.close)
        return service

    def another_bridge(self, project_root=None):
        bridge = Bridge(
            self.root / "state",
            str(self.peer),
            "unused",
            project_root=project_root or self.root,
            webhook_enabled=False,
            jev_config=self.config,
        )
        self.addAsyncCleanup(asyncio.to_thread, bridge.shutdown)
        self.addAsyncCleanup(bridge.jev.close)
        return bridge

    async def completed(
        self, goal="paragraph", acceptance=None, *, mode="think", **contract
    ):
        started = await self.call(
            "tandem_start",
            cwd=str(self.root),
            mode=mode,
            timeout_seconds=10,
            contract={
                "goal": goal,
                "acceptance": acceptance
                or ["The report demonstrates the requested behavior"],
                **contract,
            },
        )
        result = await self.result(started["task_id"], details=True)
        self.assertEqual(result["status"], "completed")
        return started, result

    async def audit(self, task_id, **page):
        return await self.call("tandem_audit", task_id=task_id, **page)

    async def test_plain_acceptance_audit_is_advisory_and_excludes_private_sources(
        self,
    ):
        started, before = await self.completed(
            "contract-error-then-partial",
            mode="work",
            acceptance=["The full suite passes", "The Linux suite passes"],
            context="PRIVATE_CONTEXT_DO_NOT_TRANSMIT",
            verification={
                "checks": [
                    {
                        "id": "pytest",
                        "criterion": "The full suite passes",
                        "command": "PRIVATE_PLANNED_COMMAND_DO_NOT_TRANSMIT",
                    }
                ]
            },
        )
        await self.client.call_tool(
            "tandem_publish_artifact",
            {
                "conversation_id": started["conversation_id"],
                "name": "private-output",
                "content": "PRIVATE_ARTIFACT_BODY_DO_NOT_TRANSMIT",
            },
        )
        task_id = started["task_id"]
        audit = await self.audit(task_id)
        self.assertEqual(audit["status"], "completed")
        self.assertTrue(audit["advisory"])
        self.assertEqual(audit["basis"], "reported_evidence_claims_not_test_inspection")
        self.assertEqual(
            [item["choice"] for item in audit["items"]],
            ["partial_or_missing", "partial_or_missing"],
        )
        after = await self.result(task_id, details=True)
        for field in (
            "status",
            "outcome",
            "answer",
            "report",
            "facts",
            "acceptance_coverage",
            "check_runs",
        ):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])
        self.assertEqual(len(self.provider.requests), 1)
        transmitted = json.dumps(self.provider.requests[0], ensure_ascii=False)
        self.assertIn(before["answer"], transmitted)
        self.assertIn("participant_reported", transmitted)
        self.assertIn("linux suite", transmitted)
        for excluded in (
            "PRIVATE_CONTEXT_DO_NOT_TRANSMIT",
            "PRIVATE_PLANNED_COMMAND_DO_NOT_TRANSMIT",
            "PRIVATE_ARTIFACT_BODY_DO_NOT_TRANSMIT",
            "pytest -q",
        ):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, transmitted)

    async def test_keyless_disabled_preview_is_effect_free_and_send_matches_exact_bytes(
        self,
    ):
        started, _ = await self.completed(
            acceptance=["First clause — полный", "Second clause", "Third clause"],
            context="PRIVATE_CONTEXT_NOT_PREVIEWED",
        )
        task_id = started["task_id"]
        keyless = self.service(config=JevConfig())
        task_before = self.bridge.tasks.get(task_id, refresh=False)
        artifacts_before = self.bridge.artifacts.for_task(task_id)
        status_before = keyless.status()
        preview = await keyless.audit(task_id, offset=0, limit=2, preview=True)
        self.assertEqual(preview["status"], "preview")
        self.assertFalse(preview["sent"])
        self.assertEqual(preview["next_offset"], 2)
        self.assertEqual(preview["endpoint"], self.endpoint)
        self.assertEqual(preview["model"]["requested"], preview["payload"]["model"])
        payload_bytes = json.dumps(
            preview["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(preview["request_bytes"], len(payload_bytes))
        self.assertEqual(
            preview["input_sha256"],
            hashlib.sha256(
                self.endpoint.encode("utf-8") + b"\0" + payload_bytes
            ).hexdigest(),
        )
        self.assertNotIn(b"PRIVATE_CONTEXT_NOT_PREVIEWED", payload_bytes)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.bridge.artifacts.for_task(task_id), artifacts_before)
        self.assertEqual(self.bridge.tasks.get(task_id, refresh=False), task_before)
        self.assertEqual(keyless.status(), status_before)
        for service, reason in (
            (keyless, "not_enabled"),
            (self.service(config=JevConfig(enabled=True)), "missing_api_key"),
        ):
            refused = await service.audit(
                task_id,
                offset=0,
                limit=2,
                expected_input_sha256=preview["input_sha256"],
            )
            self.assertEqual(refused["reason"], reason)
            self.assertFalse(refused["sent"])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.bridge.artifacts.for_task(task_id), artifacts_before)
        sent = await self.audit(
            task_id,
            offset=0,
            limit=2,
            expected_input_sha256=preview["input_sha256"],
        )
        self.assertEqual(sent["status"], "completed")
        self.assertEqual(sent["input_sha256"], preview["input_sha256"])
        self.assertEqual(self.provider.requests, [preview["payload"]])
        self.assertEqual(self.provider.raw_requests, [payload_bytes])
        repeated = await self.audit(
            task_id,
            offset=0,
            limit=2,
            expected_input_sha256=preview["input_sha256"],
        )
        self.assertTrue(repeated["cached"])
        self.assertEqual(repeated["artifact_id"], sent["artifact_id"])
        self.assertEqual(self.provider.raw_requests, [payload_bytes])

    async def test_changed_report_refuses_preview_hash_without_reserving_new_input(
        self,
    ):
        started, _ = await self.completed()
        task_id = started["task_id"]
        preview = await self.audit(task_id, preview=True)
        task = self.bridge.tasks.get(task_id, refresh=False)
        report = json.loads(task["report_json"])
        report["answer"] += "\nNew evidence arrived after preview."
        self.bridge.tasks.update(task_id, report_json=json.dumps(report))
        task_before = self.bridge.tasks.get(task_id, refresh=False)
        artifacts_before = self.bridge.artifacts.for_task(task_id)
        refused = await self.audit(
            task_id, expected_input_sha256=preview["input_sha256"]
        )
        self.assertEqual(refused["status"], "not_assessable")
        self.assertEqual(refused["reason"], "input_sha256_mismatch")
        self.assertFalse(refused["sent"])
        self.assertNotEqual(refused["input_sha256"], preview["input_sha256"])
        self.assertNotIn("payload", refused)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.bridge.artifacts.for_task(task_id), artifacts_before)
        self.assertEqual(self.bridge.tasks.get(task_id, refresh=False), task_before)
        current = await self.audit(task_id, preview=True)
        self.assertEqual(current["input_sha256"], refused["input_sha256"])
        sent = await self.audit(task_id, expected_input_sha256=current["input_sha256"])
        self.assertEqual(sent["status"], "completed")
        self.assertFalse(sent["cached"])
        self.assertEqual(self.provider.requests, [current["payload"]])

    async def test_preview_after_provider_failure_does_not_resend_or_replace_cache(
        self,
    ):
        started, _ = await self.completed()
        task_id = started["task_id"]
        self.provider.status = 503
        preview = await self.audit(task_id, preview=True)
        failed = await self.audit(
            task_id, expected_input_sha256=preview["input_sha256"]
        )
        self.assertEqual(failed["reason"], "http_error")
        artifacts_before = self.bridge.artifacts.for_task(task_id)
        self.provider.status = 200
        again = await self.audit(task_id, preview=True)
        self.assertEqual(again, preview)
        self.assertEqual(self.bridge.artifacts.for_task(task_id), artifacts_before)
        cached = await self.audit(task_id, expected_input_sha256=again["input_sha256"])
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["artifact_id"], failed["artifact_id"])
        self.assertEqual(cached["reason"], "http_error")
        self.assertEqual(self.provider.requests, [preview["payload"]])

    async def test_preview_preserves_scope_report_and_service_boundaries(self):
        started, _ = await self.completed()
        task_id = started["task_id"]
        foreign_root = self.root / "preview-other-project"
        foreign_root.mkdir()
        foreign = self.service(
            bridge=self.another_bridge(foreign_root), config=JevConfig()
        )
        with self.assertRaises(ValueError):
            await foreign.audit(task_id, preview=True)
        closed = self.service(config=JevConfig())
        await closed.close()
        result = await closed.audit(task_id, preview=True)
        self.assertEqual(result["reason"], "service_closed")
        self.assertFalse(result["sent"])
        keyless = self.service(config=JevConfig())
        active = await self.start("hold")
        try:
            result = await keyless.audit(active["task_id"], preview=True)
            self.assertEqual(result["reason"], "task_not_completed")
            self.assertFalse(result["sent"])
            self.assertNotIn("payload", result)
        finally:
            await self.call("tandem_cancel", task_id=active["task_id"])
        self.bridge.tasks.update(task_id, report_json=None)
        artifacts_before = self.bridge.artifacts.for_task(task_id)
        result = await keyless.audit(task_id, preview=True)
        self.assertEqual(result["reason"], "structured_report_required")
        self.assertFalse(result["sent"])
        self.assertNotIn("payload", result)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.bridge.artifacts.for_task(task_id), artifacts_before)

    async def test_exact_page_cache_survives_new_bridge_and_overlapping_page_is_distinct(
        self,
    ):
        started, _ = await self.completed(
            acceptance=["First clause", "Second clause", "Third clause"]
        )
        task_id = started["task_id"]
        first = await self.audit(task_id, offset=0, limit=2)
        repeated = await self.audit(task_id, offset=0, limit=2)
        self.assertTrue(repeated["cached"])
        await self.bridge.jev.close()
        restarted = self.service(bridge=self.another_bridge())
        restored = await restarted.audit(task_id, offset=0, limit=2)
        self.assertTrue(restored["cached"])
        self.assertEqual(restored["items"], first["items"])
        self.assertEqual(restored["artifact_id"], first["artifact_id"])
        self.assertEqual(len(self.provider.requests), 1)
        other_page = await restarted.audit(task_id, offset=1, limit=2)
        self.assertEqual(other_page["status"], "completed")
        self.assertEqual([item["index"] for item in other_page["items"]], [1, 2])
        self.assertNotEqual(other_page["input_sha256"], first["input_sha256"])
        self.assertEqual(len(self.provider.requests), 2)

    async def test_separate_services_cannot_bill_the_same_inflight_page(self):
        started, _ = await self.completed()
        other = self.service(bridge=self.another_bridge())
        self.provider.release.clear()
        first = asyncio.create_task(self.bridge.jev.audit(started["task_id"]))
        try:
            await asyncio.wait_for(self.provider.received.wait(), timeout=3)
            competing = await other.audit(started["task_id"])
            self.assertEqual(competing["status"], "unavailable")
            self.assertEqual(competing["reason"], "prior_attempt_unresolved")
            self.assertEqual(competing["items"], [])
        finally:
            self.provider.release.set()
            completed = await asyncio.wait_for(first, timeout=3)
        self.assertEqual(completed["status"], "completed")
        cached = await other.audit(started["task_id"])
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["items"], completed["items"])
        self.assertEqual(len(self.provider.requests), 1)

    async def test_cancelled_provider_attempt_is_not_automatically_retried(self):
        started, _ = await self.completed()
        self.provider.release.clear()
        pending = asyncio.create_task(self.bridge.jev.audit(started["task_id"]))
        try:
            await asyncio.wait_for(self.provider.received.wait(), timeout=3)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            restarted = self.service(bridge=self.another_bridge())
            artifacts_before = self.bridge.artifacts.for_task(started["task_id"])
            preview = await restarted.audit(started["task_id"], preview=True)
            self.assertEqual(preview["status"], "preview")
            self.assertFalse(preview["sent"])
            self.assertEqual(preview["payload"], self.provider.requests[0])
            self.assertEqual(
                self.bridge.artifacts.for_task(started["task_id"]), artifacts_before
            )
            result = await restarted.audit(
                started["task_id"], expected_input_sha256=preview["input_sha256"]
            )
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["reason"], "prior_attempt_unresolved")
            self.assertEqual(result["items"], [])
            self.assertEqual(len(self.provider.requests), 1)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            self.provider.release.set()

    async def assert_provider_failure_is_cached(self, acceptance=None):
        started, _ = await self.completed(acceptance=acceptance)
        first = await self.audit(started["task_id"])
        self.assertEqual(first["status"], "unavailable")
        self.assertEqual(first["items"], [])
        self.provider.status = 200
        self.provider.body = None
        restarted = self.service(bridge=self.another_bridge())
        repeated = await restarted.audit(started["task_id"])
        self.assertEqual(repeated["status"], "unavailable")
        self.assertEqual(repeated["items"], [])
        self.assertTrue(repeated["cached"])
        self.assertEqual(len(self.provider.requests), 1)

    async def test_http_failure_is_not_rebilled_even_after_provider_recovers(self):
        self.provider.status = 503
        self.provider.body = {"error": "temporarily unavailable"}
        await self.assert_provider_failure_is_cached()

    async def test_invalid_choice_rejects_entire_page_including_other_valid_answers(
        self,
    ):
        self.provider.body = decision_response(["unit_0", "unit_1"])
        self.provider.body["answers"]["unit_1"]["choice"] = "approved"
        await self.assert_provider_failure_is_cached(
            acceptance=["First acceptance clause", "Second acceptance clause"]
        )

    async def test_nonfinite_probability_cannot_become_a_positive_judgment(self):
        self.provider.body = decision_response(["unit_0"])
        self.provider.body["answers"]["unit_0"]["probabilities"][
            "partial_or_missing"
        ] = float("nan")
        await self.assert_provider_failure_is_cached()

    async def test_disabled_missing_credentials_foreign_and_active_tasks_never_send(
        self,
    ):
        started, _ = await self.completed()
        disabled = self.service(config=JevConfig())
        missing_key = self.service(config=JevConfig(enabled=True))
        self.assertEqual(
            (await disabled.audit(started["task_id"]))["status"], "disabled"
        )
        self.assertEqual(
            (await missing_key.audit(started["task_id"]))["status"], "unavailable"
        )
        foreign_root = self.root / "other-project"
        foreign_root.mkdir()
        foreign = self.service(bridge=self.another_bridge(foreign_root))
        with self.assertRaises(ValueError):
            await foreign.audit(started["task_id"])
        active = await self.start("hold")
        try:
            result = await self.audit(active["task_id"])
            self.assertEqual(result["status"], "not_assessable")
        finally:
            await self.call("tandem_cancel", task_id=active["task_id"])
        self.assertEqual(self.provider.requests, [])
        # Ineligible calls must not poison the reservation for a later enabled audit.
        enabled = await self.audit(started["task_id"])
        self.assertEqual(enabled["status"], "completed")
        self.assertEqual(len(self.provider.requests), 1)

    async def test_utf8_oversize_rejects_the_whole_answer_without_truncation(self):
        started, result = await self.completed("long-answer")
        self.assertLess(len(result["answer"]), 32000)
        self.assertGreater(len(result["answer"].encode("utf-8")), 32000)
        before = self.bridge.artifacts.for_task(started["task_id"])
        preview = await self.audit(started["task_id"], preview=True)
        self.assertEqual(preview["status"], "not_assessable")
        self.assertEqual(preview["reason"], "input_too_large")
        self.assertFalse(preview["sent"])
        self.assertNotIn("payload", preview)
        audit = await self.audit(started["task_id"])
        self.assertEqual(audit["status"], "not_assessable")
        self.assertEqual(audit["reason"], "input_too_large")
        self.assertEqual(audit["items"], [])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.bridge.artifacts.for_task(started["task_id"]), before)

    async def test_structured_obligations_replace_plain_acceptance_instead_of_duplicating_it(
        self,
    ):
        items = [
            {
                "id": "AC-1",
                "text": "The behavior works on supported platforms",
                "obligations": [
                    {
                        "id": "linux",
                        "text": "The behavior works on Linux",
                        "environment": {"os": "linux"},
                    },
                    {
                        "id": "mac",
                        "text": "The behavior works on macOS",
                        "environment": {"os": "macos"},
                    },
                ],
            }
        ]
        started, before = await self.completed(
            acceptance=[items[0]["text"]],
            acceptance_set={
                "set_id": str(uuid4()),
                "revision": acceptance_revision(items),
                "items": items,
            },
        )
        audit = await self.audit(started["task_id"])
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["total"], 2)
        self.assertEqual(
            {item["ref"]["obligation_id"] for item in audit["items"]}, {"linux", "mac"}
        )
        self.assertEqual(len(self.provider.requests[0]["questions"]), 2)
        after = await self.result(started["task_id"], details=True)
        self.assertEqual(after["acceptance_coverage"], before["acceptance_coverage"])

    async def test_failed_environment_stays_relevant_after_another_environment_passes(
        self,
    ):
        criterion = "The supported-platform suite passes"
        started, _ = await self.completed("scoped-report", acceptance=[criterion])
        task_id = started["task_id"]
        for ended_at, platform, result in (
            (1, "linux", "failed"),
            (2, "macos", "passed"),
        ):
            await asyncio.to_thread(
                self.bridge.artifacts.publish,
                started["conversation_id"],
                task_id,
                CHECK_RUN_ARTIFACT,
                json.dumps(
                    {
                        "check_id": "platform-suite",
                        "run_id": str(uuid4()),
                        "criterion": criterion,
                        "role": "author",
                        "command": "PRIVATE_PLATFORM_COMMAND",
                        "scope": {"kind": "commit", "digest": "a" * 40},
                        "environment": {"os": platform},
                        "ended_at": ended_at,
                        "result": result,
                        "note": platform,
                    }
                ),
                "application/json",
            )
        before = await self.result(task_id)
        self.assertEqual(before["facts"]["checks"]["status"], "failed")
        audit = await self.audit(task_id)
        self.assertEqual(audit["status"], "completed")
        runs = {
            run["note"]: run
            for run in self.provider.requests[0]["state"]["recorded_runs"]
        }
        self.assertTrue(runs["linux"]["current_for_reported_scope"])
        self.assertTrue(runs["linux"]["unresolved_failure_for_reported_scope"])
        self.assertEqual(runs["linux"]["environment"], {"os": "linux"})
        self.assertEqual(runs["macos"]["environment"], {"os": "macos"})
        self.assertNotIn("PRIVATE_PLATFORM_COMMAND", json.dumps(self.provider.requests))
        after = await self.result(task_id)
        self.assertEqual(after["facts"], before["facts"])

    async def test_latest_observation_is_separate_for_each_environment_and_command(
        self,
    ):
        started, _ = await self.completed(
            "scoped-report", acceptance=["The suite passes on Linux and macOS"]
        )
        task_id = started["task_id"]
        observations = [
            ("old-linux", "linux", "PRIVATE_FULL_SUITE", "passed"),
            ("macos", "macos", "PRIVATE_FULL_SUITE", "passed"),
            ("new-linux", "linux", "PRIVATE_FULL_SUITE", "failed"),
            ("linux-smoke", "linux", "PRIVATE_SMOKE_CHECK", "passed"),
        ]
        for ended_at, (note, platform, command, result) in enumerate(observations, 1):
            await asyncio.to_thread(
                self.bridge.artifacts.publish,
                started["conversation_id"],
                task_id,
                CHECK_RUN_ARTIFACT,
                json.dumps(
                    {
                        "check_id": "suite",
                        "run_id": str(uuid4()),
                        "criterion": "The supported-platform suite passes",
                        "role": "author",
                        "command": command,
                        "scope": {"kind": "commit", "digest": "a" * 40},
                        "environment": {"os": platform},
                        "ended_at": ended_at,
                        "result": result,
                        "note": note,
                    }
                ),
                "application/json",
            )
        await self.audit(task_id)
        runs = {
            run["note"]: run
            for run in self.provider.requests[0]["state"]["recorded_runs"]
        }
        self.assertEqual(
            {note for note, run in runs.items() if run["current_for_reported_scope"]},
            {"macos", "new-linux", "linux-smoke"},
        )
        self.assertTrue(runs["new-linux"]["unresolved_failure_for_reported_scope"])
        self.assertNotIn("PRIVATE_FULL_SUITE", json.dumps(self.provider.requests))
        self.assertNotIn("PRIVATE_SMOKE_CHECK", json.dumps(self.provider.requests))
