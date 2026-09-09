"""Real stdio handshake, push event and webhook round trips; no model provider calls."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_peer


class ChannelStdioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.process = None
        self.notifications = []
        self.counter = 0

    async def asyncTearDown(self):
        if self.process is not None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def start(self, disabled=False, configured_root=True):
        peer = make_peer(self.root)
        stderr = (self.root / "stderr.log").open("wb")
        self.addCleanup(stderr.close)
        command = [
            sys.executable,
            "-I",
            "-c",
            "import faulthandler, signal; faulthandler.register(signal.SIGUSR1); "
            "from omp_tandem.cli import main; main()",
            "--state-dir",
            str(self.root / "state"),
            "--omp",
            str(peer),
            "--model",
            "unused",
        ]
        if configured_root:
            command.extend(["--project-root", str(self.root)])
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "CLAUDE_PROJECT_DIR",
                "PLUGIN_ROOT",
                "PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA",
            )
        }
        environment.update(OMP_TANDEM_CHANNEL="1", OMP_TANDEM_WEBHOOK="1")
        if disabled:
            command.append("--disable-channel")
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            limit=1024 * 1024,
        )
        initialized = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "test"},
            },
        )
        capabilities = initialized["result"]["capabilities"]["experimental"]
        self.assertEqual(capabilities["claude/channel"], {})
        self.assertNotIn("claude/channel/permission", capabilities)
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await self.request("tools/list", {})

    async def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def read(self):
        try:
            raw = await asyncio.wait_for(self.process.stdout.readline(), 6)
        except TimeoutError:
            if self.process.returncode is None:
                self.process.send_signal(signal.SIGUSR1)
                await asyncio.sleep(0.1)
            self.fail(
                "MCP response timed out; child diagnostics:\n"
                + (self.root / "stderr.log").read_text()
            )
        self.assertTrue(raw, (self.root / "stderr.log").read_text())
        return json.loads(raw)

    async def request(self, method, params):
        self.counter += 1
        identifier = self.counter
        await self.send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        while True:
            frame = await self.read()
            if frame.get("id") == identifier:
                self.assertNotIn("error", frame)
                return frame
            self.notifications.append(frame)

    async def call(self, name, arguments):
        response = await self.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        self.assertFalse(response["result"].get("isError"), response)
        return response["result"].get("structuredContent") or json.loads(
            response["result"]["content"][0]["text"]
        )

    async def notification(self, kind):
        while True:
            for index, frame in enumerate(self.notifications):
                if (
                    frame.get("method") == "notifications/claude/channel"
                    and frame["params"]["meta"]["event_type"] == kind
                ):
                    return self.notifications.pop(index)
            self.notifications.append(await self.read())

    def post(self, descriptor):
        token = Path(descriptor["token_file"]).read_text().strip()
        port = int(descriptor["url"].split(":")[2].split("/")[0])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(
                "POST",
                "/webhook",
                body=json.dumps({"id": "wire-ci", "content": "CI complete"}),
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            return json.loads(response.read())
        finally:
            connection.close()

    async def test_standalone_launch_cwd_receives_discovery_probe(self):
        await self.start(configured_root=False)
        probe = await self.notification("channel_probe")
        confirmed = await self.call(
            "tandem_channel",
            {"action": "ack", "probe_token": probe["params"]["meta"]["probe_token"]},
        )
        self.assertEqual(confirmed["delivery"], "push")
        scope = await self.call("tandem_scope", {})
        self.assertEqual(scope["project_root"], str(self.root.resolve()))
        self.assertEqual(scope["root_source"], "launch_cwd")

    async def test_push_task_and_webhook_need_no_status_polling(self):
        await self.start()
        probe = await self.notification("channel_probe")
        confirmed = await self.call(
            "tandem_channel",
            {"action": "ack", "probe_token": probe["params"]["meta"]["probe_token"]},
        )
        self.assertEqual(confirmed["delivery"], "push")
        job = await self.call(
            "tandem_start",
            {
                "cwd": str(self.root),
                "mode": "think",
                "prompt": "paragraph",
                "timeout_seconds": 10,
            },
        )
        self.assertEqual(job["next_action"], "await_event")
        ready = await self.notification("task_completed")
        self.assertEqual(ready["params"]["meta"]["task_id"], job["task_id"])
        result = await self.call("tandem_result", {"task_id": job["task_id"]})
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "success")
        )
        self.assertEqual(
            result["answer"], "Поручайте независимый анализ; приёмку проверяйте сами."
        )
        descriptor_path = Path(confirmed["webhook"]["descriptor_file"])
        descriptor = json.loads(descriptor_path.read_text())
        accepted = await asyncio.to_thread(self.post, descriptor)
        event = await self.notification("webhook")
        self.assertEqual(event["params"]["meta"]["event_id"], accepted["event_id"])
        self.assertIn("CI complete", event["params"]["content"])
        await self.call(
            "tandem_channel", {"action": "ack", "event_id": accepted["event_id"]}
        )
        pending = await self.call("tandem_channel", {"action": "pending"})
        self.assertEqual(pending["events"], [])
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 10)
        self.assertFalse(descriptor_path.exists())
        self.assertFalse(Path(descriptor["token_file"]).exists())

    async def test_explicit_poll_mode_keeps_original_workflow(self):
        await self.start(disabled=True)
        job = await self.call(
            "tandem_start",
            {
                "cwd": str(self.root),
                "mode": "think",
                "prompt": "paragraph",
                "timeout_seconds": 10,
            },
        )
        self.assertEqual((job["delivery"], job["next_action"]), ("poll", "wait"))
        result = await self.call(
            "tandem_result", {"task_id": job["task_id"], "wait_seconds": 5}
        )
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "success")
        )
        status = await self.call("tandem_channel", {"action": "status"})
        self.assertFalse(status["confirmed"])
        self.assertFalse(status["webhook"]["enabled"])
        self.assertEqual(self.notifications, [])


if __name__ == "__main__":
    unittest.main()
