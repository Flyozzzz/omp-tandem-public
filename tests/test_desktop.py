"""Desktop adapter regressions using a local MCP fixture, never real GUI access."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from anyio import CancelScope
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from omp_tandem.channel import ChannelDelivery
from omp_tandem.desktop import (
    DesktopQA,
    DesktopQAConfig,
    DesktopRequest,
    load_desktop_config,
)

# This is a deliberately controlled protocol fixture, not the Desktop QA backend.
# Files synchronize test ordering and record externally observable mutations.
MCP_FIXTURE = r"""
import asyncio
import json
import os
from pathlib import Path
import sys
from fastmcp import FastMCP

root = Path(sys.argv[1])
protocol = sys.argv[2]
with (root / "starts").open("a") as stream:
    stream.write("started\n")
(root / "backend-pid").write_text(str(os.getpid()))
server = FastMCP("Desktop adapter protocol fixture")
jobs = {
    "foreign": {"id": "foreign", "created": "fixture", "status": "queued",
                "request": {"app": "Private app"}, "report": None},
}
scopes = {"foreign": "different-project"}


def lookup(job_id, scope):
    if job_id not in jobs or scopes[job_id] != scope:
        raise ValueError("Unknown desktop job")
    job = dict(jobs[job_id])
    if job["status"] == "queued" and (root / "finished").exists():
        job["status"] = "passed"
        job["report"] = {"outcome": "passed", "observations": "Fixture only",
                         "steps_performed": ["Fixture transition"], "evidence": []}
    return job


@server.tool
async def desktop_qa_status(scope: str = "") -> dict:
    return {"protocol": protocol, "waiting_supported": True,
            "gui_execution": "client_owned", "fixture": True}


@server.tool
async def submit_desktop_test(app: str, scenario: str, expected: str,
                             launch_instructions: str = "", build: str = "",
                             scope: str = "", request_id: str = "") -> dict:
    with (root / "submissions").open("a") as stream:
        stream.write(request_id + "\n")
    if scenario == "disconnect":
        os._exit(7)
    job_id = request_id
    if job_id in jobs:
        return lookup(job_id, scope)
    jobs[job_id] = {"id": job_id, "created": "fixture", "status": "queued",
                    "request": {"app": app}, "report": None}
    scopes[job_id] = scope
    return lookup(job_id, scope)


@server.tool
async def get_desktop_test(job_id: str, scope: str = "") -> dict:
    return lookup(job_id, scope)


@server.tool
async def cancel_desktop_test(job_id: str, reason: str, scope: str = "") -> dict:
    if not reason.strip():
        raise ValueError("Cancellation reason must be nonblank")
    job = lookup(job_id, scope)
    if job["status"] == "cancelled":
        if job["report"]["cancellation"]["reason"] != reason:
            raise ValueError("Cancellation reason conflicts")
        return job
    if job["status"] != "queued":
        raise ValueError("Only queued jobs can be cancelled")
    job["status"] = "cancelled"
    job["report"] = {
        "outcome": "cancelled", "observations": reason,
        "steps_performed": [], "evidence": [],
        "cancellation": {"reason": reason, "before_execution": True},
    }
    jobs[job_id] = job
    return job


@server.tool
async def wait_desktop_test(job_id: str, scope: str = "",
                            timeout_seconds: float | None = None) -> dict:
    lookup(job_id, scope)
    (root / ("waiting-" + job_id)).touch()
    deadline = None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
    while not (root / "release").exists():
        if lookup(job_id, scope)["status"] == "cancelled":
            return lookup(job_id, scope)
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            return {**lookup(job_id, scope), "wait_expired": True}
        await asyncio.sleep(.01)
    return lookup(job_id, scope)


@server.tool
async def next_desktop_test(worker_id: str, claim_id: str, apps: list[str],
                           scope: str = "", timeout_seconds: float | None = None) -> dict:
    (root / "next-waiting").touch()
    try:
        await asyncio.Event().wait()
    finally:
        (root / "next-cancelled").touch()
    return {"status": "waiting", "execute": False}


