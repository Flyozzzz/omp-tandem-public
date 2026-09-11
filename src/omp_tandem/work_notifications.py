"""Wake attached clients from committed shared-work changes; polling remains authoritative."""

import asyncio
import logging
import sqlite3
from contextlib import suppress

from .events import QueueFull

logger = logging.getLogger(__name__)


class WorkNotifications:
    def __init__(self, bridge):
        self.bridge = bridge
        self.task = None
        self.cursor = 0

    async def start(self):
        if self.task is not None or self.bridge.work_token:
            return
        self.cursor = await asyncio.to_thread(self.bridge.work_items.event_head)
        self.task = asyncio.create_task(self._run())

    async def close(self):
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    async def _run(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                events = await asyncio.to_thread(
                    self.bridge.work_items.events, self.cursor, 100
                )
                # Coalesce progress/heartbeats in one wake per work item per scan.
                changed = {}
                for event in events:
                    changed[event["work_id"]] = event
                for work_id, event in changed.items():
                    if self.bridge.channel.delivery == "push":
                        await asyncio.to_thread(
                            self.bridge.channel.emit,
                            "work_changed",
                            {"work_id": work_id, "revision": event["revision"]},
                            dedupe_key=f"work:{event['event_id']}",
                        )
                if events:
                    self.cursor = events[-1]["event_id"]
            except (sqlite3.Error, OSError, ValueError, QueueFull):
                logger.exception(
                    "Shared work wake unavailable; use tandem_work polling"
                )
                # Keep cursor so a later scan can observe committed state again.
