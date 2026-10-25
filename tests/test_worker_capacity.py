"""Kernel lane limits and turn-local admission, without provider calls."""

import asyncio
import multiprocessing
import subprocess
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from omp_tandem.bridge import Bridge
from omp_tandem.native_worker import _OwnedRpcClient
from omp_tandem.workspace import TaskAdmissionBusy, WorkerSlots, resolve_scope
from tests.helpers import RpcHarness


def _contend(base, root, purpose, barrier, release, results):
    pool = WorkerSlots(resolve_scope(Path(base), Path(root)).base)
    barrier.wait(timeout=20)
    try:
        lease = pool.acquire(purpose=purpose)
    except TaskAdmissionBusy:
        results.put((purpose, False))
    else:
        with lease:
            results.put((purpose, True))
            release.wait(timeout=20)


class NativeTerminationTests(unittest.TestCase):
    def test_best_effort_sdk_stop_is_not_termination_confirmation(self):
        process = SimpleNamespace(
            pid=424242,
            stdin=None,
            stdout=None,
            stderr=None,
            poll=Mock(return_value=None),
            wait=Mock(side_effect=subprocess.TimeoutExpired("owned fixture", 1)),
        )
        client = _OwnedRpcClient(executable="unused")
        client._process = process
        client._pgid = process.pid
        with patch(
            "omp_rpc.client.os.killpg", side_effect=PermissionError
        ) as signal_group:
            client.stop()
            self.assertFalse(client.termination_confirmed(timeout=0))
            process.poll.return_value = 0
            self.assertFalse(client.termination_confirmed(timeout=0))
            signal_group.side_effect = ProcessLookupError
            self.assertTrue(client.termination_confirmed(timeout=0))


