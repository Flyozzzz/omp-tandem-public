"""Outbox delivery and recovery regressions using disposable SQLite state."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.events import EventConflict, EventStore, QueueFull


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "tasks.sqlite3"
        self.store = EventStore(self.db_path)
        self.owner = str(uuid4())
        self.other = str(uuid4())
        self.task_id = str(uuid4())

    def test_dedupe_survives_acknowledgment_and_rejects_changed_event(self):
        first = self.store.enqueue(
            self.owner,
            "task.done",
            {"b": 2, "a": {"z": 3, "x": 1}},
            self.task_id,
            "finished",
        )
        self.store.acknowledge(self.owner, first["event_id"])
        repeated = self.store.enqueue(
            self.owner,
            "task.done",
            {"a": {"x": 1, "z": 3}, "b": 2},
            self.task_id,
            "finished",
        )
        self.assertEqual(repeated["event_id"], first["event_id"])
        self.assertEqual(self.store.pending(self.owner), [])
        for kind, task_id, payload in (
            ("task.failed", self.task_id, first["payload"]),
            ("task.done", str(uuid4()), first["payload"]),
            ("task.done", self.task_id, {"a": 1}),
        ):
            with (
                self.subTest(kind=kind, task_id=task_id, payload=payload),
                self.assertRaises(EventConflict),
            ):
                self.store.enqueue(self.owner, kind, payload, task_id, "finished")
        unkeyed = [self.store.enqueue(self.owner, "task.done", {}) for _ in range(2)]
        self.assertNotEqual(unkeyed[0]["event_id"], unkeyed[1]["event_id"])

    def test_owner_routing_and_acknowledgment_are_isolated(self):
        first = self.store.enqueue(
            self.owner, "webhook", {"value": 1}, dedupe_key="same"
        )
        second = self.store.enqueue(
            self.other, "webhook", {"value": 2}, dedupe_key="same"
        )
        self.assertEqual(
            [e["event_id"] for e in self.store.pending(self.owner)], [first["event_id"]]
        )
        self.assertEqual(
            [e["event_id"] for e in self.store.pending(self.other)],
            [second["event_id"]],
        )
        self.assertEqual(
            [e["event_id"] for e in self.store.pending(None)],
            [first["event_id"], second["event_id"]],
        )
        for event_id in (first["event_id"], str(uuid4())):
            with self.assertRaises(ValueError):
                self.store.acknowledge(self.other, event_id)
        self.assertTrue(self.store.acknowledge(self.owner, first["event_id"]))
        self.assertFalse(self.store.acknowledge(self.owner, first["event_id"]))
        self.assertEqual(self.store.acknowledge_key(self.owner, "same"), 0)
        self.assertEqual(self.store.acknowledge_key(self.other, "absent"), 0)
        self.assertEqual(self.store.acknowledge_key(self.other, "same"), 1)
        self.assertEqual(self.store.acknowledge_key(self.other, "same"), 0)
        self.assertEqual(self.store.pending(None), [])

    def test_external_transaction_rolls_back_state_and_outbox_together(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE task_state (status TEXT)")
            db.execute("INSERT INTO task_state VALUES ('running')")
            with self.assertRaises(RuntimeError), db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE task_state SET status='done'")
                event = self.store.enqueue(
                    self.owner,
                    "task.done",
                    {},
                    self.task_id,
                    connection=db,
                )
                self.assertTrue(db.in_transaction)
                self.assertEqual(self.store.pending(self.owner), [])
                raise RuntimeError("abort")
            self.assertEqual(
                db.execute("SELECT status FROM task_state").fetchone()[0], "running"
            )
            with self.assertRaises(ValueError):
                self.store.get(event["event_id"])
            with db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE task_state SET status='done'")
                committed = self.store.enqueue(
                    self.owner,
                    "task.done",
                    {},
                    self.task_id,
                    connection=db,
                )
            self.assertEqual(
                db.execute("SELECT status FROM task_state").fetchone()[0], "done"
            )
            self.assertEqual(
                self.store.pending(self.owner)[0]["event_id"], committed["event_id"]
            )

    def test_sent_event_remains_pending_and_can_be_replayed_after_reopening(self):
        event = self.store.enqueue(self.owner, "task.done", {}, self.task_id, "done")
        acknowledged = self.store.enqueue(self.owner, "task.started", {}, self.task_id)
        self.store.acknowledge(self.owner, acknowledged["event_id"])
        self.store.mark_sent(self.owner, event["event_id"])
        reopened = EventStore(self.db_path)
        pending = reopened.pending(self.owner)
        self.assertEqual([e["event_id"] for e in pending], [event["event_id"]])
        self.assertIsNotNone(pending[0]["sent_at"])
        self.assertEqual(reopened.adopt(self.task_id, self.other), 1)
        self.assertEqual(reopened.pending(self.owner), [])
        recovered = reopened.pending(self.other)[0]
        self.assertEqual(recovered["event_id"], event["event_id"])
        self.assertIsNone(recovered["sent_at"])
        self.assertEqual(reopened.get(acknowledged["event_id"])["owner"], self.owner)
        with self.assertRaises(ValueError):
            reopened.acknowledge(self.owner, event["event_id"])
        self.assertTrue(reopened.acknowledge(self.other, event["event_id"]))
        self.assertEqual(EventStore(self.db_path).pending(None), [])

    def test_sent_backlog_cannot_starve_fresh_delivery(self):
        for index in range(260):
            old = self.store.enqueue(self.owner, "task.done", {}, dedupe_key=str(index))
            self.store.mark_sent(self.owner, old["event_id"])
        fresh = self.store.enqueue(self.owner, "task.done", {"ready": True})
        pending = self.store.pending(self.owner, 1, unsent_only=True)
        self.assertEqual([event["event_id"] for event in pending], [fresh["event_id"]])

    def test_task_adoption_collision_rolls_back_all_events(self):
        events = [
            self.store.enqueue(self.owner, "task.done", {}, self.task_id, key)
            for key in ("free", "collision")
        ]
        target = self.store.enqueue(self.other, "task.done", {}, dedupe_key="collision")
        self.store.acknowledge(self.other, target["event_id"])
        for event in events:
            self.store.mark_sent(self.owner, event["event_id"])
        with self.assertRaises(EventConflict):
            self.store.adopt(self.task_id, self.other)
        pending = self.store.pending(self.owner)
        self.assertEqual(
            [e["event_id"] for e in pending], [e["event_id"] for e in events]
        )
        self.assertTrue(all(e["sent_at"] is not None for e in pending))
        self.assertEqual(self.store.pending(self.other), [])

    def test_explicit_webhook_adoption_preserves_id_and_rejects_collision(self):
        event = self.store.enqueue(
            self.owner, "webhook", {"message": "hello"}, dedupe_key="request"
        )
        self.store.mark_sent(self.owner, event["event_id"])
        collision = self.store.enqueue(self.other, "webhook", {}, dedupe_key="request")
        with self.assertRaises(EventConflict):
            self.store.adopt_event(event["event_id"], self.other)
        self.assertIsNotNone(self.store.get(event["event_id"])["sent_at"])
        recovery_owner = str(uuid4())
        recovered = EventStore(self.db_path).adopt_event(
            event["event_id"], recovery_owner
        )
        self.assertEqual(recovered["event_id"], event["event_id"])
        self.assertIsNone(recovered["sent_at"])
        self.assertEqual(self.store.pending(self.owner), [])
        self.assertEqual(
            self.store.pending(recovery_owner)[0]["payload"], {"message": "hello"}
        )
        self.assertEqual(
            self.store.pending(self.other)[0]["event_id"], collision["event_id"]
        )
        self.store.acknowledge(recovery_owner, event["event_id"])
        with self.assertRaises(ValueError):
            self.store.adopt_event(event["event_id"], self.owner)

    def test_webhook_capacity_does_not_block_lifecycle_or_deduplication(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            webhooks = [
                self.store.enqueue(
                    self.owner, "webhook", {}, dedupe_key=str(i), connection=db
                )
                for i in range(256)
            ]
        self.assertEqual(
            self.store.enqueue(self.owner, "webhook", {}, dedupe_key="0")["event_id"],
            webhooks[0]["event_id"],
        )
        with self.assertRaises(QueueFull):
            self.store.enqueue(self.owner, "webhook", {})
        lifecycle = self.store.enqueue(self.owner, "task.done", {}, self.task_id)
        self.assertTrue(self.store.acknowledge(self.owner, lifecycle["event_id"]))
        incoming = self.store.enqueue(self.other, "webhook", {})
        with self.assertRaises(QueueFull):
            self.store.adopt_event(incoming["event_id"], self.owner)
        self.assertEqual(self.store.get(incoming["event_id"])["owner"], self.other)
        self.store.mark_sent(self.owner, webhooks[0]["event_id"])
        with self.assertRaises(QueueFull):
            self.store.enqueue(self.owner, "webhook", {})
        self.store.acknowledge(self.owner, webhooks[0]["event_id"])
        self.store.adopt_event(incoming["event_id"], self.owner)
        self.assertEqual(self.store.pending(self.other), [])
        with self.assertRaises(QueueFull):
            self.store.enqueue(self.owner, "webhook", {})

    def test_utf8_payload_boundary_and_invalid_inputs(self):
        # Canonical JSON wrapper {"x":""} uses eight bytes; each snow character uses three.
        accepted = self.store.enqueue(self.owner, "webhook", {"x": "雪" * 10920})
        self.assertEqual(
            self.store.get(accepted["event_id"])["payload"]["x"], "雪" * 10920
        )
        for payload in ({"x": "雪" * 10920 + "x"}, {"x": float("nan")}):
            with self.assertRaises(ValueError):
                self.store.enqueue(self.owner, "webhook", payload)
        for kwargs in (
            {"kind": ""},
            {"kind": "x" * 65},
            {"dedupe_key": " "},
            {"dedupe_key": "x" * 257},
            {"task_id": "invalid"},
        ):
            arguments = {
                "owner": self.owner,
                "kind": "webhook",
                "payload": {},
                **kwargs,
            }
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.enqueue(**arguments)
        for limit in (0, 257, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.pending(None, limit)
        self.assertEqual(len(self.store.pending(self.owner, 1)), 1)


if __name__ == "__main__":
    unittest.main()
