"""Shared task capability, recovery and manual submission through actual MCP/CLI."""

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
from omp_rpc.host_tools import HostToolContext
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.work_adapters import OmpWorkAdapter
from omp_tandem.work_notifications import WorkNotifications
from omp_tandem.work_supervisor import WorkSupervisor
from omp_tandem.work_workspace import WorkWorkspace


class WorkIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir()
        self.state = self.home / "state"
        self.git("init", "-q")
        (self.root / "module.txt").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.claude_bridge = self.bridge("claude")
        self.omp_bridge = self.bridge("omp")
        self.claude = Client(build_server(self.claude_bridge))
        self.omp = Client(build_server(self.omp_bridge))
        await self.claude.__aenter__()
        await self.omp.__aenter__()
        self.addAsyncCleanup(self.omp.__aexit__, None, None, None)
        self.addAsyncCleanup(self.claude.__aexit__, None, None, None)

    def bridge(self, actor, **kwargs):
        bridge = Bridge(
            self.state,
            "unused",
            "unused",
            project_root=self.root,
            channel_enabled=False,
            webhook_enabled=False,
            migrate_legacy=False,
            work_participant=actor,
            **kwargs,
        )
        self.addCleanup(bridge.shutdown)
        return bridge

    def git(self, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    async def call(self, client, request):
        result = await client.call_tool("tandem_work", {"request": request})
        return to_jsonable_python(result.data)

    async def mutate(self, client, work_id, action, **values):
        view = await self.call(client, {"action": "get", "work_id": work_id})
        return await self.call(
            client,
            {
                "action": action,
                "work_id": work_id,
                "expected_revision": view["revision"],
                "operation_id": str(uuid4()),
                **values,
            },
        )

    async def create(self, owner="claude", *, review_context_paths=None):
        view = await self.call(
            self.claude,
            {
                "action": "create",
                "expected_revision": 0,
                "operation_id": str(uuid4()),
                "plan": {
                    "title": "Shared integration fixture",
                    "goal": "Change one module",
                    "acceptance": [
                        "The recorded module change was checked by the other seat."
                    ],
                    "steps": [
                        {
                            "id": "change",
                            "title": "Change module",
                            "goal": "Update module",
                            "owner": owner,
                            "reviewer": "omp" if owner == "claude" else "claude",
                            "owned_files": ["module.txt"],
                            "review_context_paths": review_context_paths or [],
                            "depends_on": [],
                            "acceptance": ["Module contains the agreed output."],
                        }
                    ],
                },
            },
        )
        identifier = view["work_id"]
        await self.mutate(self.claude, identifier, "agree")
        await self.mutate(self.omp, identifier, "agree")
        return identifier

    async def context_attempt(self, paths):
        (self.root / "caller.py").write_text("pinned caller\n")
        self.git("add", "caller.py")
        self.git("commit", "-qm", "context base")
        identifier = await self.create(review_context_paths=paths)
        await self.mutate(self.claude, identifier, "claim", step_id="change")
        (self.root / "module.txt").write_text("submitted module\n")
        self.git("add", "module.txt")
        self.git("commit", "-qm", "module change")
        await self.mutate(
            self.claude,
            identifier,
            "submit",
            step_id="change",
            commit=self.git("rev-parse", "HEAD"),
            note="Changed module",
            evidence=["Saved committed module"],
        )
        store = self.omp_bridge.work_items
        view = store.authorize(
            identifier,
            budget_seconds=60,
            max_launches=2,
            max_cost_usd=1,
            allow_work=False,
            allow_shell=False,
            omp_model="provider/fixture",
        )
        attempt = store.reserve(
            identifier,
            "change",
            actor="omp",
            kind="review",
            owner_id="fixture",
        )
        workspace = WorkWorkspace(self.omp_bridge.scope).prepare(
            attempt, view["plan"], []
        )
        token = self.omp_bridge.scope.directory / ("token-" + attempt["attempt_id"])
        token.write_text(attempt["token"])
        token.chmod(0o600)
        return attempt, view["plan"], workspace, token

    async def test_declared_review_context_is_pinned_and_changed_paths_remain_source(
        self,
    ):
        attempt, plan, _, _ = await self.context_attempt(["caller.py", "module.txt"])
        (self.root / "caller.py").write_text("live caller changed\n")
        adapter = OmpWorkAdapter(self.omp_bridge)
        captured = adapter._pin_snapshot(attempt, plan)
        reviews = self.omp_bridge.reviews
        self.assertEqual(
            reviews.read(captured["review_id"], "selected", "caller.py")["content"],
            "pinned caller\n",
        )
        manifest = json.loads(
            reviews.read(captured["review_id"], limit=50000)["content"]
        )
        self.assertEqual(
            {item["path"]: item["role"] for item in manifest["files"]},
            {"caller.py": "context", "module.txt": "change"},
        )
        self.assertEqual(manifest["git"]["commit"], attempt["submission"]["commit"])
        for path in ("undeclared.py", "../caller.py", ".git/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                reviews.read(captured["review_id"], "selected", path)
        (self.root / "caller.py").unlink()
        (self.root / "caller.py").symlink_to(self.home / "outside")
        self.assertEqual(
            reviews.read(captured["review_id"], "selected", "caller.py")["content"],
            "pinned caller\n",
        )

    async def test_missing_declared_context_blocks_before_model_launch(self):
        attempt, plan, workspace, token = await self.context_attempt(["missing.py"])
        (self.root / "missing.py").write_text("live fallback must not be read")
        adapter = OmpWorkAdapter(self.omp_bridge)
        self.addCleanup(adapter.close)
        handle = adapter.start(attempt, plan, workspace, token_file=token)
        result = adapter.poll(handle)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["cost_usd"], 0)
        self.assertIsNone(handle.native_task_id)
        self.assertIsNone(handle.process)
        store = self.omp_bridge.work_items
        current = store.attempt(attempt["attempt_id"])
        self.assertIsNone(current.get("review_id"))
        self.assertEqual(current["review_stage"], "blocked")
        supervisor = WorkSupervisor(self.omp_bridge)
        supervisor._finish(attempt, plan, workspace, result)
        self.assertEqual(store.attempt(attempt["attempt_id"])["state"], "blocked")
        card = store.perform(
            {"action": "get", "work_id": attempt["work_id"]}, actor="operator"
        )
        self.assertEqual(
            card["steps"][0]["blockers"][-1]["reason"], "review_capture_failed"
        )
        self.assertFalse((handle.directory / "launch.json").exists())

    async def daemon(self, *args):
        return await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-I",
                "-m",
                "omp_tandem.work_daemon",
                "--project-root",
                str(self.root),
                "--state-dir",
                str(self.state),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    async def clarification_task(self, *, comparison=False, bind=True):
        identifier = await self.create()
        await self.mutate(self.claude, identifier, "claim", step_id="change")
        (self.root / "module.txt").write_text("after\n")
        self.git("add", "module.txt")
        self.git("commit", "-qm", "submitted")
        await self.mutate(
            self.claude,
            identifier,
            "submit",
            step_id="change",
            commit=self.git("rev-parse", "HEAD"),
            note="Author interpretation sentinel",
            evidence=["Submitted bytes"],
        )
        claim = await self.mutate(self.omp, identifier, "claim", step_id="change")
        attempt = claim["claim"]
        if comparison:
            await self.mutate(
                self.omp,
                identifier,
                "report",
                step_id="change",
                submission_id=attempt["submission"]["submission_id"],
                resolution="success",
                note="Independent assessment",
                evidence=["Saved source reviewed"],
            )
            await self.mutate(self.omp, identifier, "compare", step_id="change")
        task_id = str(uuid4())
        now = time.time()
        conversation_id = str(uuid4())
        lease = self.omp_bridge.tasks.lock(conversation_id)
        self.addCleanup(lease.close)
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) "
                "VALUES (?,?,?,?,?,'think','unused','Review saved source','running',?)",
                (task_id, conversation_id, now, now, str(self.root), now + 60),
            )
        self.omp_bridge.work_items.started(
            attempt["attempt_id"],
            workspace=str(self.root),
            native_task_id=task_id if bind else None,
        )
        task = self.omp_bridge.tasks.get(task_id)
        ask = next(
            tool
            for tool in self.omp_bridge.runtime.worker.worker_tools(task)
            if tool.name == "tandem_ask"
        )
        updates = []
        context = HostToolContext("ask-context", threading.Event(), updates.append)
        return identifier, task_id, ask, context, updates

    def answer_pending(self, task_id, answer):
        with closing(self.omp_bridge.tasks.connect()) as db:
            question = db.execute(
                "SELECT question_id FROM questions WHERE task_id=?", (task_id,)
            ).fetchone()
        return self.omp_bridge.reply(task_id, question["question_id"], answer)

    async def test_independent_registered_clarification_never_delivers_author_text(
        self,
    ):
        identifier, task_id, ask, context, updates = await self.clarification_task()
        sentinel = "AUTHOR-CLARIFICATION-SENTINEL"
        request = ask.parse_params(
            {
                "question": "Need the unchanged caller",
                "context": json.dumps({"requested_paths": ["caller.py"]}),
            }
        )
        # Baseline follows the real reply callback and returns the sentinel.
        with patch.object(
            self.omp_bridge.tasks.channel,
            "signal",
            side_effect=lambda: self.answer_pending(task_id, sentinel),
        ):
            delivered = ask.execute(request, context)
        self.assertNotIn(sentinel, delivered)
        result = json.loads(delivered)
        self.assertTrue(result["clarification_requires_new_snapshot"])
        self.assertEqual(result["requested_paths"], ["caller.py"])
        self.assertNotIn(sentinel, json.dumps(updates))
        with self.assertRaises(ToolError):
            await self.omp.call_tool(
                "tandem_reply",
                {
                    "task_id": task_id,
                    "question_id": result["question_id"],
                    "answer": sentinel,
                },
            )
        with self.assertRaises(ValueError):
            self.omp_bridge.reply(task_id, result["question_id"], sentinel)
        attempt = self.omp_bridge.work_items.native_attempt(task_id)
        self.assertEqual(attempt["review_stage"], "blocked")
        with self.assertRaises(ToolError):
            await self.mutate(self.omp, identifier, "compare", step_id="change")
        with self.assertRaises(ValueError):
            self.omp_bridge.interaction.submit_report(
                task_id,
                {
                    "outcome": "success",
                    "answer": "Complete",
                    "summary": "Complete",
                },
            )
        self.omp_bridge.interaction.submit_report(
            task_id,
            {
                "outcome": "blocked",
                "answer": delivered,
                "summary": "Missing caller",
                "blockers": ["Need a new capture with caller.py"],
            },
        )
        with closing(self.omp_bridge.tasks.connect()) as db:
            question = db.execute(
                "SELECT * FROM questions WHERE task_id=?", (task_id,)
            ).fetchone()
        self.assertIsNone(question["answer"])

    async def test_comparison_registered_clarification_delivers_permitted_answer(self):
        _, task_id, ask, context, _ = await self.clarification_task(comparison=True)
        with patch.object(
            self.omp_bridge.tasks.channel,
            "signal",
            side_effect=lambda: self.answer_pending(
                task_id, "permitted comparison text"
            ),
        ):
            delivered = ask.execute(
                ask.parse_params({"question": "Clarify rationale"}), context
            )
        self.assertEqual(json.loads(delivered)["answer"], "permitted comparison text")

    async def test_answer_delivery_rechecks_newly_bound_independent_stage(self):
        identifier, task_id, ask, context, updates = await self.clarification_task(
            bind=False
        )
        sentinel = "AUTHOR-DELIVERY-SENTINEL"
        store = self.omp_bridge.work_items
        attempt = next(
            item for item in store.active_attempts() if item["work_id"] == identifier
        )

        def answer_then_bind():
            self.answer_pending(task_id, sentinel)
            store.bind_native(attempt["attempt_id"], task_id)

        with patch.object(
            self.omp_bridge.tasks.channel, "signal", side_effect=answer_then_bind
        ):
            delivered = ask.execute(
                ask.parse_params({"question": "Need context"}), context
            )
        self.assertNotIn(sentinel, delivered)
        self.assertNotIn(sentinel, json.dumps(updates))
        self.assertTrue(json.loads(delivered)["clarification_requires_new_snapshot"])

    async def test_independent_reply_refuses_historical_duplicate_and_terminal_questions(
        self,
    ):
        _, task_id, _, _, _ = await self.clarification_task()
        sentinel = "AUTHOR-CLARIFICATION-SENTINEL"
        for state in ("pending", "answered", "cancelled", "expired"):
            question_id = str(uuid4())
            with closing(self.omp_bridge.tasks.connect()) as db, db:
                db.execute(
                    "INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        question_id,
                        task_id,
                        "Missing context",
                        "",
                        "[]",
                        time.time(),
                        time.time() + 60,
                        state,
                        sentinel if state == "answered" else None,
                        None,
                    ),
                )
            with self.subTest(state=state), self.assertRaises(ToolError):
                await self.omp.call_tool(
                    "tandem_reply",
                    {
                        "task_id": task_id,
                        "question_id": question_id,
                        "answer": sentinel,
                    },
                )
            with closing(self.omp_bridge.tasks.connect()) as db, db:
                db.execute(
                    "UPDATE questions SET state='cancelled' WHERE question_id=?",
                    (question_id,),
                )

    async def test_mcp_orphan_blocker_survives_replan_and_cli_show(self):
        identifier = await self.create()
        blocked = await self.mutate(
            self.omp,
            identifier,
            "block",
            step_id="change",
            note="Required fixture unavailable",
            condition="Restore fixture or justify inapplicability",
        )
        original = blocked["steps"][0]["blockers"][0]
        replacement = blocked["plan"]
        replacement["steps"][0]["id"] = "replacement"
        await self.mutate(self.claude, identifier, "propose", plan=replacement)
        await self.mutate(self.claude, identifier, "agree")
        await self.mutate(self.omp, identifier, "agree")
        shown = await self.daemon("show", identifier)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        card = json.loads(shown.stdout)
        orphan = card["blockers"][0]
        self.assertEqual(orphan["blocker_id"], original["blocker_id"])
        self.assertEqual(orphan["origin"], {"plan_revision": 1, "step_id": "change"})
        self.assertIn(original["blocker_id"], card["markdown"])
        self.assertIn(original["condition"], card["markdown"])
        self.assertEqual(card["steps"][0]["state"], "blocked")
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "claim", step_id="replacement")
        resolution = {
            "blocker_id": orphan["blocker_id"],
            "resolution": "not_applicable",
            "note": "Replacement contract no longer requires fixture",
            "evidence": ["Approved replacement criteria"],
        }
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "unblock", **resolution)
        await self.mutate(self.omp, identifier, "unblock", **resolution)
        claim = await self.mutate(
            self.claude, identifier, "claim", step_id="replacement"
        )
        self.assertEqual(claim["claim"]["step_id"], "replacement")
        self.assertEqual(claim["blockers"][0]["resolution_history"][0]["actor"], "omp")

    async def test_cli_authorize_show_and_model_alias_pin_selections(self):
        for alias in ("--model", "--omp-model"):
            identifier = await self.create()
            result = await self.daemon(
                "--claude-model",
                "claude-opus-4-6",
                alias,
                "provider/authorized",
                "authorize",
                identifier,
                "--budget-seconds",
                "60",
                "--max-launches",
                "2",
                "--max-cost-usd",
                "1",
                "--allow-work",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            grant = json.loads(result.stdout)["authorization"]
            self.assertEqual(grant["claude_model"], "claude-opus-4-6")
            self.assertEqual(grant["omp_model"], "provider/authorized")
            self.assertEqual(
                grant["model_provenance"], {"claude": "explicit", "omp": "explicit"}
            )
            shown = await self.daemon("show", identifier)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["authorization"], grant)
            for command in ("run", "start"):
                rejected = await self.daemon(
                    "--claude-model",
                    "sonnet",
                    alias,
                    "other/model",
                    command,
                    "--work-id",
                    identifier,
                    "--once",
                )
                self.assertNotEqual(rejected.returncode, 0)
            saved = await self.call(
                self.claude, {"action": "get", "work_id": identifier}
            )
            self.assertEqual(saved["authorization"], grant)
            self.assertIsNone(saved["steps"][0]["attempt"])

    async def test_cli_default_selection_and_invalid_identifiers(self):
        identifier = await self.create()
        arguments = (
            "authorize",
            identifier,
            "--budget-seconds",
            "60",
            "--max-launches",
            "2",
            "--max-cost-usd",
            "1",
        )
        for flag in ("--claude-model", "--omp-model"):
            rejected = await self.daemon(flag, "", *arguments)
            self.assertNotEqual(rejected.returncode, 0)
            saved = await self.call(
                self.claude, {"action": "get", "work_id": identifier}
            )
            self.assertIsNone(saved["authorization"])
        result = await self.daemon(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        grant = json.loads(result.stdout)["authorization"]
        self.assertEqual(grant["claude_model"], "sonnet")
        self.assertIsNone(grant["omp_model"])
        self.assertEqual(
            grant["model_provenance"], {"claude": "default", "omp": "default"}
        )
        shown = await self.daemon("show")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["items"][0]["authorization"], grant)

    async def test_native_launch_resolves_authorized_model_without_process_inheritance(
        self,
    ):
        for selected in ("provider/authorized", None):
            identifier = await self.create(owner="omp")
            store = self.omp_bridge.work_items
            view = store.authorize(
                identifier,
                budget_seconds=60,
                max_launches=2,
                max_cost_usd=1,
                allow_work=True,
                allow_tests=False,
                omp_model=selected,
            )
            attempt = store.reserve(
                identifier,
                "change",
                actor="omp",
                kind="implement",
                owner_id=str(uuid4()),
            )
            workspace = WorkWorkspace(self.omp_bridge.scope).prepare(
                attempt, view["plan"], []
            )
            token_file = self.omp_bridge.scope.directory / (
                "token-" + attempt["attempt_id"]
            )
            token_file.write_text(attempt["token"])
            token_file.chmod(0o600)
            # Explicit grants override process defaults; configured-default grants
            # use a model-less daemon bridge, as make_bridge constructs it.
            self.omp_bridge.runtime.model = "process/other" if selected else ""
            adapter = OmpWorkAdapter(self.omp_bridge)
            self.addCleanup(adapter.close)
            # Stop at the provider boundary; admission and ExecutionOptions resolution are real.
            with patch.object(self.omp_bridge.runtime.worker, "execute"):
                handle = adapter.start(
                    attempt, view["plan"], workspace, token_file=token_file
                )
                adapter._join(handle)
            launch = json.loads((handle.directory / "launch.json").read_text())
            self.assertEqual(launch["execution"]["effective"]["model"], selected)
            self.assertIsNone(launch["execution"]["actual"]["model"])
            self.assertEqual(launch["model_selection"]["omp_model"], selected)

    async def test_rejected_create_can_be_corrected_without_partial_work(self):
        steps = [
            {
                "id": identifier,
                "title": identifier,
                "goal": "Verify the combined result",
                "owner": "claude",
                "reviewer": "omp",
                "acceptance": ["Recorded evidence supports the result"],
            }
            for identifier in ("suite-duration", "final-green")
        ]
        request = {
            "action": "create",
            "expected_revision": 0,
            "operation_id": "correctable-create",
            "plan": {
                "title": "Integration checklist",
                "goal": "Include every required check",
                "acceptance": ["All checks contribute to the final result"],
                "steps": steps,
            },
        }
        with self.assertRaises(ToolError):
            await self.call(self.claude, request)
        steps[1]["depends_on"] = ["suite-duration"]
        for field in ("expected_revision", "operation_id"):
            with self.subTest(field=field), self.assertRaises(ToolError):
                await self.call(
                    self.claude,
                    {key: value for key, value in request.items() if key != field},
                )
        with self.assertRaises(ToolError):
            await self.call(self.claude, {**request, "expected_revision": 1})
        self.assertEqual(
            (await self.call(self.claude, {"action": "list"}))["items"], []
        )
        created = await self.call(self.claude, request)
        retried = await self.call(self.claude, request)
        self.assertEqual(retried["work_id"], created["work_id"])
        items = (await self.call(self.claude, {"action": "list"}))["items"]
        self.assertEqual([item["work_id"] for item in items], [created["work_id"]])

    async def test_invalid_manual_commit_does_not_poison_later_valid_submission(self):
        identifier = await self.create()
        await self.mutate(self.claude, identifier, "claim", step_id="change")
        with self.assertRaises(ToolError):
            await self.mutate(
                self.claude,
                identifier,
                "submit",
                step_id="change",
                commit="f" * 40,
                note="Not a valid source result",
                evidence=["Rejected example"],
            )
        (self.root / "module.txt").write_text("after\n")
        self.git("add", "module.txt")
        self.git("commit", "-qm", "implementation")
        commit = self.git("rev-parse", "HEAD")
        submitted = await self.mutate(
            self.claude,
            identifier,
            "submit",
            step_id="change",
            commit=commit,
            note="Updated declared module",
            evidence=["Actual module.txt reads after"],
        )
        self.assertEqual(submitted["steps"][0]["state"], "review")
        submission = submitted["steps"][0]["submission"]["submission_id"]
        with self.assertRaises(ToolError):
            await self.mutate(
                self.claude,
                identifier,
                "accept",
                step_id="change",
                submission_id=submission,
                note="Cannot self-approve",
                evidence=["Owner report"],
            )
        await self.mutate(self.omp, identifier, "claim", step_id="change")
        self.assertEqual((self.root / "module.txt").read_text(), "after\n")
        hidden = await self.mutate(self.omp, identifier, "get")
        self.assertIn("withheld", hidden["steps"][0]["submission"]["answer"])
        self.assertEqual(hidden.get("visibility"), "independent_stage")
        self.assertIn("commit", hidden["steps"][0]["submission"])
        await self.mutate(
            self.omp,
            identifier,
            "report",
            step_id="change",
            submission_id=submission,
            resolution="success",
            note="Independent read of the exact committed module",
            evidence=[f"{commit}:module.txt contains after"],
        )
        accepted = await self.mutate(
            self.omp,
            identifier,
            "accept",
            step_id="change",
            submission_id=submission,
            note="Read exact committed module",
            evidence=[f"{commit}:module.txt contains after"],
        )
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(accepted["result"]["commit"], commit)
        self.assertNotIn("token", json.dumps(accepted))

    async def test_operator_reconcile_uses_revision_after_stop_attestation(self):
        identifier = await self.create()
        await self.mutate(self.claude, identifier, "claim", step_id="change")
        self.claude_bridge.work_items.recover_owner("manual:claude")
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-I",
                "-m",
                "omp_tandem.work_daemon",
                "--project-root",
                str(self.root),
                "--state-dir",
                str(self.state),
                "reconcile",
                identifier,
                "change",
                "--resolution",
                "retry",
                "--note",
                "Manual claim never launched a process; inspected unchanged source",
                "--evidence",
                "No process was launched by this manual claim",
                "--confirm-stopped",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recovered = json.loads(result.stdout)
        self.assertEqual(recovered["steps"][0]["state"], "ready")
        claimed = await self.mutate(self.claude, identifier, "claim", step_id="change")
        self.assertEqual(claimed["steps"][0]["state"], "running")

    async def test_managed_capability_exposes_only_assigned_work_tool(self):
        identifier = await self.create()
        self.claude_bridge.work_items.authorize(
            identifier,
            budget_seconds=60,
            max_launches=2,
            max_cost_usd=1,
            allow_work=True,
            allow_tests=False,
        )
        attempt = self.claude_bridge.work_items.reserve(
            identifier,
            "change",
            actor="claude",
            kind="implement",
            owner_id=str(uuid4()),
        )
        token_file = self.home / "token"
        token_file.write_text(attempt["token"])
        token_file.chmod(0o600)
        managed = self.bridge("omp", work_token_file=token_file)
        async with Client(build_server(managed)) as client:
            tools = await client.list_tools()
            self.assertEqual(
                {tool.name for tool in tools}, {"tandem_work", "tandem_review_read"}
            )
            with self.assertRaises(ToolError):
                await client.call_tool("tandem_review_read", {"section": "manifest"})
            observed = await self.call(client, {"action": "get", "work_id": identifier})
            self.assertEqual(observed["participant"], "claude")
            with self.assertRaises(ToolError):
                await self.call(client, {"action": "list"})
            with self.assertRaises(ToolError):
                await self.call(
                    client,
                    {
                        "action": "agree",
                        "work_id": identifier,
                        "expected_revision": observed["revision"],
                        "operation_id": str(uuid4()),
                    },
                )
            with self.assertRaises(ToolError):
                await self.call(
                    client,
                    {"action": "get", "work_id": identifier, "actor": "operator"},
                )

    async def test_committed_peer_change_generates_wake_hint_without_dispatch(self):
        # No live-client wake guarantee is asserted; inspect actual persisted outbox.
        channel = self.claude_bridge.channel
        channel.confirmed = True
        notifications = WorkNotifications(self.claude_bridge)
        await notifications.start()
        try:
            identifier = await self.create()
            deadline = time.monotonic() + 3
            observed = []
            while time.monotonic() < deadline:
                observed = [
                    event
                    for event in channel.store.pending(channel.owner, 256)
                    if event["kind"] == "work_changed"
                    and event["payload"]["work_id"] == identifier
                ]
                if observed:
                    break
                await asyncio.sleep(0.03)
            self.assertTrue(
                observed, "No committed shared-work event reached the outbox"
            )
            self.assertEqual(self.claude_bridge.work_items.active_attempts(), [])
            view = await self.call(self.omp, {"action": "get", "work_id": identifier})
            self.assertGreaterEqual(
                view["revision"], observed[-1]["payload"]["revision"]
            )
            await self.call(self.claude, {"action": "get", "work_id": identifier})
            remaining = {
                event["event_id"] for event in channel.store.pending(channel.owner, 256)
            }
            self.assertFalse(
                remaining.intersection(event["event_id"] for event in observed)
            )
        finally:
            await notifications.close()
            channel.confirmed = False

    async def test_retired_claim_receipt_does_not_restore_write_capability(self):
        identifier = await self.create()
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        command = {
            "action": "claim",
            "work_id": identifier,
            "step_id": "change",
            "expected_revision": view["revision"],
            "operation_id": str(uuid4()),
        }
        first = await self.call(self.claude, command)
        await self.mutate(self.claude, identifier, "pause")
        repeated = await self.call(self.claude, command)
        self.assertEqual(repeated["claim"]["attempt_id"], first["claim"]["attempt_id"])
        self.assertNotIn("token", repeated["claim"])
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "heartbeat", step_id="change")

    async def test_removing_live_step_cannot_strand_its_recovery(self):
        identifier = await self.create()
        claimed = await self.mutate(self.claude, identifier, "claim", step_id="change")
        changed = claimed["plan"]
        changed["steps"][0]["id"] = "renamed"
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "propose", plan=changed)
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(view["steps"][0]["id"], "change")
        self.assertEqual(
            view["steps"][0]["attempt"]["attempt_id"], claimed["claim"]["attempt_id"]
        )

    async def test_supervisor_stop_remains_sticky_after_concurrent_revision(self):
        identifiers = [await self.create(), await self.create()]
        store = self.claude_bridge.work_items
        for identifier in identifiers:
            store.authorize(
                identifier,
                budget_seconds=60,
                max_launches=2,
                max_cost_usd=1,
                allow_work=True,
                allow_tests=False,
            )
        original = store.perform
        raced = False

        def concurrent_change(command, **kwargs):
            nonlocal raced
            if command["action"] == "pause" and not raced:
                raced = True
                original(
                    {
                        "action": "agree",
                        "work_id": command["work_id"],
                        "expected_revision": command["expected_revision"],
                        "operation_id": str(uuid4()),
                        "note": "Concurrent participant update",
                    },
                    actor="omp",
                )
            return original(command, **kwargs)

        supervisor = WorkSupervisor(self.claude_bridge)
        with patch.object(store, "perform", side_effect=concurrent_change):
            supervisor._pause_owned()
        self.assertTrue(raced)
        self.assertEqual(
            [
                original({"action": "get", "work_id": identifier}, actor="operator")[
                    "status"
                ]
                for identifier in identifiers
            ],
            ["paused", "paused"],
        )

    async def test_unscoped_work_cards_cannot_be_attached_to_another_project(self):
        import sqlite3

        from omp_tandem.workspace import resolve_scope

        foreign = self.home / "other-project"
        foreign.mkdir()
        scope = resolve_scope(self.state, foreign)
        with sqlite3.connect(scope.directory / "tasks.sqlite3") as db:
            db.execute("CREATE TABLE work_cards (work_id TEXT PRIMARY KEY, card TEXT)")
            db.execute("INSERT INTO work_cards VALUES ('old-task', '{}')")
        with self.assertRaises(ValueError):
            Bridge(
                self.state,
                "unused",
                "unused",
                project_root=foreign,
                channel_enabled=False,
                webhook_enabled=False,
                migrate_legacy=False,
            )


