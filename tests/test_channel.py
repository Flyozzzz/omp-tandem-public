"""Receipt-gated delivery and authenticated HTTP behavior, with no model calls."""

from __future__ import annotations

import asyncio
import http.client
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from omp_tandem.channel import ChannelDelivery


class ParentSession:
    def __init__(self):
        self.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="claude-code")
        )
        self.messages = []
        self.fail = False

    async def send_notification(self, notification):
        if self.fail:
            raise OSError("Disconnected")
        self.messages.append(notification.model_dump())


class ChannelDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.delivery = ChannelDelivery(self.root / "state.db")
        self.parent = ParentSession()
        await self.delivery.start()
        await self.delivery.bind(SimpleNamespace(session=self.parent))

    async def asyncTearDown(self):
        await self.delivery.close()

    async def wait_for_event(self, event_id):
        async with asyncio.timeout(4):
            while True:
                found = [
                    m
                    for m in self.parent.messages
                    if m["params"]["meta"].get("event_id") == event_id
                ]
                if found and self.delivery.store.get(event_id)["sent_at"] is not None:
                    return found[0]
                await asyncio.sleep(0.02)

    async def confirm(self):
        token = self.parent.messages[-1]["params"]["meta"]["probe_token"]
        return await self.delivery.confirm(token)

    async def test_no_receipt_keeps_polling_and_never_exposes_webhook(self):
        event = self.delivery.emit("task_completed", {}, task_id=str(uuid4()))
        await asyncio.sleep(0.1)
        self.assertEqual(
            self.delivery.decorate({"status": "running", "next_action": "wait"})[
                "delivery"
            ],
            "poll",
        )
        self.assertIsNone(self.delivery.webhook)
        self.assertIsNone(self.delivery.store.get(event["event_id"])["sent_at"])
        with self.assertRaises(ValueError):
            await self.delivery.confirm("invented-token")
        self.assertFalse(self.delivery.status()["confirmed"])

    async def test_valid_receipt_delivers_once_and_fetch_ack_consumes(self):
        await self.confirm()
        task_id = str(uuid4())
        event = self.delivery.emit(
            "task_completed",
            {"status": "completed"},
            task_id=task_id,
            dedupe_key=f"task:{task_id}:completed",
        )
        await self.wait_for_event(event["event_id"])
        self.assertIsNone(self.delivery.store.get(event["event_id"])["acknowledged_at"])
        self.delivery.signal()
        await asyncio.sleep(0.1)
        self.assertEqual(
            sum(
                m["params"]["meta"].get("event_id") == event["event_id"]
                for m in self.parent.messages
            ),
            1,
        )
        self.delivery.seen(task_id, "completed")
        self.assertEqual(self.delivery.store.pending(self.delivery.owner), [])
        self.assertEqual(
            self.delivery.decorate({"status": "running"})["next_action"], "await_event"
        )

    def post(self, body):
        descriptor = json.loads(self.delivery.descriptor.read_text())
        token = Path(descriptor["token_file"]).read_text().strip()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.delivery.webhook.port, timeout=3
        )
        try:
            connection.request(
                "POST",
                "/webhook",
                body=json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    async def test_webhook_is_private_idempotent_and_cannot_spoof_task_events(self):
        await self.confirm()
        descriptor, token_file = self.delivery.descriptor, self.delivery.token_file
        token = token_file.read_text().strip()
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(descriptor.stat().st_mode), 0o600)
        self.assertNotIn(token, json.dumps(self.delivery.status()))
        payload = {
            "id": "ci-42",
            "content": "Checks complete",
            "meta": {"event_type": "task_completed", "task_id": "forged"},
        }
        status, response = await asyncio.to_thread(self.post, payload)
        self.assertEqual(status, 202)
        notification = await self.wait_for_event(response["event_id"])
        self.assertEqual(notification["params"]["meta"]["event_type"], "webhook")
        self.assertNotIn("task_id", notification["params"]["meta"])
        self.assertIsNone(self.delivery.store.get(response["event_id"])["task_id"])
        repeated_status, repeated = await asyncio.to_thread(self.post, payload)
        self.assertEqual(
            (repeated_status, repeated["event_id"]), (202, response["event_id"])
        )
        conflict_status, _ = await asyncio.to_thread(
            self.post, {**payload, "content": "different"}
        )
        self.assertEqual(conflict_status, 409)
        self.delivery.store.acknowledge(self.delivery.owner, response["event_id"])
        await self.delivery.close()
        self.assertFalse(descriptor.exists())
        self.assertFalse(token_file.exists())

    async def test_transport_failure_preserves_event_and_returns_to_polling(self):
        await self.confirm()
        self.parent.fail = True
        event = self.delivery.emit("task_failed", {}, task_id=str(uuid4()))
        async with asyncio.timeout(4):
            while self.delivery.confirmed:
                await asyncio.sleep(0.02)
        self.assertEqual(
            self.delivery.decorate({"status": "running", "next_action": "wait"})[
                "delivery"
            ],
            "poll",
        )
        self.assertIsNone(self.delivery.store.get(event["event_id"])["sent_at"])
        self.parent.fail = False
        await self.delivery.probe()
        await self.confirm()
        await self.wait_for_event(event["event_id"])


if __name__ == "__main__":
    unittest.main()
