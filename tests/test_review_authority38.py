"""Review-stage disclosure and admission regressions without provider calls."""

import asyncio
import json
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastmcp.exceptions import ToolError
from omp_rpc.host_tools import HostToolContext

from omp_tandem import __file__ as package_file
from tests import test_review_runs as run_fixtures
from tests import test_work_integration as work_fixtures
from tests.helpers import RpcHarness
from tests.test_review_integration import REVIEW_PEER

AUTHOR = "AUTHOR-INTERPRETATION-38-SENTINEL"
PEER = REVIEW_PEER.replace(
    "        elif scenario == 'missing-report':",
    "        elif scenario == 'packet':\n"
    "            finish({'outcome': 'success', 'summary': 'Packet observed', 'answer': json.dumps(task)})\n"
    "        elif scenario == 'partial':\n"
    "            finish({'outcome': 'partial', 'summary': 'Incomplete', 'answer': 'Missing evidence'})\n"
    "        elif scenario == 'missing-report':",
).replace(
    "        if response.get('expired'):",
    "        if response.get('clarification_requires_new_snapshot'):\n"
    "            finish({'outcome': 'blocked', 'summary': 'Recapture required', 'answer': json.dumps(response), 'blockers': [response['reason']]})\n"
    "        elif response.get('expired'):",
)


class AuthorityAdmissionTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(PEER)
        (self.root / "sample.txt").write_text("saved source")
        self.review = await self.call(
            "tandem_review",
            action="create",
            request={
                "requirements": "Assess saved source",
                "paths": ["sample.txt"],
                "author_proposal": AUTHOR,
            },
        )
        self.context = self.bridge.projects.publish(
            {
                "project_id": "authority38",
                "product_summary": AUTHOR,
                "rules": [
                    {
                        "id": "required",
                        "text": "Preserve exact evidence",
                        "source": "Declared product requirement",
                    }
                ],
            }
        )

    async def test_public_independent_context_rejected_without_dropping_policy(self):
        for delivery in ("capsule", "full"):
            with self.subTest(delivery=delivery), self.assertRaises(ToolError):
                await self.start(
                    prompt=None,
                    contract={
                        "goal": "packet",
                        "context_options": {"delivery": delivery},
                    },
                    review_id=self.review["review_id"],
                    project_context_id=self.context["context_id"],
                )
        self.assertEqual(await self.call("tandem_list"), [])
        ordinary = await self.start(
            "packet", project_context_id=self.context["context_id"]
        )
        packet = json.loads((await self.result(ordinary["task_id"]))["answer"])
        self.assertEqual(
            packet["project_context"]["context"]["product_summary"], AUTHOR
        )
        self.assertEqual(
            packet["project_context"]["context"]["rules"][0]["text"],
            "Preserve exact evidence",
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=ordinary["conversation_id"],
                prompt="packet",
                review_id=self.review["review_id"],
            )

    async def test_message_sink_refuses_independent_product_snapshot(self):
        first = await self.start("paragraph", review_id=self.review["review_id"])
        await self.result(first["task_id"])
        task = self.bridge.tasks.get(first["task_id"], refresh=False)
        snapshot = self.bridge.projects.get(self.context["context_id"])
        for run_id in (None, str(uuid4())):
            for delivery in ("capsule", "full"):
                with (
                    self.subTest(run=run_id, delivery=delivery),
                    self.assertRaises(ValueError),
                ):
                    self.bridge.runtime.messages.build(
                        {
                            **task,
                            "review_run_id": run_id,
                            "project_context_id": snapshot["context_id"],
                            "contract_json": json.dumps(
                                {
                                    "goal": "packet",
                                    "context_options": {"delivery": delivery},
                                }
                            ),
                        },
                        snapshot,
                    )

    async def test_comparison_rechecks_after_concurrent_context_required_turn(self):
        first = await self.start("paragraph", review_id=self.review["review_id"])
        await self.result(first["task_id"])
        with self.bridge.runtime.guard:
            thread = self.bridge.runtime.threads.get(first["task_id"])
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
        waiting, release = threading.Event(), threading.Event()
        original_lock = self.bridge.tasks.lock

        def delayed_lock(conversation_id):
            if not waiting.is_set():
                waiting.set()
                if not release.wait(10):
                    raise RuntimeError(
                        "Comparison interleaving fixture was not released"
                    )
            return original_lock(conversation_id)

        with patch.object(self.bridge.tasks, "lock", delayed_lock):
            pending = asyncio.create_task(
                self.call(
                    "tandem_continue",
                    conversation_id=first["conversation_id"],
                    prompt="snapshot-reader",
                    review_stage="comparison",
                )
            )
            try:
                self.assertTrue(await asyncio.to_thread(waiting.wait, 5))
                later = await self.call(
                    "tandem_continue",
                    conversation_id=first["conversation_id"],
                    prompt="question",
                    review_stage="independent",
                )
                result = await self.result(later["task_id"])
                self.assertEqual(result["outcome"], "blocked")
                with self.bridge.runtime.guard:
                    thread = self.bridge.runtime.threads.get(later["task_id"])
                if thread is not None:
                    await asyncio.to_thread(thread.join, 5)
            finally:
                release.set()
            with self.assertRaises(ToolError):
                await pending
        with closing(self.bridge.tasks.connect()) as db:
            comparisons = db.execute(
                "SELECT count(*) FROM tasks WHERE conversation_id=? AND review_stage='comparison'",
                (first["conversation_id"],),
            ).fetchone()[0]
        self.assertEqual(comparisons, 0)

    async def test_blocked_clarification_requires_recapture_before_comparison(self):
        first = await self.start("question", review_id=self.review["review_id"])
        result = await self.result(first["task_id"])
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "blocked")
        )
        self.assertTrue(
            json.loads(result["answer"])["clarification_requires_new_snapshot"]
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=first["conversation_id"],
                prompt="snapshot-reader",
                review_stage="comparison",
            )
        # Even a later success cannot erase this snapshot's recapture requirement.
        retry = await self.call(
            "tandem_continue",
            conversation_id=first["conversation_id"],
            prompt="paragraph",
        )
        await self.result(retry["task_id"])
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=first["conversation_id"],
                prompt="snapshot-reader",
                review_stage="comparison",
            )
        recaptured = await self.call(
            "tandem_review",
            action="create",
            request={
                "requirements": "Assess saved source with the newly declared requirements",
                "paths": ["sample.txt"],
                "author_proposal": AUTHOR,
            },
        )
        fresh = await self.start("snapshot-reader", review_id=recaptured["review_id"])
        await self.result(fresh["task_id"])
        comparison = await self.call(
            "tandem_continue",
            conversation_id=fresh["conversation_id"],
            prompt="snapshot-reader",
            review_stage="comparison",
        )
        self.assertTrue(
            json.loads((await self.result(comparison["task_id"]))["answer"])[
                "author_visible"
            ]
        )

    async def test_partial_and_legacy_incomplete_reports_do_not_authorize_comparison(
        self,
    ):
        for report in (
            "partial",
            "blocked",
            {"outcome": "success", "summary": "No deliverable"},
            {
                "outcome": "success",
                "summary": "Old unchecked claim",
                "answer": "Claim",
                "checks": [{"name": "missing", "result": "not_run"}],
            },
        ):
            with self.subTest(report=report):
                first = await self.start(
                    report if isinstance(report, str) else "paragraph",
                    review_id=self.review["review_id"],
                )
                await self.result(first["task_id"])
                if isinstance(report, dict):
                    with closing(self.bridge.tasks.connect()) as db, db:
                        db.execute(
                            "UPDATE tasks SET report_json=? WHERE task_id=?",
                            (json.dumps(report), first["task_id"]),
                        )
                with self.assertRaises(ToolError):
                    await self.call(
                        "tandem_continue",
                        conversation_id=first["conversation_id"],
                        prompt="snapshot-reader",
                        review_stage="comparison",
                    )

    async def test_successful_comparison_accepts_explicit_product_context(self):
        first = await self.start("paragraph", review_id=self.review["review_id"])
        await self.result(first["task_id"])
        compared = await self.call(
            "tandem_continue",
            conversation_id=first["conversation_id"],
            prompt="packet",
            review_stage="comparison",
            project_context_id=self.context["context_id"],
        )
        packet = json.loads((await self.result(compared["task_id"]))["answer"])
        self.assertEqual(
            packet["project_context"]["context"]["product_summary"], AUTHOR
        )
        self.assertEqual(packet["review"]["stage"], "comparison")


class AuthorityRunTests(RpcHarness):
    request = run_fixtures.ReviewRunTests.request
    begin = run_fixtures.ReviewRunTests.begin
    wait_run = run_fixtures.ReviewRunTests.wait_run

    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(run_fixtures.RUN_PEER)
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

    async def test_generic_continue_cannot_escape_review_run_reservation(self):
        (self.root / "scenario").write_text("question")
        run = await self.begin()
        result = await self.wait_run(run["run_id"])
        self.assertEqual(result["outcome"], "blocked")
        first = result["independent"]
        for stage in ("independent", "comparison"):
            with self.subTest(stage=stage), self.assertRaises(ToolError):
                await self.call(
                    "tandem_continue",
                    conversation_id=first["conversation_id"],
                    prompt="snapshot-reader",
                    review_stage=stage,
                )


class AuthorityWorkTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = work_fixtures.WorkIntegrationTests.asyncSetUp
    bridge = work_fixtures.WorkIntegrationTests.bridge
    git = work_fixtures.WorkIntegrationTests.git
    call = work_fixtures.WorkIntegrationTests.call
    mutate = work_fixtures.WorkIntegrationTests.mutate
    create = work_fixtures.WorkIntegrationTests.create
    clarification_task = work_fixtures.WorkIntegrationTests.clarification_task
    context_attempt = work_fixtures.WorkIntegrationTests.context_attempt

    def tool(self, task_id):
        task = self.omp_bridge.tasks.get(task_id, refresh=False)
        # Callback access follows the persisted record, not these forged fields.
        task = {**task, "review_id": None, "review_stage": "comparison"}
        return next(
            tool
            for tool in self.omp_bridge.runtime.worker.worker_tools(task)
            if tool.name == "tandem_work"
        )

    async def test_unbound_independent_native_callbacks_deny_author_work(self):
        identifier, task_id, _, context, _ = await self.clarification_task(bind=False)
        review = self.omp_bridge.reviews.create(
            {"requirements": "Assess saved module", "paths": ["module.txt"]}
        )
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "UPDATE tasks SET review_id=?,review_stage='independent' WHERE task_id=?",
                (review["review_id"], task_id),
            )
        visible = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertIn("Author interpretation sentinel", json.dumps(visible))
        for run_id in (None, str(uuid4())):
            with closing(self.omp_bridge.tasks.connect()) as db, db:
                db.execute(
                    "UPDATE tasks SET review_run_id=? WHERE task_id=?",
                    (run_id, task_id),
                )
            tool = self.tool(task_id)
            for action in ("list", "get", "history"):
                values = {
                    "action": action,
                    **({"work_id": identifier} if action != "list" else {}),
                }
                with (
                    self.subTest(run=run_id, action=action),
                    self.assertRaises(ValueError),
                ):
                    tool.execute(
                        tool.parse_params({"request": values, "view": "full"}), context
                    )

    async def test_bound_native_review_preserves_redacted_work_protocol(self):
        identifier, task_id, _, context, _ = await self.clarification_task()
        review = self.omp_bridge.reviews.create(
            {"requirements": "Assess saved module", "paths": ["module.txt"]}
        )
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "UPDATE tasks SET review_id=?,review_stage='independent' WHERE task_id=?",
                (review["review_id"], task_id),
            )
        tool = self.tool(task_id)

        def work(action, **values):
            revision = self.omp_bridge.work_items.progress(identifier)["revision"]
            return json.loads(
                tool.execute(
                    tool.parse_params(
                        {
                            "request": {
                                "action": action,
                                "work_id": identifier,
                                "step_id": "change",
                                "expected_revision": revision,
                                "operation_id": str(uuid4()),
                                **values,
                            },
                            "view": "full",
                        }
                    ),
                    context,
                )
            )

        independent = work("get")
        self.assertNotIn("Author interpretation sentinel", json.dumps(independent))
        attempt = self.omp_bridge.work_items.native_attempt(task_id)
        submission = attempt["submission"]["submission_id"]
        work(
            "report",
            submission_id=submission,
            resolution="success",
            note="Independent saved source assessment",
            evidence=["Saved module"],
        )
        work("compare", submission_id=submission)
        self.assertIn("Author interpretation sentinel", json.dumps(work("get")))

    async def test_managed_snapshot_callback_keeps_bound_heartbeat(self):
        attempt, _, workspace, _ = await self.context_attempt([])
        store = self.omp_bridge.work_items
        review = self.omp_bridge.reviews.create(
            {
                "requirements": "Assess saved module",
                "paths": ["module.txt"],
            }
        )
        task_id, conversation_id = str(uuid4()), str(uuid4())
        lease = self.omp_bridge.tasks.lock(conversation_id)
        self.addCleanup(lease.close)
        now = time.time()
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline,review_id,review_stage) "
                "VALUES (?,?,?,?,?,'think','unused','Managed review','running',?,?,'independent')",
                (
                    task_id,
                    conversation_id,
                    now,
                    now,
                    str(self.root),
                    now + 60,
                    review["review_id"],
                ),
            )
        store.started(
            attempt["attempt_id"], workspace=workspace["path"], native_task_id=task_id
        )
        tool = self.tool(task_id)
        context = HostToolContext("managed-review", threading.Event(), lambda _: None)
        self.assertTrue(store.native_attempt(task_id)["autonomous"])
        observed = json.loads(
            tool.execute(
                tool.parse_params(
                    {
                        "request": {"action": "get", "work_id": attempt["work_id"]},
                        "view": "full",
                    }
                ),
                context,
            )
        )
        heartbeat_started = time.time()
        self.assertNotIn("Changed module", json.dumps(observed))
        tool.execute(
            tool.parse_params(
                {
                    "request": {
                        "action": "heartbeat",
                        "work_id": attempt["work_id"],
                        "step_id": attempt["step_id"],
                        "expected_revision": observed["revision"],
                        "operation_id": str(uuid4()),
                    },
                    "view": "full",
                }
            ),
            context,
        )
        self.assertGreaterEqual(
            store.attempt(attempt["attempt_id"])["heartbeat_at"], heartbeat_started
        )