class AuthorizeCliFlagTests(unittest.TestCase):
    def test_allow_shell_flags_and_attempt_ceiling_parse(self):
        from omp_tandem.work_daemon import parser

        base = [
            "--project-root",
            ".",
            "authorize",
            "work",
            "--budget-seconds",
            "10",
            "--max-launches",
            "3",
            "--max-cost-usd",
            "6",
        ]
        args = parser().parse_args(
            [*base, "--allow-shell", "--max-attempt-cost-usd", "2.5"]
        )
        self.assertTrue(args.allow_shell)
        self.assertFalse(args.allow_tests)
        self.assertEqual(args.max_attempt_cost_usd, 2.5)
        legacy = parser().parse_args([*base, "--allow-tests", "--preview"])
        self.assertTrue(legacy.preview)
        self.assertTrue(legacy.allow_tests)
        self.assertFalse(legacy.allow_shell)
        self.assertIsNone(legacy.max_attempt_cost_usd)
        import argparse

        subparsers = next(
            action
            for action in parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        help_text = subparsers.choices["authorize"].format_help()
        self.assertIn("--allow-shell", help_text)
        self.assertIn("--max-attempt-cost-usd", help_text)
        self.assertIn("--preview", help_text)
        self.assertIn("Deprecated alias", help_text)
