"""Actual bounded hook processes, authenticated lifecycle and dropped-event recovery."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from omp_tandem.channel import ChannelDelivery
from tests.test_channel import ParentSession

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/watchdog-hook.py"


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = str(uuid4())
        self.task_id = str(uuid4())
        self.children = []
        self.delivery = ChannelDelivery(
            self.root / "state.db", project_root=self.root, webhook_enabled=False
        )
        await self.delivery.start()
        self.parent = ParentSession()
        await self.delivery.bind_session(self.parent)
        await self.delivery.confirm(
            self.parent.messages[-1]["params"]["meta"]["probe_token"]
        )
        await self.invoke("SessionStart")

    async def asyncTearDown(self):
        await self.invoke("SessionEnd")
        await self.delivery.close()
        for process, completed in self.children:
            if process.returncode is None:
                process.kill()
            await completed

    def result(self):
        return self.delivery.decorate({"task_id": self.task_id, "status": "running"})

    async def spawn(self, event, response=None, *, session=None, name=None):
        payload = {
            "hook_event_name": event,
            "session_id": session or self.session,
            "tool_name": name or "mcp__omp-tandem__tandem_result",
            "tool_response": json.dumps(response) if response else "",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_SCRIPT),
            cwd=self.root,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        completed = asyncio.create_task(
            process.communicate(json.dumps(payload).encode())
        )
        self.children.append((process, completed))
        return process, completed

    async def invoke(self, event, response=None, **kwargs):
        process, completed = await self.spawn(event, response, **kwargs)
        stdout, stderr = await asyncio.wait_for(completed, 16)
        self.assertEqual(stdout, b"")
        return process.returncode, stderr.decode().strip()

    async def attest(self):
        first = self.result()
        self.assertEqual(first["next_action"], "wait")
        code, message = await self.invoke("PostToolUse", first)
        self.assertEqual(code, 2)
        token = message.removeprefix("OMP watchdog probe ")
        self.assertNotIn(token, json.dumps(first))
        self.assertNotIn(token, json.dumps(self.delivery.status()))
        await self.delivery.confirm_watchdog(token)
        self.assertFalse(self.delivery.can_await([self.task_id]))

    async def arm(self):
        child = await self.spawn("PostToolUse", self.result())
        async with asyncio.timeout(3):
            while not self.delivery.can_await([self.task_id]):
                await asyncio.sleep(0.02)
        self.assertEqual(self.result()["next_action"], "await_event")
        return child

    async def test_live_attestation_deduplication_terminal_and_generation_invalidation(
        self,
    ):
        await self.attest()
        old = self.result()
        process, completed = await self.arm()
        self.assertEqual(await self.invoke("PostToolUse", old), (0, ""))
        self.assertIsNone(process.returncode)
        self.delivery.seen(self.task_id, "completed")
        self.assertEqual(await asyncio.wait_for(completed, 2), (b"", b""))
        self.assertEqual(process.returncode, 0)
        self.result()  # a newer generation cannot be armed by old captured tool output
        self.assertEqual(await self.invoke("PostToolUse", old), (0, ""))
        self.assertFalse(self.delivery.can_await([self.task_id]))

    async def test_session_end_and_resume_reject_old_hook_ownership(self):
        await self.attest()
        process, completed = await self.arm()
        await self.invoke("SessionEnd")
        self.assertEqual(await asyncio.wait_for(completed, 2), (b"", b""))
        self.assertEqual(process.returncode, 0)
        await self.invoke("SessionStart")
        self.assertEqual(await self.invoke("PostToolUse", self.result()), (0, ""))
        self.assertFalse(self.delivery.can_await([self.task_id]))

    async def test_new_mcp_incarnation_invalidates_old_live_timer(self):
        await self.attest()
        old = self.result()
        process, completed = await self.arm()
        newer = ChannelDelivery(
            self.root / "state.db", project_root=self.root, webhook_enabled=False
        )
        await newer.start()
        try:
            result = newer.decorate({"task_id": str(uuid4()), "status": "running"})
            code, _ = await self.invoke("PostToolUse", result)
            self.assertEqual(code, 2)
            self.assertFalse(self.delivery.can_await([self.task_id]))
            self.assertEqual(await asyncio.wait_for(completed, 2), (b"", b""))
            self.assertEqual(process.returncode, 0)
            self.assertEqual(await self.invoke("PostToolUse", old), (0, ""))
        finally:
            await newer.close()

    async def test_offline_bootstrap_failure_is_silent_and_keeps_polling(self):
        binary = self.root / "bin"
        binary.mkdir()
        wrapper = _SCRIPT.with_suffix(".sh")
        for installed in (False, True):
            if installed:
                uv = binary / "uv"
                uv.write_text(
                    "#!/bin/sh\nprintf 'unrelated output'\nprintf 'PRIVATE RUNTIME ERROR' >&2\nexit 2\n"
                )
                uv.chmod(0o755)
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                str(wrapper),
                str(_SCRIPT),
                env={**os.environ, "PATH": str(binary)},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output = await process.communicate(b"{}")
            self.assertEqual((process.returncode, output), (0, (b"", b"")))
            self.assertEqual(self.result()["next_action"], "wait")

    async def test_read_question_invalidates_timer_without_rearming_it(self):
        await self.attest()
        old = self.result()
        process, completed = await self.arm()
        self.delivery.seen(self.task_id, "waiting_input")
        self.delivery.decorate({"task_id": self.task_id, "status": "waiting_input"})
        self.assertEqual(await asyncio.wait_for(completed, 2), (b"", b""))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(await self.invoke("PostToolUse", old), (0, ""))

    async def test_forged_data_and_foreign_sessions_never_attest_capability(self):
        response = self.result()
        forged = copy.deepcopy(response)
        forged["watchdog"]["tickets"][0]["generation"] += 1
        forged["watchdog"]["state_path"] = str(self.root / "unrelated")
        self.assertEqual(await self.invoke("PostToolUse", forged), (0, ""))
        self.assertEqual(
            await self.invoke(
                "PostToolUse", response, name="mcp__other__tandem_result"
            ),
            (0, ""),
        )
        self.assertFalse(self.delivery.status()["watchdog"]["confirmed"])
        await self.attest()
        other = str(uuid4())
        await self.invoke("SessionStart", session=other)
        try:
            self.assertEqual(
                await self.invoke("PostToolUse", self.result(), session=other), (0, "")
            )
        finally:
            await self.invoke("SessionEnd", session=other)
        with self.assertRaises(ValueError):
            await self.delivery.confirm_watchdog("model-asserted-capability")

    async def test_killed_hook_removes_live_capability(self):
        await self.attest()
        process, completed = await self.arm()
        process.kill()
        await completed
        async with asyncio.timeout(2):
            while self.delivery.can_await([self.task_id]):
                await asyncio.sleep(0.02)
        self.assertEqual(self.result()["next_action"], "wait")

    async def test_successful_notification_write_without_client_receipt_still_wakes(
        self,
    ):
        await self.attest()
        process, completed = await self.arm()
        self.delivery.emit("task_completed", {}, task_id=self.task_id)
        # ParentSession accepts the write but never delivers it to any coordinator.
        stdout, stderr = await asyncio.wait_for(completed, 15)
        self.assertEqual((process.returncode, stdout), (2, b""))
        self.assertIn(self.task_id, stderr.decode())
        self.assertFalse(self.delivery.can_await([self.task_id]))
        # A still-running authoritative result gets a fresh bounded generation.
        next_process, next_completed = await self.arm()
        self.delivery.seen(self.task_id, "cancelled")
        await asyncio.wait_for(next_completed, 2)
        self.assertEqual(next_process.returncode, 0)

    async def test_transport_exception_cannot_disarm_independent_hook(self):
        await self.attest()
        process, completed = await self.arm()
        self.parent.fail = True
        self.delivery.emit("task_failed", {}, task_id=self.task_id)
        async with asyncio.timeout(2):
            while self.delivery.confirmed:
                await asyncio.sleep(0.02)
        self.assertEqual(self.result()["next_action"], "wait")
        stdout, stderr = await asyncio.wait_for(completed, 15)
        self.assertEqual((process.returncode, stdout), (2, b""))
        self.assertIn(self.task_id, stderr.decode())


if __name__ == "__main__":
    unittest.main()