@server.tool
async def list_desktop_tests(limit: int = 20, scope: str = "") -> list[dict]:
    return [{key: job[key] for key in ("id", "created", "status")}
            for job_id, job in jobs.items() if scopes[job_id] == scope][:limit]


server.run(show_banner=False)
"""


class DesktopAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fixture = self.root / "desktop_protocol_fixture.py"
        self.fixture.write_text(MCP_FIXTURE, encoding="utf-8")
        self.channel = ChannelDelivery(
            self.root / "events.sqlite", enabled=False, webhook_enabled=False
        )

    def adapter(self, protocol="desktop-qa/2"):
        adapter = DesktopQA(
            DesktopQAConfig(
                command=sys.executable,
                args=[str(self.fixture), str(self.root), protocol],
                cwd=self.root,
            ),
            "bound-project",
            self.channel,
        )
        self.addAsyncCleanup(adapter.close)
        return adapter

    async def wait_for_file(self, name):
        async with asyncio.timeout(10):
            while not (self.root / name).exists():
                await asyncio.sleep(0.01)

    async def submit(self, adapter, job_id="own", scenario="fixture"):
        return await adapter.perform(
            DesktopRequest(
                action="submit",
                request_id=job_id,
                app="Fixture app",
                scenario=scenario,
                expected="Fixture result",
            )
        )

    def pending(self):
        return self.channel.store.pending(self.channel.owner)

    async def test_scope_cannot_be_overridden_and_foreign_job_is_unavailable(self):
        adapter = self.adapter()
        own = await self.submit(adapter)
        self.assertEqual(own["id"], "own")
        rows = await adapter.perform(DesktopRequest(action="list"))
        self.assertEqual([row["id"] for row in rows], ["own"])
        for job_id in ("foreign", "nonexistent"):
            with self.assertRaises(RuntimeError):
                await adapter.perform(
                    DesktopRequest(action="get", job_id=job_id, wait_seconds=0)
                )
        for injected in ({"scope": "different-project"}, {"command": sys.executable}):
            with self.assertRaises(ValidationError):
                DesktopRequest(
                    action="get", job_id="foreign", wait_seconds=0, **injected
                )

    async def test_cancelling_long_wait_preserves_concurrent_calls_and_observer(self):
        adapter = self.adapter()
        waiting = asyncio.create_task(
            adapter.perform(
                DesktopRequest(
                    action="next",
                    worker_id="fixture-worker",
                    claim_id="fixture-claim",
                    apps=["Fixture app"],
                )
            )
        )
        await self.wait_for_file("next-waiting")
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        await self.wait_for_file("next-cancelled")
        rows = await adapter.perform(DesktopRequest(action="list"))
        self.assertEqual([row["id"] for row in rows], ["own"])
        (self.root / "finished").touch()
        (self.root / "release").touch()
        async with asyncio.timeout(10):
            await adapter._observers["own"]
        events = self.pending()
        self.assertEqual(
            [(event["kind"], event["payload"]) for event in events],
            [("desktop_test_finished", {"job_id": "own", "status": "passed"})],
        )
        self.assertIsNone(events[0]["task_id"])
        self.assertEqual((self.root / "starts").read_text().splitlines(), ["started"])
        await adapter.close()

    async def test_host_cancel_scope_stops_remote_wait_without_closing_connection(self):
        adapter = self.adapter()
        scopes = []

        async def host_request():
            with CancelScope() as scope:
                scopes.append(scope)
                await adapter.perform(
                    DesktopRequest(
                        action="next",
                        worker_id="host-worker",
                        claim_id="host-claim",
                        apps=["Fixture app"],
                    )
                )

        waiting = asyncio.create_task(host_request())
        await self.wait_for_file("next-waiting")
        scopes[0].cancel()
        await waiting
        await self.wait_for_file("next-cancelled")
        await self.submit(adapter)
        rows = await adapter.perform(DesktopRequest(action="list"))
        self.assertEqual([row["id"] for row in rows], ["own"])
        self.assertEqual((self.root / "starts").read_text().splitlines(), ["started"])

    async def test_terminal_get_acknowledges_only_exact_result_in_both_race_orders(
        self,
    ):
        adapter = self.adapter()
        self.channel.emit(
            "desktop_test_finished",
            {"job_id": "unrelated", "status": "failed"},
            dedupe_key="desktop:unrelated:failed",
        )
        self.channel.emit(
            "desktop_test_finished",
            {"job_id": "own", "status": "blocked"},
            dedupe_key="desktop:own:blocked",
        )
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        (self.root / "finished").touch()
        # The direct get sees the result while the background response is held.
        result = await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        self.assertEqual(result["status"], "passed")
        (self.root / "release").touch()
        async with asyncio.timeout(10):
            await adapter._observers["own"]
        self.assertEqual(
            {event["dedupe_key"] for event in self.pending()},
            {"desktop:unrelated:failed", "desktop:own:blocked"},
        )
        # Reverse order: the observer delivers before the exact result is read.
        await self.submit(adapter, job_id="second")
        async with asyncio.timeout(10):
            await adapter._observers["second"]
        self.assertIn(
            "desktop:second:passed", {e["dedupe_key"] for e in self.pending()}
        )
        await adapter.perform(DesktopRequest(action="get", job_id="second"))
        self.assertEqual(
            {event["dedupe_key"] for event in self.pending()},
            {"desktop:unrelated:failed", "desktop:own:blocked"},
        )

    async def test_cancel_observes_terminal_result_without_replaying_submission(self):
        adapter = self.adapter()
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        unrelated = self.channel.emit(
            "desktop_test_finished",
            {"job_id": "own", "status": "blocked"},
            dedupe_key="desktop:own:blocked",
        )
        request = DesktopRequest(
            action="cancel", job_id="own", reason="Obsolete queued test"
        )
        result = await adapter.perform(request)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(
            result["report"],
            {
                "outcome": "cancelled",
                "observations": request.reason,
                "steps_performed": [],
                "evidence": [],
                "cancellation": {"reason": request.reason, "before_execution": True},
            },
        )
        self.assertEqual(result["notification_bookkeeping"]["outcome"], "recorded")
        async with asyncio.timeout(10):
            await adapter._observers["own"]
        self.assertEqual(
            [event["event_id"] for event in self.pending()], [unrelated["event_id"]]
        )
        repeated = await adapter.perform(request)
        self.assertEqual(repeated["report"], result["report"])
        with self.assertRaises(RuntimeError):
            await adapter.perform(
                DesktopRequest(action="cancel", job_id="own", reason="Different reason")
            )
        existing = await self.submit(adapter)
        self.assertEqual(
            (existing["id"], existing["status"], existing["report"]),
            ("own", "cancelled", result["report"]),
        )
        rows = await adapter.perform(DesktopRequest(action="list"))
        self.assertEqual(
            [(row["id"], row["status"]) for row in rows], [("own", "cancelled")]
        )
        read = await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        self.assertEqual(read["report"], result["report"])

    async def test_external_cancellation_wakes_waiter_and_acknowledges_exact_event(
        self,
    ):
        adapter = self.adapter()
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        unrelated = self.channel.emit(
            "desktop_test_finished",
            {"job_id": "unrelated", "status": "cancelled"},
            dedupe_key="desktop:unrelated:cancelled",
        )
        # A different queue client cancels; this adapter has not observed the report.
        await adapter._call(
            "cancel_desktop_test", {"job_id": "own", "reason": "Retired by operator"}
        )
        async with asyncio.timeout(10):
            await adapter._observers["own"]
        self.assertEqual(
            [(event["kind"], event["payload"]) for event in self.pending()],
            [
                (
                    "desktop_test_finished",
                    {"job_id": "unrelated", "status": "cancelled"},
                ),
                ("desktop_test_finished", {"job_id": "own", "status": "cancelled"}),
            ],
        )
        async with asyncio.timeout(10):
            result = await adapter.perform(DesktopRequest(action="get", job_id="own"))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["report"]["observations"], "Retired by operator")
        self.assertEqual(
            [event["event_id"] for event in self.pending()], [unrelated["event_id"]]
        )

    async def test_cancel_cannot_reach_foreign_jobs_or_replace_completed_results(self):
        adapter = self.adapter()
        await self.submit(adapter)
        for job_id in ("foreign", "nonexistent"):
            with self.assertRaises(RuntimeError):
                await adapter.perform(
                    DesktopRequest(action="cancel", job_id=job_id, reason="Retired")
                )
        queued = await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        self.assertEqual(queued["status"], "queued")
        (self.root / "finished").touch()
        completed = await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        with self.assertRaises(RuntimeError):
            await adapter.perform(
                DesktopRequest(action="cancel", job_id="own", reason="Too late")
            )
        unchanged = await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        self.assertEqual(unchanged["status"], "passed")
        self.assertEqual(unchanged["report"], completed["report"])

    async def cancellation_failure(self, *, stall):
        adapter = self.adapter()
        waiting = asyncio.create_task(
            adapter.perform(
                DesktopRequest(
                    action="next",
                    worker_id="failure-worker",
                    claim_id="failure-claim",
                    apps=["Fixture app"],
                )
            )
        )
        await self.wait_for_file("next-waiting")

        async def fail_notification(*args, **kwargs):
            if stall:
                await asyncio.Event().wait()
            raise OSError("Synthetic cancellation transport failure")

        with patch.object(adapter._session, "send_notification", fail_notification):
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
        status = await adapter.perform(DesktopRequest(action="status"))
        self.assertEqual(status["connection"], "retired")
        self.assertTrue(status["reconciliation_required"])
        with self.assertRaises(RuntimeError):
            await self.submit(adapter)
        self.assertFalse((self.root / "submissions").exists())
        pid = int((self.root / "backend-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_failed_cancel_delivery_retires_backend_without_replaying(self):
        await self.cancellation_failure(stall=False)

    async def test_stalled_cancel_delivery_retires_backend_without_replaying(self):
        await self.cancellation_failure(stall=True)

    async def test_busy_event_store_preserves_report_and_other_calls(self):
        adapter = self.adapter()
        unrelated = self.channel.emit(
            "desktop_test_finished",
            {"job_id": "unrelated", "status": "failed"},
            dedupe_key="desktop:unrelated:failed",
        )
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        (self.root / "finished").touch()
        with closing(
            sqlite3.connect(self.channel.store.db_path, isolation_level=None)
        ) as holder:
            holder.execute("BEGIN IMMEDIATE")
            try:
                async with asyncio.timeout(2):
                    result, rows = await asyncio.gather(
                        adapter.perform(
                            DesktopRequest(action="get", job_id="own", wait_seconds=0)
                        ),
                        adapter.perform(DesktopRequest(action="list")),
                    )
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["report"]["outcome"], "passed")
                self.assertEqual(
                    result["notification_bookkeeping"]["outcome"], "deferred"
                )
                self.assertEqual([row["id"] for row in rows], ["own"])
            finally:
                holder.rollback()
        (self.root / "release").touch()
        await adapter._observers["own"]
        await adapter.perform(
            DesktopRequest(action="get", job_id="own", wait_seconds=0)
        )
        self.assertEqual(
            [event["event_id"] for event in self.pending()], [unrelated["event_id"]]
        )

    async def test_deferred_terminal_notification_recovers_without_model_polling(self):
        adapter = self.adapter()
        await self.submit(adapter)
        await self.wait_for_file("waiting-own")
        with closing(
            sqlite3.connect(self.channel.store.db_path, isolation_level=None)
        ) as holder:
            holder.execute("BEGIN IMMEDIATE")
            try:
                (self.root / "finished").touch()
                (self.root / "release").touch()
                async with asyncio.timeout(2):
                    await adapter._observers["own"]
                self.assertEqual(self.pending(), [])
            finally:
                holder.rollback()
        async with asyncio.timeout(3):
            while not self.pending():
                await asyncio.sleep(0.02)
        self.assertEqual(
            [event["dedupe_key"] for event in self.pending()], ["desktop:own:passed"]
        )

    async def test_protocol_mismatch_refuses_mutation_before_backend_execution(self):
        adapter = self.adapter(protocol="desktop-qa/1")
        status = await adapter.perform(DesktopRequest(action="status"))
        self.assertEqual(status["protocol"], "desktop-qa/1")
        with self.assertRaises(RuntimeError):
            await self.submit(adapter)
        self.assertFalse((self.root / "submissions").exists())
        self.assertEqual(self.pending(), [])

    async def test_transport_loss_does_not_replay_a_committed_mutation(self):
        adapter = self.adapter()
        async with asyncio.timeout(10):
            with self.assertRaises(McpError):
                await self.submit(adapter, scenario="disconnect")
        await adapter.close()
        self.assertEqual((self.root / "submissions").read_text().splitlines(), ["own"])
        self.assertEqual((self.root / "starts").read_text().splitlines(), ["started"])
        self.assertEqual(self.pending(), [])

    async def test_disabled_adapter_has_no_process_side_effects(self):
        adapter = DesktopQA(None, "bound-project", self.channel)
        with patch(
            "omp_tandem.desktop.stdio_client", side_effect=AssertionError("spawned")
        ):
            self.assertFalse(
                (await adapter.perform(DesktopRequest(action="status")))["configured"]
            )
            with self.assertRaises(RuntimeError):
                await self.submit(adapter)
            await adapter.close()

    def test_request_rejects_inappropriate_fields_and_unsubstantiated_results(self):
        for request in (
            {"action": "status", "wait_seconds": None},
            {"action": "list", "job_id": "ignored"},
            {"action": "submit", "app": "Fixture", "scenario": "x", "expected": "y"},
            {"action": "next", "worker_id": "w", "claim_id": "c", "apps": [""]},
            {"action": "get", "job_id": "j", "wait_seconds": float("inf")},
            {"action": "cancel", "job_id": "j"},
            {"action": "cancel", "reason": "Retired"},
            {"action": "cancel", "job_id": " ", "reason": "Retired"},
            {"action": "cancel", "job_id": "j", "reason": " "},
            {"action": "cancel", "job_id": "j", "reason": "Retired", "wait_seconds": 0},
            {
                "action": "cancel",
                "job_id": "j",
                "reason": "Retired",
                "scope": "foreign",
            },
            {
                "action": "cancel",
                "job_id": "j",
                "reason": "Retired",
                "claim_token": "t",
            },
            {"action": "get", "job_id": "j", "reason": "Retired"},
            {
                "action": "finish",
                "job_id": "j",
                "claim_token": "fixture-token",
                "report": {
                    "outcome": "cancelled",
                    "observations": "Worker cannot retire work",
                    "steps_performed": ["Attempted cancellation"],
                    "evidence": [],
                },
            },
            {
                "action": "finish",
                "job_id": "j",
                "claim_token": "fixture-token",
                "report": {
                    "outcome": "passed",
                    "observations": "claimed",
                    "steps_performed": [],
                    "evidence": [],
                },
            },
        ):
            with self.subTest(request=request), self.assertRaises(ValidationError):
                DesktopRequest(**request)

    def test_operator_config_requires_absolute_cwd_and_rejects_extra_fields(self):
        config = self.root / "desktop.json"
        config.write_text(
            json.dumps({"command": sys.executable, "args": [], "cwd": "."})
        )
        with self.assertRaises(ValidationError):
            load_desktop_config(config)
        config.write_text(
            json.dumps(
                {
                    "command": sys.executable,
                    "args": [],
                    "cwd": str(self.root),
                    "scope": "override",
                }
            )
        )
        with self.assertRaises(ValidationError):
            load_desktop_config(config)
