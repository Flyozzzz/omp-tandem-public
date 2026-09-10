"""Review runs through the native host-tool boundary and durable owner reservations."""

import asyncio
import json
import os
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from omp_tandem import __file__ as package_file
from omp_tandem.review_runs import RUN_ACTIVE, ReviewRuns
from tests.helpers import RpcHarness
from tests.test_review_integration import REVIEW_PEER

RUN_PEER = REVIEW_PEER.replace(
    "scenario = task['task']['goal']",
    "scenario = Path(__file__).with_name('scenario').read_text()",
).replace(
    "        elif scenario == 'missing-report':",
    "        elif scenario == 'partial':\n"
    "            finish({'outcome': 'partial', 'summary': 'Incomplete evidence', 'answer': 'Only part of the saved material was assessed.'})\n"
    "        elif scenario == 'missing-report':",
)


class ReviewRunTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(RUN_PEER)
        (self.root / "scenario").write_text("snapshot-reader")
        (self.root / "sample.txt").write_text("saved bytes")
        self.runs = self.bridge.review_runs
        capture_command = self.runs._capture_command
        source = str(Path(package_file).parent.parent)

        def selected_capture(run):
            command = capture_command(run)
            code = (
                f"import sys; sys.path.insert(0, {source!r}); "
                "from omp_tandem.capture_worker import main; raise SystemExit(main())"
            )
            return [*command[:2], "-c", code, *command[4:]]

        source_patch = patch.object(
            self.runs, "_capture_command", side_effect=selected_capture
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def request(self, **values):
        return {
            "requirements": "Assess the saved sample",
            "paths": ["sample.txt"],
            "author_proposal": "PRIVATE PROPOSAL",
            **values,
        }

    async def begin(self, **values):
        return await asyncio.to_thread(
            self.runs.start, str(uuid4()), self.request(), **values
        )

    async def wait_run(self, run_id, waiting=False):
        async def wait():
            while True:
                state = await asyncio.to_thread(self.runs.state, run_id)
                if state not in RUN_ACTIVE or (waiting and state == "waiting_input"):
                    return await asyncio.to_thread(self.runs.view, run_id)
                await asyncio.sleep(0.03)

        return await asyncio.wait_for(wait(), timeout=8)

    async def wait_capture(self, run_id):
        async def wait():
            while True:
                run = await asyncio.to_thread(self.runs.view, run_id)
                if run["phase"] != "capture" or run["status"] not in RUN_ACTIVE:
                    return run
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(wait(), timeout=8)

    def controlled_capture(
        self, *, after_commit=False, during_publication=False, delay=None
    ):
        """Exercise real child capture/SQLite publication, never a mocked create result."""
        command = self.runs._capture_command
        marker = self.root / f"capture-{uuid4()}.entered"
        release = self.root / f"capture-{uuid4()}.release"
        code = """
import sys, time
from pathlib import Path
from omp_tandem.reviews import ReviewStore
from omp_tandem.capture_worker import main
marker, release, method, delay = sys.argv[1:5]
sys.argv = ['capture_worker', *sys.argv[5:]]
original = getattr(ReviewStore, method)
def controlled(self, *args, **kwargs):
    if method == '_check_reservation':
        if not kwargs.get('published'):
            return original(self, *args, **kwargs)
        result = None
    else:
        result = original(self, *args, **kwargs)
    Path(marker).write_text('ready')
    if delay != 'None':
        time.sleep(float(delay))
    else:
        until = time.monotonic() + 10
        while not Path(release).exists() and time.monotonic() < until:
            time.sleep(0.01)
    return original(self, *args, **kwargs) if method == '_check_reservation' else result
setattr(ReviewStore, method, controlled)
raise SystemExit(main())
"""
        # Preserve the selected package for both pre-fix and post-fix evidence,
        # even though the supervised child intentionally uses Python isolated mode.
        code = (
            f"import sys; sys.path.insert(0, {str(Path(package_file).parent.parent)!r})\n"
            + code
        )

        def wrapped(run):
            arguments = command(run)
            return [
                *arguments[:2],
                "-c",
                code,
                str(marker),
                str(release),
                "_check_reservation"
                if during_publication
                else "create"
                if after_commit
                else "_capture",
                str(delay),
                *arguments[4:],
            ]

        return (
            patch.object(self.runs, "_capture_command", side_effect=wrapped),
            marker,
            release,
        )

    async def wait_marker(self, marker):
        async def wait():
            while not marker.exists():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), timeout=5)

    def expire_budget(self, run_id):
        # Trigger the deadline only after the tested barrier is reached, not
        # while an arbitrarily slow CI machine is still importing the child.
        with self.runs.guard:
            self.runs.monotonic_deadlines[run_id] = time.monotonic()
            self.runs.wake.set()

    async def test_two_stages_saved_snapshot_author_gate_and_stale_applicability(self):
        run = await self.begin()
        await self.wait_capture(run["run_id"])
        (self.root / "sample.txt").write_text("later live bytes")
        result = await self.wait_run(run["run_id"])
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "success")
        )
        first, second = result["independent"], result["comparison"]
        self.assertEqual(
            json.loads(first["answer"]),
            {"saved": "saved bytes", "author_visible": False},
        )
        self.assertEqual(
            json.loads(second["answer"]),
            {"saved": "saved bytes", "author_visible": True},
        )
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["review"]["review_id"], second["review"]["review_id"])
        self.assertEqual(result["applicability"]["status"], "stale")
        self.assertEqual(
            self.runs.view(run["run_id"])["independent"]["answer"], first["answer"]
        )
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE review_run_id=?", (run["run_id"],)
                ).fetchone()[0],
                2,
            )

    async def test_long_complete_answers_survive_both_stages_and_idempotent_reuse(self):
        (self.root / "scenario").write_text("long-answer")
        key = str(uuid4())
        run = await asyncio.to_thread(self.runs.start, key, self.request())
        result = await self.wait_run(run["run_id"])
        for stage in ("independent", "comparison"):
            self.assertEqual(result[stage]["answer"], "雪界𝄞" * 6000)
            self.assertFalse(result[stage]["answer_truncated"])
        repeated = await asyncio.to_thread(
            self.runs.start, key, self.request(paths=["sample.txt", "sample.txt"])
        )
        self.assertEqual(repeated["run_id"], result["run_id"])
        with self.assertRaises(ValueError):
            self.runs.start(
                key, self.request(requirements="Different acceptance criteria")
            )
        self.assertEqual(
            repeated["comparison"]["task_id"], result["comparison"]["task_id"]
        )

    async def test_partial_blocked_and_unstructured_results_never_compare(self):
        for scenario in ("partial", "blocked", "missing-report"):
            with self.subTest(scenario=scenario):
                (self.root / "scenario").write_text(scenario)
                run = await self.begin()
                result = await self.wait_run(run["run_id"])
                self.assertIsNone(result["comparison"])
                self.assertIsNotNone(result["independent"])
                self.assertNotEqual(result["outcome"], "success")
                if scenario != "missing-report":
                    self.assertEqual(result["outcome"], scenario)
                    self.assertTrue(result["independent"]["answer"])

    async def test_no_author_means_independent_only(self):
        run = await asyncio.to_thread(
            self.runs.start, str(uuid4()), self.request(author_proposal="")
        )
        result = await self.wait_run(run["run_id"])
        self.assertEqual(result["outcome"], "success")
        self.assertIsNone(result["comparison"])

    async def test_reply_and_cancel_are_run_scoped_and_preserve_first_result(self):
        (self.root / "scenario").write_text("question")
        run = await self.begin()
        waiting = await self.wait_run(run["run_id"], waiting=True)
        with (
            patch.object(
                self.runs, "read_task", side_effect=AssertionError("full result read")
            ),
            patch.object(
                self.bridge.reviews,
                "assess",
                side_effect=AssertionError("live hashing"),
            ),
        ):
            self.assertEqual(self.runs.state(run["run_id"]), "waiting_input")
        replied = await asyncio.to_thread(
            self.runs.reply, run["run_id"], waiting["question"]["question_id"], "blue"
        )
        self.assertEqual(replied["replied_task_id"], waiting["task_id"])
        second = await self.wait_run(run["run_id"], waiting=True)
        self.assertEqual(second["phase"], "comparison")
        repeated = await asyncio.to_thread(
            self.runs.reply, run["run_id"], waiting["question"]["question_id"], "blue"
        )
        self.assertEqual(repeated["replied_task_id"], waiting["task_id"])
        self.assertEqual(
            repeated["question"]["question_id"], second["question"]["question_id"]
        )
        self.assertEqual(repeated["phase"], "comparison")
        cancelled = await asyncio.to_thread(self.runs.cancel, run["run_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["independent"]["answer"], "blue")
        with self.assertRaises(ValueError):
            self.runs.reply(run["run_id"], second["question"]["question_id"], "green")

    async def test_total_budget_includes_waiting_input_and_status_does_not_cancel(self):
        (self.root / "scenario").write_text("question")
        run = await self.begin(budget_seconds=30)
        waiting = await self.wait_run(run["run_id"], waiting=True)
        self.assertEqual(waiting["status"], "waiting_input")
        self.assertIn(self.runs.state(run["run_id"]), RUN_ACTIVE)
        self.assertIn(self.runs.view(run["run_id"])["status"], RUN_ACTIVE)
        began = time.monotonic()
        self.expire_budget(run["run_id"])
        result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["comparison"])
        self.assertLess(time.monotonic() - began, 2)

    async def test_startup_failure_is_final_and_reservation_cannot_be_replayed(self):
        key = str(uuid4())
        with patch.object(
            self.runs,
            "start_task",
            side_effect=ValueError("All four worker slots are occupied"),
        ):
            run = await asyncio.to_thread(self.runs.start, key, self.request())
            result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("four worker slots", result["error"])
        self.assertIsNone(result["independent"])
        self.assertIsNotNone(result["task_id"])
        repeat = self.runs.start(key, self.request())
        self.assertEqual(repeat["task_id"], result["task_id"])
        with self.assertRaises(ValueError):
            self.bridge.start(
                prompt="hold",
                mode="think",
                review_id=result["review_id"],
                review_stage="independent",
                reserved_task_id=result["task_id"],
                review_run_id=result["run_id"],
            )

    async def test_foreign_owner_can_observe_but_not_control_or_recover_live_run(self):
        (self.root / "scenario").write_text("hold")
        run = await self.begin()
        foreign = ReviewRuns(
            self.bridge.tasks,
            self.bridge.reviews,
            self.bridge.start,
            self.bridge.view,
            self.bridge.reply,
            self.bridge.cancel,
            str(uuid4()),
        )
        try:
            self.assertIn(foreign.state(run["run_id"]), RUN_ACTIVE)
            with self.assertRaises(ValueError):
                foreign.cancel(run["run_id"])
            with self.assertRaises(ValueError):
                foreign.reply(run["run_id"], str(uuid4()), "answer")
        finally:
            foreign.close()
        await asyncio.to_thread(self.runs.close)
        self.assertEqual(self.runs.view(run["run_id"])["status"], "interrupted")
        self.assertIsNone(self.runs.view(run["run_id"])["comparison"])

    async def test_dead_owner_reservation_is_interrupted_without_dispatch(self):
        run_id, owner, now = str(uuid4()), str(uuid4()), time.time()
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO review_runs (run_id,owner,request_key,payload_json,created,updated,deadline,status,phase,independent_task_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    owner,
                    "lost",
                    "{}",
                    now,
                    now,
                    now + 600,
                    "starting",
                    "independent",
                    str(uuid4()),
                ),
            )
        with patch.object(
            self.runs, "start_task", side_effect=AssertionError("replayed stage")
        ):
            self.assertEqual(self.runs.state(run_id), "interrupted")
            self.assertIsNone(self.runs.view(run_id)["independent"])

    async def test_missing_context_is_explicit_capture_failure_with_no_task(self):
        run = await asyncio.to_thread(
            self.runs.start, str(uuid4()), self.request(context_paths=["missing.txt"])
        )
        run = await self.wait_run(run["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("Required context is missing", run["error"])
        self.assertIsNone(run["task_id"])
        self.assertIsNone(run["independent"])

    async def test_clean_staged_context_returns_no_changes_without_dispatch(self):
        def git(*args):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
                timeout=15,
            )

        git("init", "-q")
        git("config", "user.email", "review@example.invalid")
        git("config", "user.name", "Review Run Regression")
        git("add", "sample.txt")
        git("commit", "-qm", "base")
        with patch.object(
            self.runs,
            "start_task",
            side_effect=AssertionError("context-only model call"),
        ):
            run = await asyncio.to_thread(
                self.runs.start,
                str(uuid4()),
                self.request(paths=None, source="staged", context_paths=["sample.txt"]),
            )
            run = await self.wait_run(run["run_id"])
        self.assertEqual(run["status"], "no_changes")
        self.assertIsNone(run["independent"])
        self.assertIsNone(run["task_id"])

    async def test_lost_dispatch_response_keeps_admitted_child_and_never_retries(self):
        (self.root / "scenario").write_text("hold")
        original = self.runs.start_task

        def lost_response(**kwargs):
            original(**kwargs)
            raise RuntimeError("Response lost after task admission")

        key = str(uuid4())
        with patch.object(self.runs, "start_task", side_effect=lost_response):
            run = await asyncio.to_thread(self.runs.start, key, self.request())
            result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("Response lost", result["error"])
        self.assertIsNotNone(result["independent"])
        self.assertIsNone(result["comparison"])
        repeated = self.runs.start(key, self.request())
        self.assertEqual(repeated["task_id"], result["task_id"])
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE review_run_id=?", (run["run_id"],)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT cancel_requested FROM tasks WHERE task_id=?",
                    (result["task_id"],),
                ).fetchone()[0],
                1,
            )

    async def test_cancel_serializes_with_inflight_admission_and_prevents_comparison(
        self,
    ):
        (self.root / "scenario").write_text("hold")
        entered, release = threading.Event(), threading.Event()
        original = self.runs.start_task

        def paused_dispatch(**kwargs):
            result = original(**kwargs)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test admission barrier expired")
            return result

        with patch.object(self.runs, "start_task", side_effect=paused_dispatch):
            run = await self.begin()
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                cancellation = asyncio.create_task(
                    asyncio.to_thread(self.runs.cancel, run["run_id"])
                )
                await asyncio.sleep(0.03)
                self.assertFalse(cancellation.done())
            finally:
                release.set()
            cancelled = await cancellation
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["comparison"])
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT cancel_requested FROM tasks WHERE task_id=?",
                    (cancelled["task_id"],),
                ).fetchone()[0],
                1,
            )

    async def test_controller_exit_interrupts_without_replay_and_keeps_request_identity(
        self,
    ):
        key = str(uuid4())
        with patch.object(self.runs, "_drive", return_value=None):
            run = await asyncio.to_thread(self.runs.start, key, self.request())
        result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(result["independent"])
        self.assertIsNone(result["comparison"])
        repeated = self.runs.start(key, self.request())
        self.assertEqual(repeated["run_id"], run["run_id"])
        with self.assertRaises(ValueError):
            self.runs.start(str(uuid4()), self.request())
        self.assertEqual(self.bridge.recent(), [])

    async def test_mcp_progress_is_compact_and_cancellation_uses_same_run(self):
        (self.root / "scenario").write_text("hold")
        started = await self.call(
            "tandem_review_run",
            action="start",
            request_key="compact-progress",
            request=self.request(),
            wait_seconds=0,
        )
        self.assertTrue(started["full_result_pending"])
        self.assertNotIn("independent", started)
        self.assertNotIn("comparison", started)
        repeated = await self.call(
            "tandem_review_run",
            action="start",
            request_key="compact-progress",
            request=self.request(),
            wait_seconds=0,
        )
        self.assertEqual(repeated["run_id"], started["run_id"])
        cancelled = await self.call(
            "tandem_review_run",
            action="cancel",
            run_id=started["run_id"],
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["comparison"])

    async def test_mcp_terminal_result_contains_both_complete_answers(self):
        (self.root / "scenario").write_text("long-answer")
        result = await self.call(
            "tandem_review_run",
            action="start",
            request_key="full-answers",
            request=self.request(),
            wait_seconds=25,
        )
        self.assertEqual(result["status"], "completed")
        for stage in ("independent", "comparison"):
            self.assertEqual(result[stage]["answer"], "雪界𝄞" * 6000)
            self.assertFalse(result[stage]["answer_truncated"])
        self.assertIsNone(result["usage"]["coordinator"])
        self.assertIsNone(result["usage"]["total_cost"])

    async def test_slow_successful_capture_obeys_total_budget_without_late_publication(
        self,
    ):
        control, marker, _ = self.controlled_capture()
        began = time.monotonic()
        with control:
            run = await self.begin(budget_seconds=30)
            self.assertLess(time.monotonic() - began, 0.8)
            self.assertIsNone(run["review_id"])
            await self.wait_marker(marker)
            began = time.monotonic()
            self.expire_budget(run["run_id"])
            result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertLess(time.monotonic() - began, 2)
        self.assertIsNone(result["review_id"])
        self.assertIsNone(result["independent"])
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0
            )

    async def test_slow_empty_capture_cannot_finish_as_no_changes_after_deadline(self):
        control, marker, _ = self.controlled_capture()
        for arguments in (
            ("init", "-q"),
            ("add", "sample.txt"),
            ("commit", "-qm", "base"),
        ):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.name=Review Regression",
                    "-c",
                    "user.email=review@example.invalid",
                    *arguments,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
                timeout=15,
            )
        with control:
            run = await asyncio.to_thread(
                self.runs.start,
                str(uuid4()),
                self.request(paths=None, source="staged"),
                budget_seconds=30,
            )
            await self.wait_marker(marker)
            self.expire_budget(run["run_id"])
            result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["review_id"])
        self.assertIsNone(result["independent"])

    async def test_blocked_capture_does_not_block_other_cancel_or_status(self):
        (self.root / "scenario").write_text("question")
        other = await self.begin()
        await self.wait_run(other["run_id"], waiting=True)
        control, marker, release = self.controlled_capture()
        with control:
            run = await self.begin()
            await self.wait_marker(marker)
            try:
                before = time.monotonic()
                state = await asyncio.wait_for(
                    asyncio.to_thread(self.runs.state, run["run_id"]), timeout=0.5
                )
                self.assertIn(state, RUN_ACTIVE)
                cancelled = await asyncio.wait_for(
                    asyncio.to_thread(self.runs.cancel, other["run_id"]), timeout=0.8
                )
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(
                    (await asyncio.to_thread(self.runs.cancel, run["run_id"]))[
                        "status"
                    ],
                    "cancelled",
                )
            finally:
                release.touch()
        result = await self.wait_run(run["run_id"])
        self.assertIsNone(result["review_id"])
        self.assertIsNone(result["independent"])

    async def test_cancel_during_publication_rolls_back_snapshot_and_keeps_reservation(
        self,
    ):
        control, marker, release = self.controlled_capture(during_publication=True)
        key = str(uuid4())
        with control:
            run = await asyncio.to_thread(self.runs.start, key, self.request())
            await self.wait_marker(marker)
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.runs.cancel, run["run_id"]), timeout=0.8
                )
            finally:
                release.touch()
        result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["review_id"])
        self.assertIsNone(result["independent"])
        repeated = await asyncio.to_thread(self.runs.start, key, self.request())
        self.assertEqual(repeated["run_id"], run["run_id"])
        with closing(self.bridge.tasks.connect()) as db:
            for table in ("reviews", "review_contents", "review_authors", "tasks"):
                self.assertEqual(
                    db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
                )

    async def test_publication_writer_defers_other_cancel_without_blocking_status_or_deadlines(
        self,
    ):
        """Real held SQLite publication, including MCP's channel acknowledgement."""
        (self.root / "scenario").write_text("question")
        other = await self.begin()
        await self.wait_run(other["run_id"], waiting=True)
        capture_control, capture_marker, capture_release = self.controlled_capture()
        with capture_control:
            expiring = await self.begin(budget_seconds=30)
            await self.wait_marker(capture_marker)
        publication_control, marker, release = self.controlled_capture(
            during_publication=True
        )
        with publication_control:
            publisher = await self.begin()
            await self.wait_marker(marker)
            process = self.runs.captures[publisher["run_id"]][0]
            try:
                # The child has inserted the snapshot and run mapping in a real,
                # uncommitted BEGIN IMMEDIATE transaction. Readers see neither.
                with closing(self.bridge.tasks.connect()) as db:
                    self.assertIsNone(
                        db.execute(
                            "SELECT review_id FROM review_runs WHERE run_id=?",
                            (publisher["run_id"],),
                        ).fetchone()[0]
                    )
                began = time.monotonic()
                cancelled = await asyncio.wait_for(
                    self.call(
                        "tandem_review_run", action="cancel", run_id=other["run_id"]
                    ),
                    timeout=0.8,
                )
                self.assertLess(time.monotonic() - began, 0.8)
                self.assertIn(cancelled["status"], RUN_ACTIVE)
                self.assertEqual(cancelled["stop_pending"]["status"], "cancelled")
                self.assertEqual(cancelled["next_action"], "wait")
                status = await asyncio.wait_for(
                    self.call(
                        "tandem_review_run",
                        action="status",
                        run_id=other["run_id"],
                        wait_seconds=0,
                    ),
                    timeout=0.5,
                )
                self.assertEqual(status["stop_pending"]["status"], "cancelled")
                self.expire_budget(expiring["run_id"])

                async def budget_observed():
                    while True:
                        progress = await asyncio.to_thread(
                            self.runs.view, expiring["run_id"]
                        )
                        if progress.get("stop_pending"):
                            return progress
                        await asyncio.sleep(0.01)

                expired = await asyncio.wait_for(budget_observed(), timeout=3)
                self.assertEqual(expired["stop_pending"]["status"], "failed")
                self.assertIsNone(process.poll(), "Unrelated publisher was killed")
                self.assertIn(
                    await asyncio.wait_for(
                        asyncio.to_thread(self.runs.state, publisher["run_id"]),
                        timeout=0.5,
                    ),
                    RUN_ACTIVE,
                )
                # Deadline supervision has killed only the expired capture.
                self.assertIsNone(expired["review_id"])
                self.assertIsNone(expired["independent"])
            finally:
                release.touch()
                capture_release.touch()
        cancelled = await self.wait_run(other["run_id"])
        expired = await self.wait_run(expiring["run_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(expired["status"], "failed")
        self.assertIsNone(cancelled["comparison"])
        self.assertIsNone(expired["independent"])
        healthy = await self.wait_run(publisher["run_id"], waiting=True)
        self.assertEqual(healthy["status"], "waiting_input")
        self.assertIsNotNone(healthy["review_id"])
        await asyncio.to_thread(self.runs.cancel, publisher["run_id"])
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE review_run_id=?",
                    (other["run_id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE review_run_id=?",
                    (expiring["run_id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT cancel_requested FROM tasks WHERE review_run_id=?",
                    (other["run_id"],),
                ).fetchone()[0],
                1,
            )

    async def test_cancel_after_capture_commit_keeps_snapshot_mapping_without_dispatch(
        self,
    ):
        control, marker, release = self.controlled_capture(after_commit=True)
        with control:
            run = await self.begin()
            await self.wait_marker(marker)
            try:
                with patch.object(
                    self.bridge.reviews,
                    "assess",
                    side_effect=AssertionError(
                        "capture control must not hash live files"
                    ),
                ):
                    progress = await asyncio.to_thread(self.runs.view, run["run_id"])
                    self.assertIn(progress["status"], RUN_ACTIVE)
                    result = await asyncio.wait_for(
                        asyncio.to_thread(self.runs.cancel, run["run_id"]), timeout=0.8
                    )
            finally:
                release.touch()
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNotNone(result["review_id"])
        self.assertIsNone(result["independent"])
        with closing(self.bridge.tasks.connect()) as db:
            saved = db.execute(
                "SELECT review_id, capture_id FROM review_runs WHERE run_id=?",
                (run["run_id"],),
            ).fetchone()
            self.assertEqual(saved["capture_id"], result["review_id"])
            self.assertEqual(saved["review_id"], result["review_id"])
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1
            )

    async def test_capture_retry_and_cancellation_never_replay_reserved_attempt(self):
        control, marker, release = self.controlled_capture()
        key = str(uuid4())
        with control:
            run = await asyncio.to_thread(self.runs.start, key, self.request())
            await self.wait_marker(marker)
            repeated = await asyncio.gather(
                *[
                    asyncio.to_thread(self.runs.start, key, self.request())
                    for _ in range(4)
                ]
            )
            self.assertEqual({item["run_id"] for item in repeated}, {run["run_id"]})
            process = self.runs.captures[run["run_id"]][0]
            try:
                await asyncio.to_thread(self.runs.cancel, run["run_id"])
            finally:
                release.touch()
            again = await asyncio.to_thread(self.runs.start, key, self.request())
        self.assertEqual(again["status"], "cancelled")
        self.assertIsNone(again["review_id"])
        await asyncio.to_thread(process.wait, timeout=3)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)
        self.assertEqual(self.bridge.recent(), [])

    async def test_capture_capacity_rejects_excess_without_rejecting_idempotent_retry(
        self,
    ):
        control, marker, release = self.controlled_capture()
        keys = [str(uuid4()) for _ in range(4)]
        with control:
            runs = [
                await asyncio.to_thread(self.runs.start, key, self.request())
                for key in keys
            ]
            await self.wait_marker(marker)
            try:
                with self.assertRaisesRegex(ValueError, "capture slots"):
                    await self.begin()
                repeated = await asyncio.to_thread(
                    self.runs.start, keys[0], self.request()
                )
                self.assertEqual(repeated["run_id"], runs[0]["run_id"])
                await asyncio.to_thread(self.runs.close)
            finally:
                release.touch()
        self.assertTrue(
            all(self.runs.state(run["run_id"]) == "interrupted" for run in runs)
        )
        self.assertEqual(self.runs.captures, {})

    async def test_lost_capture_response_keeps_committed_snapshot_and_never_recaptures(
        self,
    ):
        control, marker, release = self.controlled_capture(after_commit=True)
        key = str(uuid4())
        with control:
            run = await asyncio.to_thread(self.runs.start, key, self.request())
            await self.wait_marker(marker)
            self.runs.captures[run["run_id"]][0].kill()
            release.touch()
            result = await self.wait_run(run["run_id"])
            repeated = await asyncio.to_thread(self.runs.start, key, self.request())
        self.assertEqual(result["status"], "failed")
        self.assertIsNotNone(result["review_id"])
        self.assertEqual(repeated["review_id"], result["review_id"])
        self.assertEqual(repeated["run_id"], run["run_id"])
        self.assertIsNone(result["independent"])
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1
            )

    async def test_clock_rollback_does_not_extend_live_capture_budget(self):
        control, marker, _ = self.controlled_capture()
        with control:
            run = await self.begin(budget_seconds=30)
            await self.wait_marker(marker)
            with patch("omp_tandem.review_runs.time", wraps=time) as clock:
                clock.time.return_value = time.time() - 3600
                began = time.monotonic()
                self.expire_budget(run["run_id"])
                result = await self.wait_run(run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertLess(time.monotonic() - began, 2)
        self.assertIsNone(result["independent"])

    async def test_concurrent_close_waits_for_existing_capture_cleanup(self):
        control, marker, release = self.controlled_capture()
        with control:
            run = await self.begin()
            await self.wait_marker(marker)
        entered, finish_cleanup = threading.Event(), threading.Event()
        collect = self.runs._collect_capture

        def paused_collect(*args):
            entered.set()
            if not finish_cleanup.wait(5):
                raise RuntimeError("Cleanup barrier expired")
            return collect(*args)

        with patch.object(self.runs, "_collect_capture", side_effect=paused_collect):
            first = asyncio.create_task(asyncio.to_thread(self.runs.close))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                second = asyncio.create_task(asyncio.to_thread(self.runs.close))
                await asyncio.sleep(0.05)
                self.assertFalse(
                    second.done(), "close returned while cleanup still runs"
                )
            finally:
                release.touch()
                finish_cleanup.set()
                await first
                if second is not None:
                    await second
        self.assertEqual(self.runs.state(run["run_id"]), "interrupted")
        self.assertEqual(self.runs.captures, {})
