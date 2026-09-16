"""One requested observation, not a chain of them: bounded waits and their metadata."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from omp_tandem.observation import MAX_WAIT_SECONDS, Observation, step_interval
from tests.helpers import RpcHarness


class ObservationHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_unknown_mode_is_refused_rather_than_read_as_bounded(self):
        with self.assertRaises(ValueError):
            Observation(5, "strict")
        self.assertTrue(Observation(5, "auto").follows_delivery)
        self.assertFalse(Observation(5, "bounded").follows_delivery)

    def test_the_ladder_slows_reads_as_the_wait_lengthens(self):
        self.assertEqual(
            [step_interval(elapsed) for elapsed in (0, 4.9, 5, 29, 30, 119, 120, 600)],
            [0.15, 0.15, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0],
        )
        # A twenty-minute wait reads the store hundreds of times, not thousands.
        elapsed, reads = 0.0, 0
        while elapsed < MAX_WAIT_SECONDS:
            elapsed += step_interval(elapsed)
            reads += 1
        self.assertLess(reads, 1000)

    async def test_only_a_long_request_backs_off(self):
        recorded = []

        async def capture(seconds):
            recorded.append(seconds)

        with patch("omp_tandem.observation.asyncio.sleep", capture):
            brisk = Observation(25)
            brisk._started -= 60
            brisk._deadline += 600
            await brisk.pause()
            patient = Observation(600)
            patient._started -= 60
            await patient.pause()
        self.assertEqual(recorded, [0.15, 1.0])

    def test_a_zero_wait_describes_no_wait(self):
        self.assertNotIn("wait", Observation(0).record({}, "terminal"))
        block = Observation(7, "bounded").record({}, "deadline_elapsed")["wait"]
        self.assertEqual(
            (block["mode"], block["requested_seconds"], block["effective_seconds"]),
            ("bounded", 7, 7),
        )
        self.assertEqual(block["reason"], "deadline_elapsed")
        self.assertLessEqual(block["observed_at"], time.time())


class BoundedWaitTests(RpcHarness):
    async def held(self):
        """A task that stays running, so a wait has something to wait for."""
        job = await self.start("hold", timeout_seconds=60)
        return job["task_id"]

    async def test_bounded_waiting_ignores_delivery_coverage(self):
        task_id = await self.held()
        with patch.object(self.bridge.channel, "can_await", return_value=True):
            started = time.monotonic()
            handed_back = await self.call(
                "tandem_result", task_id=task_id, wait_seconds=2
            )
            quick = time.monotonic() - started
            started = time.monotonic()
            waited = await self.call(
                "tandem_result", task_id=task_id, wait_seconds=2, wait_mode="bounded"
            )
            held = time.monotonic() - started
        self.assertLess(quick, 1)
        self.assertGreaterEqual(held, 2)
        self.assertEqual(handed_back["wait"]["reason"], "event_delivery_available")
        self.assertEqual(handed_back["next_action"], "await_event")
        self.assertEqual(waited["wait"]["reason"], "deadline_elapsed")
        # A caller that declined delivery is not handed back to it.
        self.assertEqual(waited["next_action"], "wait")
        self.assertEqual(waited["status"], "running")

    async def test_group_waiting_honours_the_same_choice(self):
        task_id = await self.held()
        with patch.object(self.bridge.channel, "can_await", return_value=True):
            started = time.monotonic()
            waited = await self.call(
                "tandem_wait", task_ids=[task_id], wait_seconds=2, wait_mode="bounded"
            )
            held = time.monotonic() - started
        self.assertGreaterEqual(held, 2)
        self.assertEqual(waited["wait"]["reason"], "deadline_elapsed")
        self.assertEqual(waited["pending"], [task_id])
        self.assertEqual(waited["next_action"], "wait")

    async def test_a_question_ends_a_long_bounded_wait_at_once(self):
        job = await self.start()
        started = time.monotonic()
        waiting = await self.call(
            "tandem_result",
            task_id=job["task_id"],
            wait_seconds=MAX_WAIT_SECONDS,
            wait_mode="bounded",
        )
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(waiting["status"], "waiting_input")
        self.assertEqual(waiting["wait"]["reason"], "question")

    async def test_an_unrequested_wait_reports_nothing_about_waiting(self):
        job = await self.start("paragraph")
        await self.result(job["task_id"])
        immediate = await self.call("tandem_result", task_id=job["task_id"])
        self.assertEqual(immediate["status"], "completed")
        self.assertNotIn("wait", immediate)

    async def test_an_unknown_mode_is_refused_at_the_surface(self):
        task_id = await self.held()
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_result", task_id=task_id, wait_seconds=1, wait_mode="strict"
            )


if __name__ == "__main__":
    unittest.main()