class KernelCapacityTests(unittest.TestCase):
    def test_racing_processes_share_lane_limits_across_projects(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = (root / "a", root / "b")
            for project in projects:
                project.mkdir()
            purposes = ["work"] * 5 + ["coordination"] * 3
            barrier = context.Barrier(len(purposes) + 1)
            release, results = context.Event(), context.Queue()
            processes = [
                context.Process(
                    target=_contend,
                    args=(
                        str(root / "state"),
                        str(projects[index % 2]),
                        purpose,
                        barrier,
                        release,
                        results,
                    ),
                )
                for index, purpose in enumerate(purposes)
            ]
            try:
                for process in processes:
                    process.start()
                barrier.wait(timeout=20)
                observations = [results.get(timeout=20) for _ in processes]
                admitted = Counter(
                    purpose for purpose, success in observations if success
                )
                self.assertEqual(admitted, {"work": 3, "coordination": 1})
                pool = WorkerSlots(root / "state")
                for purpose in ("work", "coordination"):
                    with self.assertRaises(TaskAdmissionBusy):
                        pool.acquire(purpose=purpose)
            finally:
                release.set()
                for process in processes:
                    process.join(timeout=20)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                results.close()
                results.join_thread()
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            with ExitStack() as stack:
                for purpose in ("work", "work", "work", "coordination"):
                    stack.enter_context(pool.acquire(purpose=purpose))


class CoordinationAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bridge = Bridge(
            self.root / "state",
            "unused",
            None,
            project_root=self.root,
            channel_enabled=False,
            webhook_enabled=False,
        )
        self.addCleanup(self.bridge.shutdown)

    def test_coordination_cannot_admit_work_permissions_or_managed_assignment(self):
        requests = [
            {"prompt": "consult", "mode": "analyze"},
            {"prompt": "consult", "mode": "work"},
            {"contract": {"goal": "consult", "scope": {"owned_files": ["owned.txt"]}}},
            {"contract": {"goal": "consult", "requirements": {"requires_shell": True}}},
            {"contract": {"goal": "consult", "requirements": {"requires_write": True}}},
        ]
        for arguments in requests:
            for dry_run in (True, False):
                with (
                    self.subTest(arguments=arguments, dry_run=dry_run),
                    self.assertRaises(ValueError),
                ):
                    self.bridge.start(
                        **{"mode": "think", **arguments},
                        execution={"purpose": "coordination"},
                        dry_run=dry_run,
                    )
        with (
            patch.object(
                self.bridge.work_items,
                "attempt",
                return_value={"token": "bound", "actor": "omp"},
            ),
            patch.object(self.bridge.work_items, "authenticate"),
            self.assertRaises(ValueError),
        ):
            self.bridge.start(
                prompt="consult",
                mode="think",
                work_attempt_id="bound",
                execution={"purpose": "coordination"},
            )
        self.assertEqual(self.bridge.recent(), [])
        self.assertEqual(self.bridge.runtime.slots.capacity()["work"]["available"], 3)
        self.assertEqual(
            self.bridge.runtime.slots.capacity()["coordination"]["available"], 1
        )

    def test_cancellation_does_not_release_slot_before_owned_worker_returns(self):
        entered, release = threading.Event(), threading.Event()

        def execute(task_id):
            entered.set()
            release.wait(timeout=10)
            return True

        with patch.object(self.bridge.runtime.worker, "execute", side_effect=execute):
            task = self.bridge.start(
                prompt="consult", mode="think", execution={"purpose": "coordination"}
            )
            try:
                self.assertTrue(entered.wait(timeout=5))
                thread = self.bridge.runtime.threads[task["task_id"]]
                self.bridge.tasks.cancel(task["task_id"])
                with self.assertRaises(TaskAdmissionBusy):
                    self.bridge.runtime.slots.acquire(purpose="coordination")
            finally:
                release.set()
                thread.join(timeout=5)
            with self.bridge.runtime.slots.acquire(purpose="coordination"):
                pass

    def test_unconfirmed_native_teardown_keeps_capacity_quarantined(self):
        entered, release = threading.Event(), threading.Event()

        def execute(task_id):
            entered.set()
            release.wait(timeout=10)
            return False

        with patch.object(self.bridge.runtime.worker, "execute", side_effect=execute):
            task = self.bridge.start(
                prompt="consult", mode="think", execution={"purpose": "coordination"}
            )
            self.assertTrue(entered.wait(timeout=5))
            thread = self.bridge.runtime.threads[task["task_id"]]
            release.set()
            thread.join(timeout=5)
        with self.assertRaises(TaskAdmissionBusy):
            self.bridge.runtime.slots.acquire(purpose="coordination")
        # Test-owner disposal, not a runtime retry or claimed termination.
        for handles in self.bridge.runtime.unsettled_leases.values():
            for handle in handles:
                handle.close()


class WaitingWorkerCapacityTests(RpcHarness):
    async def test_question_waiters_leave_consultation_lane_and_followups_do_not_inherit_it(
        self,
    ):
        waiting = []
        for _ in range(3):
            job = await self.start(timeout_seconds=60)
            result = await self.result(job["task_id"])
            self.assertEqual(result["status"], "waiting_input", result)
            waiting.append(job)
        with self.assertRaises(TaskAdmissionBusy):
            self.bridge.start(prompt="paragraph", mode="think")
        consultation = await self.start(
            "paragraph", execution={"purpose": "coordination"}
        )
        result = await self.result(consultation["task_id"])
        self.assertEqual(result["status"], "completed", result)
        with self.assertRaises(TaskAdmissionBusy):
            self.bridge.start(
                prompt="paragraph", conversation_id=consultation["conversation_id"]
            )
        preflight = self.bridge.start(
            prompt="paragraph",
            conversation_id=consultation["conversation_id"],
            dry_run=True,
        )
        self.assertEqual(preflight["execution"]["effective"]["purpose"], "work")
        self.assertEqual(preflight["capacity"]["work"]["available"], 0)
        for job in waiting:
            await asyncio.to_thread(self.bridge.tasks.cancel, job["task_id"])
