"""Shared task capability, recovery and manual submission through actual MCP/CLI."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, suppress
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
from omp_rpc.host_tools import HostToolContext
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.events import QueueFull
from omp_tandem.work_adapters import OmpWorkAdapter
from omp_tandem.work_notifications import WorkNotifications
from omp_tandem.work_supervisor import WorkSupervisor
from omp_tandem.work_workspace import WorkWorkspace
from tests.helpers import make_peer


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
        self.claude_server = build_server(self.claude_bridge)
        self.claude = Client(self.claude_server)
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

    async def call(self, client, request, **presentation):
        result = await client.call_tool(
            "tandem_work", {"request": request, "view": "full", **presentation}
        )
        return to_jsonable_python(result.data)

    async def revise(self, client, work_id, new_plan):
        """Propose a differing plan; the operator activates it through the CLI."""
        proposed = await self.mutate(client, work_id, "propose", plan=new_plan)
        if not proposed.get("proposal"):
            return proposed
        activated = await self.daemon(
            "transition",
            work_id,
            "activate",
            "--proposal",
            proposed["proposal"]["proposal_id"],
            "--expected-revision",
            str(proposed["revision"]),
            "--note",
            "Operator activates the negotiated revision",
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        return await self.call(client, {"action": "get", "work_id": work_id})

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

    async def test_public_default_summary_and_native_wait_observe_committed_change(
        self,
    ):
        identifier = await self.create(owner="omp")
        response = await self.omp.call_tool(
            "tandem_work", {"request": {"action": "get", "work_id": identifier}}
        )
        summary = to_jsonable_python(response.data)
        self.assertNotIn("plan", summary)
        self.assertNotIn("markdown", summary)
        self.assertEqual([step["id"] for step in summary["steps"]], ["change"])
        worker = self.omp_bridge.runtime.worker
        tool = next(
            item
            for item in worker.worker_tools({"task_id": "wait-fixture"})
            if item.name == "tandem_work"
        )
        request = tool.parse_params(
            {"request": {"action": "get", "work_id": identifier}, "wait_seconds": 1}
        )
        mutation = {
            "action": "agree",
            "work_id": identifier,
            "expected_revision": summary["revision"],
            "operation_id": str(uuid4()),
        }
        with patch(
            "omp_tandem.native_worker.time.sleep",
            side_effect=lambda _: self.omp_bridge.work_items.perform(
                mutation, actor="claude"
            ),
        ):
            observed = json.loads(
                tool.execute(
                    request, HostToolContext("wait", threading.Event(), lambda _: None)
                )
            )
        self.assertEqual(observed["revision"], summary["revision"] + 1)
        self.assertNotIn("plan", observed)
        self.assertEqual(observed["steps"][0]["state"], "ready")

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

    async def test_outer_nested_repository_handover_cli_mcp(self):
        outer = self.root
        child = outer / "child:repository"
        child.mkdir()
        self.git("-C", str(child), "init", "-q")
        (child / "module.txt").write_text("child base\n")
        self.git("-C", str(child), "add", "module.txt")
        self.git("-C", str(child), "commit", "-qm", "child initial")
        child_base = self.git("-C", str(child), "rev-parse", "HEAD")
        original = await self.create()
        before = await self.call(self.claude, {"action": "get", "work_id": original})
        known_plan = json.loads(json.dumps(before["plan"]))
        known_plan["steps"][0]["owned_files"] = ["child:repository/new/module.txt"]
        with self.assertRaises(ToolError) as early:
            await self.call(
                self.claude,
                {
                    "action": "create",
                    "plan": known_plan,
                    "expected_revision": 0,
                    "operation_id": str(uuid4()),
                },
            )
        self.assertIn(str(child), str(early.exception))
        self.assertIn(str(outer), str(early.exception))
        self.assertEqual(
            len(
                self.claude_bridge.work_items.perform(
                    {"action": "list"}, actor="operator"
                )["items"]
            ),
            1,
        )
        claimed = await self.mutate(self.claude, original, "claim", step_id="change")
        attempt_id = claimed["claim"]["attempt_id"]
        source = claimed["claim"]["source_commit"]
        self.assertNotEqual(source, child_base)
        with self.assertRaises(ToolError) as failed_submit:
            await self.mutate(
                self.claude,
                original,
                "submit",
                step_id="change",
                commit=child_base,
                note="Work was done in the child",
                evidence=["Child Git commit"],
            )
        for expected in ("Submitted commit " + child_base, str(outer), "separate card"):
            self.assertIn(expected, str(failed_submit.exception))
        saved = self.claude_bridge.work_items.attempt(attempt_id)
        self.assertIsNone(saved["submission_intent"])
        self.assertEqual(saved["source_commit"], source)
        self.assertEqual(
            (await self.call(self.claude, {"action": "get", "work_id": original}))[
                "revision"
            ],
            claimed["revision"],
        )
        await self.mutate(self.claude, original, "pause")
        disposed = await self.daemon(
            "reconcile",
            original,
            "change",
            "--resolution",
            "abandon",
            "--confirm-stopped",
            "--note",
            "Manual execution stopped; child result retained",
            "--evidence",
            "Inspected effects; no unresolved work in outer repository",
        )
        self.assertEqual(disposed.returncode, 0, disposed.stderr)
        old_attempt = self.claude_bridge.work_items.attempt(attempt_id)
        old_history = self.claude_bridge.work_items.perform(
            {"action": "history", "work_id": original}, actor="operator"
        )["events"]
        child_bridges = [
            Bridge(
                self.state,
                "unused",
                "unused",
                project_root=child,
                channel_enabled=False,
                webhook_enabled=False,
                migrate_legacy=False,
                work_participant=actor,
            )
            for actor in ("claude", "omp")
        ]
        for bridge in child_bridges:
            self.addCleanup(bridge.shutdown)
        async with (
            Client(build_server(child_bridges[0])) as author,
            Client(build_server(child_bridges[1])) as reviewer,
        ):
            replacement = await self.call(
                author,
                {
                    "action": "create",
                    "plan": before["plan"],
                    "expected_revision": 0,
                    "operation_id": str(uuid4()),
                },
            )
            successor = replacement["work_id"]
            self.assertEqual(replacement["agreements"], {})
            self.assertIsNone(replacement["authorization"])
            self.assertEqual(replacement["repository"]["project_root"], str(child))
            with self.assertRaises(ToolError):
                await self.call(author, {"action": "get", "work_id": original})
            current = await self.call(
                self.claude, {"action": "get", "work_id": original}
            )
            closure = await self.daemon(
                "cancel",
                original,
                "--disposition",
                "superseded",
                "--expected-revision",
                str(current["revision"]),
                "--operation-id",
                "scope-cancel",
                "--note",
                "Original card pinned the wrong repository",
                "--evidence",
                "Stopped and disposed original manual attempt",
                "--continuation-root",
                str(child),
                "--continuation-work-id",
                successor,
            )
            self.assertEqual(closure.returncode, 0, closure.stderr)
            closed = json.loads(closure.stdout)
            linked = await self.daemon(
                "link",
                successor,
                "--predecessor-root",
                str(outer),
                "--predecessor-work-id",
                original,
                "--expected-revision",
                str(replacement["revision"]),
                "--operation-id",
                "scope-link",
                "--note",
                "Independent continuation after mistaken outer card",
                "--evidence",
                "Operator recorded predecessor; no foreign read or transferred acceptance",
                project_root=child,
            )
            self.assertEqual(linked.returncode, 0, linked.stderr)
            self.assertEqual(json.loads(linked.stdout)["agreements"], {})
            self.assertEqual(
                json.loads(linked.stdout)["predecessors"][0]["reciprocal_link"],
                "unverified",
            )
            await self.mutate(author, successor, "agree")
            await self.mutate(reviewer, successor, "agree")
            child_claim = await self.mutate(
                author, successor, "claim", step_id="change"
            )
            self.assertEqual(child_claim["claim"]["source_commit"], child_base)
            (child / "module.txt").write_text("independently reviewed child output\n")
            self.git("-C", str(child), "add", "module.txt")
            self.git("-C", str(child), "commit", "-qm", "child implementation")
            commit = self.git("-C", str(child), "rev-parse", "HEAD")
            submission = await self.mutate(
                author,
                successor,
                "submit",
                step_id="change",
                commit=commit,
                note="Child implementation",
                evidence=["Exact child commit"],
            )
            submission_id = submission["steps"][0]["submission"]["submission_id"]
            await self.mutate(reviewer, successor, "claim", step_id="change")
            self.assertEqual(
                self.git("-C", str(child), "show", commit + ":module.txt"),
                "independently reviewed child output",
            )
            await self.mutate(
                reviewer,
                successor,
                "report",
                step_id="change",
                submission_id=submission_id,
                resolution="success",
                note="Read the exact child commit and checked agreed output",
                evidence=[commit + ":module.txt equals agreed output"],
            )
            accepted = await self.mutate(
                reviewer,
                successor,
                "accept",
                step_id="change",
                submission_id=submission_id,
                note="Independent child-scope acceptance",
                evidence=["Exact committed output inspected by distinct reviewer"],
            )
            self.assertEqual(accepted["status"], "completed")
            self.assertEqual(accepted["result"]["commit"], commit)
            self.assertEqual(accepted["steps"][0]["acceptance"]["actor"], "omp")
        final = await self.call(self.claude, {"action": "get", "work_id": original})
        self.assertEqual(final["status"], "cancelled")
        self.assertIsNone(final["result"])
        self.assertEqual(final["closure"], closed["closure"])
        self.assertEqual(self.claude_bridge.work_items.attempt(attempt_id), old_attempt)
        history = self.claude_bridge.work_items.perform(
            {"action": "history", "work_id": original}, actor="operator"
        )["events"]
        self.assertEqual(history[: len(old_history)], old_history)
        with self.assertRaises(ToolError):
            await self.mutate(self.omp, original, "agree")
        markdown = await self.daemon("show", original, "--format", "markdown")
        self.assertEqual(markdown.returncode, 0, markdown.stderr)
        self.assertIn(successor, markdown.stdout)
        child_markdown = await self.daemon(
            "show", successor, "--format", "markdown", project_root=child
        )
        self.assertEqual(child_markdown.returncode, 0, child_markdown.stderr)
        self.assertIn(original, child_markdown.stdout)
        print(
            "SCOPE_E2E "
            + json.dumps(
                {
                    "outer_root": str(outer),
                    "child_root": str(child),
                    "original_work_id": original,
                    "successor_work_id": successor,
                    "outer_source": source,
                    "rejected_child_commit": child_base,
                    "accepted_child_commit": commit,
                    "original_status": final["status"],
                    "successor_status": accepted["status"],
                    "early_refusal": str(early.exception),
                    "submit_refusal": str(failed_submit.exception),
                    "original_history_prefix_unchanged": len(old_history),
                    "original_attempt_unchanged": True,
                    "fresh_agreements": True,
                    "cross_scope_read_refused": True,
                    "reciprocal_link": "unverified",
                    "surface": "FastMCP clients + real operator CLI subprocesses + real Git",
                },
                sort_keys=True,
            )
        )

    async def cancel_negotiation(self, identifier, before):
        proposal_id = before["proposal"]["proposal_id"]
        transition = before.get("transition")
        request = (
            "cancel",
            identifier,
            "--disposition",
            "cancelled",
            "--expected-revision",
            str(before["revision"]),
            "--operation-id",
            "cancel-negotiation",
            "--note",
            "Close this card and its pending negotiation",
            "--evidence",
            "Operator inspected all execution dispositions",
        )
        cancelled = await self.daemon(*request)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        closed = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(closed["revision"], before["revision"] + 1)
        self.assertEqual(closed["status"], "cancelled")
        self.assertIsNone(closed["proposal"])
        self.assertIsNone(closed["transition"])
        self.assertEqual(closed["closure"]["withdrawn_proposal_id"], proposal_id)
        self.assertEqual(closed["closure"]["revision"], closed["revision"])
        self.assertFalse(any(action["allowed"] for action in closed["next_actions"]))
        if transition:
            archived = closed["transition_history"][-1]
            self.assertEqual(
                closed["transition_history"][:-1], before["transition_history"]
            )
            self.assertEqual(archived["transition_id"], transition["transition_id"])
            self.assertEqual(archived["proposal_id"], proposal_id)
            self.assertEqual(archived["phase"], "cancelled")
            self.assertEqual(
                archived["closure_revision"], closed["closure"]["revision"]
            )
            self.assertEqual(archived["inventory"], transition["inventory"])
        else:
            self.assertEqual(closed["transition_history"], before["transition_history"])
        summary = await self.call(
            self.claude, {"action": "get", "work_id": identifier}, view="summary"
        )
        self.assertIsNone(summary["proposal"])
        self.assertIsNone(summary["transition"])
        inspected = await self.daemon("transition", identifier, "inspect")
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        inspected = json.loads(inspected.stdout)
        self.assertIsNone(inspected["proposal"])
        self.assertIsNone(inspected["transition"])
        self.assertEqual(inspected["commands"], [])
        self.assertEqual(inspected["transition_history"], closed["transition_history"])
        shown = await self.daemon("show", identifier, "--format", "markdown")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Closure: cancelled", shown.stdout)
        self.assertNotIn("Pending proposal", shown.stdout)
        self.assertNotIn("phase ready", shown.stdout)
        self.assertNotIn("phase stopping", shown.stdout)
        if transition:
            self.assertIn(
                f"Transition {transition['transition_id']} cancelled", shown.stdout
            )
        replay = await self.daemon(*request)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replayed = json.loads(replay.stdout)
        self.assertEqual(replayed["revision"], closed["revision"])
        self.assertEqual(replayed["transition_history"], closed["transition_history"])
        self.assertEqual(replayed["replayed_operation"]["outcome"], closed["closure"])

    async def test_cancel_closes_pending_proposal_without_transition(self):
        identifier = await self.create()
        current = await self.call(self.claude, {"action": "get", "work_id": identifier})
        pending = await self.mutate(
            self.claude,
            identifier,
            "propose",
            plan={**current["plan"], "goal": "Negotiate a different result"},
        )
        await self.cancel_negotiation(identifier, pending)

    async def test_cancel_archives_fully_disposed_transition(self):
        identifier = await self.create()
        claimed = await self.mutate(self.claude, identifier, "claim", step_id="change")
        attempt_id = claimed["claim"]["attempt_id"]
        pending = await self.mutate(
            self.claude,
            identifier,
            "propose",
            plan={**claimed["plan"], "goal": "Negotiate a different result"},
        )
        begun = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            pending["proposal"]["proposal_id"],
            "--expected-revision",
            str(pending["revision"]),
            "--operation-id",
            "begin-negotiation",
            "--note",
            "Stop before renegotiating",
        )
        self.assertEqual(begun.returncode, 0, begun.stderr)
        stopping = json.loads(begun.stdout)
        refused = await self.daemon(
            "cancel",
            identifier,
            "--disposition",
            "cancelled",
            "--expected-revision",
            str(stopping["revision"]),
            "--operation-id",
            "undisposed-cancel",
            "--note",
            "Premature closure",
            "--evidence",
            "Inventory is not disposed",
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("cancel_requires_disposition", refused.stderr)
        unchanged = await self.call(
            self.claude, {"action": "get", "work_id": identifier}
        )
        self.assertEqual(unchanged["revision"], stopping["revision"])
        self.assertEqual(unchanged["transition"], stopping["transition"])
        resolved = await self.daemon(
            "transition",
            identifier,
            "resolve",
            "--transition",
            stopping["transition"]["transition_id"],
            "--attempt",
            attempt_id,
            "--confirm-stopped",
            "--expected-revision",
            str(stopping["revision"]),
            "--operation-id",
            "resolve-negotiation",
            "--note",
            "Manual execution stopped; effects inspected",
            "--evidence",
            "No outstanding effects or work to preserve",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        ready = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(ready["transition"]["phase"], "ready")
        saved_attempt = self.claude_bridge.work_items.attempt(attempt_id)
        await self.cancel_negotiation(identifier, ready)
        self.assertEqual(
            self.claude_bridge.work_items.attempt(attempt_id), saved_attempt
        )

    async def daemon(self, *args, project_root=None):
        return await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-I",
                "-m",
                "omp_tandem.work_daemon",
                "--project-root",
                str(project_root or self.root),
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

    async def test_result_identifies_manual_review_without_claiming_confinement(self):
        _, task_id, _, _, _ = await self.clarification_task()
        result = self.omp_bridge.view(task_id)
        attempt = result["execution"]["attempt"]
        self.assertEqual(attempt["mode"], "manual")
        self.assertEqual(attempt["declared_review_protocol"], "independent_first")
        self.assertEqual(attempt["disclosure_provenance"], "manual_disclosed")
        self.assertEqual(attempt["review_stage"], "independent")
        self.assertFalse(attempt["grant"]["valid"])
        self.assertNotIn("Author interpretation sentinel", json.dumps(result))

    async def test_result_identifies_managed_grant_and_revocation(self):
        identifier = await self.create(owner="omp")
        store = self.omp_bridge.work_items
        store.authorize(
            identifier,
            budget_seconds=60,
            max_launches=2,
            max_cost_usd=1,
            allow_work=True,
            allow_shell=False,
        )
        attempt = store.reserve(
            identifier,
            "change",
            actor="omp",
            kind="implement",
            owner_id="fixture-supervisor",
            autonomous=True,
        )
        task_id, conversation_id = str(uuid4()), str(uuid4())
        lease = self.omp_bridge.tasks.lock(conversation_id)
        self.addCleanup(lease.close)
        now = time.time()
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) VALUES (?,?,?,?,?,'work','unused','Managed fixture','running',?)",
                (task_id, conversation_id, now, now, str(self.root), now + 60),
            )
        store.started(
            attempt["attempt_id"], workspace=str(self.root), native_task_id=task_id
        )
        context = self.omp_bridge.view(task_id)["execution"]["attempt"]
        self.assertEqual(context["mode"], "managed")
        self.assertTrue(context["grant"]["present"])
        self.assertTrue(context["grant"]["valid"])
        store.revoke(identifier)
        context = self.omp_bridge.view(task_id)["execution"]["attempt"]
        self.assertFalse(context["grant"]["valid"])
        self.assertEqual(context["grant"]["reason"], "revoked")
        self.assertEqual(context["mode"], "managed")

    async def manual_native_tools(self):
        identifier = await self.create()
        _, submission_id = await self._submitted_change(identifier)
        task_id, conversation_id = str(uuid4()), str(uuid4())
        lease = self.omp_bridge.tasks.lock(conversation_id)
        self.addCleanup(lease.close)
        now = time.time()
        with closing(self.omp_bridge.tasks.connect()) as db, db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) "
                "VALUES (?,?,?,?,?,'think','unused','Manual review','running',?)",
                (task_id, conversation_id, now, now, str(self.root), now + 60),
            )
        tools = {
            tool.name: tool
            for tool in self.omp_bridge.runtime.worker.worker_tools(
                self.omp_bridge.tasks.get(task_id)
            )
        }
        context = HostToolContext("manual-review", threading.Event(), lambda _: None)

        def work(action, **values):
            revision = self.omp_bridge.work_items.progress(identifier)["revision"]
            tool = tools["tandem_work"]
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

        work("claim")
        return task_id, submission_id, tools, context, work

    async def test_native_manual_claim_clarification_closes_without_free_text_wait(
        self,
    ):
        task_id, submission_id, tools, context, work = await self.manual_native_tools()
        tool = tools["tandem_ask"]
        with patch.object(
            self.omp_bridge.tasks.channel,
            "signal",
            side_effect=AssertionError(
                "Independent clarification entered ordinary wait"
            ),
        ):
            response = json.loads(
                tool.execute(
                    tool.parse_params(
                        {
                            "question": "Need caller",
                            "context": '{"requested_paths":["caller.py"]}',
                        }
                    ),
                    context,
                )
            )
        self.assertTrue(response["clarification_requires_new_snapshot"])
        self.assertEqual(response["requested_paths"], ["caller.py"])
        with self.assertRaises(ValueError):
            self.omp_bridge.reply(task_id, response["question_id"], "AUTHOR-SENTINEL")
        with self.assertRaises(ValueError):
            work(
                "report",
                submission_id=submission_id,
                resolution="success",
                note="Cannot bypass new snapshot",
                evidence=["x"],
            )
        with self.assertRaises(ValueError):
            self.omp_bridge.interaction.submit_report(
                task_id, {"outcome": "success", "answer": "x", "summary": "x"}
            )
        self.assertIsNone(self.omp_bridge.work_items.native_attempt(task_id))
        self.assertEqual(
            self.omp_bridge.work_items.stage_attempt(task_id)["review_stage"], "blocked"
        )

    async def test_native_manual_artifact_disclosure_follows_exact_comparison(self):
        task_id, submission_id, tools, context, work = await self.manual_native_tools()
        artifact = self.claude_bridge.artifacts.publish(
            str(uuid4()), str(uuid4()), name="interpretation", content="AUTHOR-SENTINEL"
        )
        read = tools["tandem_read_artifact"]
        request = read.parse_params({"artifact_id": artifact["artifact_id"]})
        with self.assertRaisesRegex(ValueError, "withheld"):
            read.execute(request, context)
        self.assertFalse(self.omp_bridge.runtime.worker._comparison_open(task_id))
        work(
            "report",
            submission_id=submission_id,
            resolution="success",
            note="Independent committed source assessment",
            evidence=["Committed module"],
        )
        with self.assertRaisesRegex(ValueError, "withheld"):
            read.execute(request, context)
        work("compare", submission_id=submission_id)
        self.assertIn("AUTHOR-SENTINEL", read.execute(request, context))
        self.assertTrue(self.omp_bridge.runtime.worker._comparison_open(task_id))
        # A disclosure binding must not inject an execution/managed capability.
        self.assertIsNone(self.omp_bridge.work_items.native_attempt(task_id))
        self.assertFalse(
            self.omp_bridge.work_items.stage_attempt(task_id)["autonomous"]
        )

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
        await self.revise(self.claude, identifier, replacement)
        await self.mutate(self.claude, identifier, "agree")
        await self.mutate(self.omp, identifier, "agree")
        shown = await self.daemon("show", identifier, "--view", "full")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        card = json.loads(shown.stdout)
        orphan = card["blockers"][0]
        self.assertEqual(orphan["blocker_id"], original["blocker_id"])
        self.assertEqual(orphan["origin"], {"plan_revision": 1, "step_id": "change"})
        rendered = await self.daemon(
            "show", identifier, "--view", "full", "--format", "markdown"
        )
        self.assertIn(original["blocker_id"], rendered.stdout)
        self.assertIn(original["condition"], rendered.stdout)
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
            shown = await self.daemon("show", identifier, "--view", "full")
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
        shown = await self.daemon("show", "--view", "full")
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

    async def _submitted_change(
        self, identifier, text="after\n", message="implementation"
    ):
        await self.mutate(self.claude, identifier, "claim", step_id="change")
        (self.root / "module.txt").write_text(text)
        self.git("add", "module.txt")
        self.git("commit", "-qm", message)
        commit = self.git("rev-parse", "HEAD")
        submitted = await self.mutate(
            self.claude,
            identifier,
            "submit",
            step_id="change",
            commit=commit,
            note="Updated declared module",
            evidence=[f"module.txt reads {text.strip()}"],
        )
        return commit, submitted["steps"][0]["submission"]["submission_id"]

    async def _peer_task(self, bridge, contract):
        async with Client(build_server(bridge)) as client:
            started = to_jsonable_python(
                (
                    await client.call_tool(
                        "tandem_start",
                        {
                            "cwd": str(self.root),
                            "mode": "think",
                            "timeout_seconds": 20,
                            "contract": contract,
                        },
                    )
                ).data
            )
            for _ in range(6):
                result = to_jsonable_python(
                    (
                        await client.call_tool(
                            "tandem_result",
                            {
                                "task_id": started["task_id"],
                                "wait_seconds": 20,
                                "details": True,
                            },
                        )
                    ).data
                )
                if result["status"] not in ("starting", "running"):
                    return result
            raise AssertionError(f"peer task did not settle: {result['status']}")

    def _counts(self):
        with closing(self.claude_bridge.tasks.connect()) as db:
            return (
                db.execute("SELECT count(*) FROM tasks").fetchone()[0],
                db.execute("SELECT count(*) FROM work_attempts").fetchone()[0],
            )

    async def test_failed_native_reviewer_is_closed_by_authorized_host_without_execution(
        self,
    ):
        identifier = await self.create(owner="claude")
        commit, submission_id = await self._submitted_change(identifier)
        revision = (
            await self.call(self.claude, {"action": "get", "work_id": identifier})
        )["revision"]
        peer = Bridge(
            self.state,
            str(make_peer(self.home)),
            "unused",
            project_root=self.root,
            channel_enabled=False,
            webhook_enabled=False,
            migrate_legacy=False,
            work_participant="omp",
        )
        self.addCleanup(peer.shutdown)
        failed = await self._peer_task(
            peer,
            {
                "goal": "review-refusal",
                "context": json.dumps(
                    {"work_id": identifier, "step_id": "change", "revision": revision}
                ),
            },
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            failed["facts"]["failure"]["classification"], "provider_policy_refusal"
        )
        finding = next(
            (
                item
                for item in failed.get("provisional_artifacts", [])
                if item["name"] == "finding"
            ),
            None,
        )
        self.assertIsNotNone(finding, failed)
        preserved = json.loads(
            self.claude_bridge.artifacts.read(artifact_id=finding["artifact_id"])[
                "content"
            ]
        )
        self.assertFalse(preserved["claim_error"], preserved)
        originated = failed["execution"]["attempt"]["originated_claims"]
        self.assertEqual(len(originated), 1)
        self.assertEqual(originated[0]["settled"]["teardown_confirmed"], True)
        self.assertEqual(
            (originated[0]["kind"], originated[0]["state"]), ("review", "reserved")
        )
        attempt_id = originated[0]["attempt_id"]
        card = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(card["steps"][0]["attempt"]["attempt_id"], attempt_id)
        hint = next(
            item for item in card["next_actions"] if item["action"] == "recover"
        )
        self.assertEqual(
            (hint["allowed"], hint["blocked_reason"]),
            (False, "recovery_not_authorized"),
        )
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "recover", step_id="change")
        with self.assertRaises(ToolError):
            await self.mutate(self.omp, identifier, "recover", step_id="change")
        baseline = self._counts()
        handoff = await self.daemon(
            "successor",
            attempt_id,
            "--host",
            self.claude_bridge.channel.owner,
            "--principal",
            "claude",
            "--note",
            "Operator handoff after the provider refusal; closure on preserved evidence only",
        )
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        successor_id = json.loads(handoff.stdout)["successor"]["successor_id"]
        with self.assertRaises(ToolError):
            await self.mutate(self.omp, identifier, "recover", step_id="change")
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        command = {
            "action": "recover",
            "work_id": identifier,
            "step_id": "change",
            "expected_revision": view["revision"],
            "operation_id": str(uuid4()),
        }
        recovered = await self.call(self.claude, command)
        self.assertEqual(recovered["recovery"]["attempt_id"], attempt_id)
        self.assertTrue(recovered["recovery"]["successors"][0]["consumed"])
        self.assertIn("withheld", json.dumps(recovered["recovery"]["submission"]))
        self.assertNotIn("Updated declared module", json.dumps(recovered))
        replay = await self.call(self.claude, command)
        self.assertEqual(replay["revision"], recovered["revision"])
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "recover", step_id="change")
        stored = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(stored["revision"], recovered["revision"])
        with self.assertRaises(ToolError):
            await self.mutate(
                self.claude,
                identifier,
                "submit",
                step_id="change",
                commit=commit,
                note="Successor must not adopt output",
                evidence=["x"],
            )
        await self.mutate(
            self.claude,
            identifier,
            "report",
            step_id="change",
            submission_id=submission_id,
            resolution="success",
            note="Closure recorded from the preserved reviewer finding",
            evidence=[finding["artifact_id"]],
        )
        accepted = await self.mutate(
            self.claude,
            identifier,
            "accept",
            step_id="change",
            submission_id=submission_id,
            note="Accepted on the preserved finding; no re-execution",
            evidence=[finding["artifact_id"]],
        )
        self.assertEqual(accepted["status"], "completed")
        verdict = accepted["steps"][0]["acceptance"]
        self.assertEqual(verdict["actor"], "omp")
        self.assertEqual(
            (
                verdict["recorded_by"]["principal"],
                verdict["recorded_by"]["successor_id"],
            ),
            ("claude", successor_id),
        )
        self.assertEqual(self._counts(), baseline)
        self.assertNotIn("token", json.dumps(accepted))
        shown = await self.daemon("show", identifier, "--format", "markdown")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("claim_recovered", shown.stdout)
        self.assertIn("successor_authorized", shown.stdout)
        self.assertIn("linked tasks: 1", shown.stdout)

    async def test_apply_and_assess_record_observations_and_report_renders_history(
        self,
    ):
        identifier = await self.create(owner="claude")
        first_commit, first_submission = await self._submitted_change(identifier)
        await self.mutate(self.omp, identifier, "claim", step_id="change")
        await self.mutate(
            self.omp,
            identifier,
            "report",
            step_id="change",
            submission_id=first_submission,
            resolution="partial",
            note="Module text does not match the agreed output",
            evidence=[f"{first_commit}:module.txt"],
        )
        rejected = await self.mutate(
            self.omp,
            identifier,
            "reject",
            step_id="change",
            submission_id=first_submission,
            note="Change the text to the agreed value",
            evidence=[f"{first_commit}:module.txt"],
        )
        self.assertEqual(rejected["steps"][0]["state"], "ready")
        self.assertIsNotNone(rejected["steps"][0]["checkpoint"])
        self.assertIsNone(rejected["steps"][0]["submission"])
        commit, submission = await self._submitted_change(
            identifier, text="agreed\n", message="fix"
        )
        await self.mutate(self.omp, identifier, "claim", step_id="change")
        await self.mutate(
            self.omp,
            identifier,
            "report",
            step_id="change",
            submission_id=submission,
            resolution="success",
            note="Exact committed module matches",
            evidence=[f"{commit}:module.txt contains agreed"],
        )
        accepted = await self.mutate(
            self.omp,
            identifier,
            "accept",
            step_id="change",
            submission_id=submission,
            note="Accepted",
            evidence=[f"{commit}:module.txt contains agreed"],
        )
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(accepted["application"]["status"], "not_recorded")
        self.assertIn("not_recorded", accepted["next_action"])
        self.assertNotIn("not merged", accepted["next_action"])
        with patch(
            "omp_tandem.work_workspace.subprocess.run",
            side_effect=AssertionError("git"),
        ):
            summary = await self.call(
                self.claude, {"action": "get", "work_id": identifier}, view="summary"
            )
            await self.call(self.claude, {"action": "history", "work_id": identifier})
            self.assertEqual(
                self.claude_bridge.work_items.progress(identifier)["status"],
                "completed",
            )
        self.assertEqual(summary["application"]["status"], "not_recorded")
        head = self.git("rev-parse", "HEAD")
        assessed = await self.daemon("assess", identifier, "--expected-head", head)
        self.assertEqual(assessed.returncode, 0, assessed.stderr)
        observation = json.loads(assessed.stdout)
        self.assertEqual(
            (observation["relation"], observation["target_commit"]), ("equal", commit)
        )
        self.assertEqual(observation["recording"]["status"], "recorded")
        applied = await self.daemon("apply", identifier, "--expected-head", head)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = json.loads(applied.stdout)
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["recording"]["status"], "recorded")
        card = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(
            [record["kind"] for record in card["application"]["records"]],
            ["git_assessment", "apply_receipt"],
        )
        for record in card["application"]["records"]:
            self.assertEqual(record["final_commit"], commit)
            for key in ("actor", "method", "published"):
                self.assertNotIn(key, record)
        self.assertIn("apply_receipt", card["next_action"])
        shown = await self.daemon("show", identifier, "--format", "markdown")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        report = shown.stdout
        for expected in (
            "## Agreements",
            "- claude: plan revision 1",
            "Verdict: accepted by omp",
            "reject by omp",
            "accept by omp",
            "independent_report by omp",
            "## Application",
            "git_assessment",
            "apply_receipt",
            "## Runtime",
            "## Usage",
            "coverage: unknown",
            "Change the text to the agreed value",
        ):
            self.assertIn(expected, report)
        self.assertNotIn("```json", report)
        self.assertNotIn("token", report)

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

    async def test_wake_acknowledgment_status_tracks_observations(self):
        async def status():
            result = await self.claude.call_tool("tandem_channel", {"action": "status"})
            return to_jsonable_python(result.data)["wake_acknowledgment"]

        initial = await status()
        self.assertEqual(initial["outcome"], "not_attempted")
        self.assertIsNone(initial["work_id"])
        identifier = await self.create()
        observed = await self.call(
            self.claude, {"action": "get", "work_id": identifier}
        )
        zero = await status()
        self.assertEqual(zero["outcome"], "acknowledged_zero")
        self.assertEqual(zero["acknowledged"], 0)
        self.assertIsNone(zero["reason"])
        channel = self.claude_bridge.channel
        event = channel.emit(
            "work_changed",
            {"work_id": identifier, "revision": observed["revision"]},
        )
        await self.call(self.claude, {"action": "get", "work_id": identifier})
        acknowledged = await status()
        self.assertEqual(acknowledged["outcome"], "acknowledged")
        self.assertEqual(acknowledged["acknowledged"], 1)
        self.assertEqual(acknowledged["work_id"], identifier)
        self.assertEqual(acknowledged["revision"], observed["revision"])
        self.assertIsNotNone(channel.store.get(event["event_id"])["acknowledged_at"])
        diagnostic = await self.claude.call_tool("tandem_diagnose", {})
        self.assertEqual(
            to_jsonable_python(diagnostic.data)["channel"]["wake_acknowledgment"],
            acknowledged,
        )
        other = await self.create()
        self.assertNotEqual(identifier, other)
        self.assertEqual(
            self.claude_bridge.wake_acknowledgments[identifier], acknowledged
        )

    async def controlled_notifications(self):
        notifications = self.claude_server.binding.work_notifications
        await notifications.close()
        # Stop transport bookkeeping before enabling push; tests control both writers.
        pump = self.claude_bridge.channel.pump
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump
        # Tests choose scan boundaries; no background producer can race an assertion.
        self.claude_bridge.channel.confirmed = True
        self.addCleanup(setattr, self.claude_bridge.channel, "confirmed", False)
        return notifications

    async def test_committed_peer_change_generates_wake_hint_without_dispatch(self):
        """The original intermittent outbox/ack failure remains unexplained history.

        These explicit commit/scan/read boundaries specify the supported contract,
        not the cause of that historical failure or a live-client wake guarantee.
        """
        notifications = await self.controlled_notifications()
        identifier = await self.create()
        channel = self.claude_bridge.channel
        before = await self.call(self.claude, {"action": "get", "work_id": identifier})
        await notifications._scan()
        observed = channel.store.pending(channel.owner, 256)
        self.assertEqual(
            [(event["kind"], event["payload"]) for event in observed],
            [("work_changed", {"work_id": identifier, "revision": before["revision"]})],
        )
        self.assertEqual(self.claude_bridge.work_items.active_attempts(), [])
        self.assertEqual(
            to_jsonable_python((await self.claude.call_tool("tandem_list", {})).data),
            [],
        )
        view = await self.call(self.omp, {"action": "get", "work_id": identifier})
        self.assertEqual(view["revision"], before["revision"])
        # Another owner observing R cannot acknowledge this owner's hint.
        self.assertIsNone(channel.store.get(observed[0]["event_id"])["acknowledged_at"])
        after = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(after, before)
        self.assertEqual(channel.store.pending(channel.owner, 256), [])
        self.assertEqual(
            self.claude_bridge.wake_acknowledgments[identifier]["acknowledged"], 1
        )

    async def test_writer_lock_defers_ack_until_next_explicit_observation(self):
        identifier = await self.create()
        channel = self.claude_bridge.channel
        before = await self.call(self.claude, {"action": "get", "work_id": identifier})
        hints = [
            channel.emit("work_changed", {"work_id": identifier, "revision": revision})
            for revision in (before["revision"] - 1, before["revision"])
        ]
        locked, release = threading.Event(), threading.Event()

        def writer():
            with closing(sqlite3.connect(channel.store.db_path)) as db:
                db.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait()
                db.rollback()

        with ThreadPoolExecutor(max_workers=1) as executor:
            held = executor.submit(writer)
            try:
                self.assertTrue(await asyncio.to_thread(locked.wait, 5))
                # This is a deadlock watchdog, not scheduling or a padded retry.
                observed = await asyncio.wait_for(
                    self.call(self.claude, {"action": "get", "work_id": identifier}),
                    timeout=5,
                )
                self.assertEqual(observed, before)
                status = await self.claude.call_tool("tandem_channel", {})
                deferred = to_jsonable_python(status.data)["wake_acknowledgment"]
                self.assertEqual(deferred["outcome"], "deferred")
                self.assertEqual(deferred["reason"], "busy")
                self.assertEqual(deferred["acknowledged"], 0)
                self.assertEqual(deferred["revision"], before["revision"])
                self.assertEqual(deferred["work_id"], identifier)
                diagnostic = await self.claude.call_tool("tandem_diagnose", {})
                self.assertEqual(
                    to_jsonable_python(diagnostic.data)["channel"][
                        "wake_acknowledgment"
                    ],
                    deferred,
                )
                self.assertEqual(
                    {
                        event["event_id"]
                        for event in channel.store.pending(channel.owner)
                    },
                    {event["event_id"] for event in hints},
                )
            finally:
                release.set()
                await asyncio.to_thread(held.result)
        observed = await self.call(
            self.claude, {"action": "get", "work_id": identifier}
        )
        self.assertEqual(observed, before)
        acknowledged = self.claude_bridge.wake_acknowledgments[identifier]
        self.assertEqual(acknowledged["outcome"], "acknowledged")
        self.assertEqual(acknowledged["acknowledged"], 2)
        self.assertIsNone(acknowledged["reason"])
        self.assertEqual(channel.store.pending(channel.owner), [])

    async def test_late_duplicate_hint_never_grants_work_or_launches(self):
        notifications = await self.controlled_notifications()
        identifier = await self.create()
        channel = self.claude_bridge.channel
        before = await self.call(self.claude, {"action": "get", "work_id": identifier})
        head = self.claude_bridge.work_items.event_head()
        await notifications._scan()
        first = channel.store.pending(channel.owner)[0]
        payload = {"work_id": identifier, "revision": before["revision"]}
        self.assertEqual(first["payload"], payload)
        late = channel.emit("work_changed", payload)
        repeated = channel.emit("work_changed", payload, dedupe_key=first["dedupe_key"])
        self.assertEqual(repeated["event_id"], first["event_id"])
        self.assertNotEqual(late["event_id"], first["event_id"])
        after = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(after, before)
        self.assertEqual(
            self.claude_bridge.wake_acknowledgments[identifier]["acknowledged"], 2
        )
        self.assertEqual(channel.store.pending(channel.owner), [])
        # Replaying an acknowledged dedupe key does not resurrect it.
        channel.emit("work_changed", payload, dedupe_key=first["dedupe_key"])
        self.assertEqual(channel.store.pending(channel.owner), [])
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "heartbeat", step_id="change")
        self.assertEqual(self.claude_bridge.work_items.event_head(), head)
        self.assertEqual(self.claude_bridge.work_items.active_attempts(), [])
        self.assertIsNone(after["authorization"])
        self.assertEqual(
            to_jsonable_python((await self.claude.call_tool("tandem_list", {})).data),
            [],
        )

    async def test_failed_wake_emit_retains_cursor_and_reobserves_committed_events(
        self,
    ):
        notifications = await self.controlled_notifications()
        channel = self.claude_bridge.channel
        first, second = await self.create(), await self.create()
        cursor = notifications.cursor
        head = self.claude_bridge.work_items.event_head()
        emit = channel.emit

        def fail_second(kind, payload, **kwargs):
            if payload["work_id"] == second:
                raise QueueFull("Injected outbox failure")
            return emit(kind, payload, **kwargs)

        with (
            patch.object(channel, "emit", side_effect=fail_second),
            self.assertLogs("omp_tandem.work_notifications", level="ERROR"),
        ):
            await notifications._scan()
        self.assertEqual(notifications.cursor, cursor)
        partial = channel.store.pending(channel.owner)
        self.assertEqual([event["payload"]["work_id"] for event in partial], [first])
        # The next specified scan replays the first event idempotently and emits
        # the retained second event, then advances the original cursor.
        await notifications._scan()
        self.assertEqual(notifications.cursor, head)
        delivered = channel.store.pending(channel.owner)
        self.assertEqual(
            {event["payload"]["work_id"] for event in delivered}, {first, second}
        )
        self.assertEqual(
            [
                event["event_id"]
                for event in delivered
                if event["payload"]["work_id"] == first
            ],
            [partial[0]["event_id"]],
        )
        self.assertEqual(self.claude_bridge.work_items.event_head(), head)
        self.assertEqual(self.claude_bridge.work_items.active_attempts(), [])

    async def test_reconnect_starts_at_event_head_and_explicit_get_synchronizes(self):
        identifier = await self.create()
        before = await self.call(self.claude, {"action": "get", "work_id": identifier})
        changed = await self.mutate(self.omp, identifier, "pause")
        reopened = self.bridge("claude")
        notifications = WorkNotifications(reopened)
        await notifications.start()
        # Cancel before yielding to the scheduled loop; exercise scans explicitly.
        await notifications.close()
        head = reopened.work_items.event_head()
        self.assertEqual(notifications.cursor, head)
        self.assertEqual(
            reopened.channel_status()["wake_acknowledgment"]["outcome"],
            "not_attempted",
        )
        reopened.channel.confirmed = True
        await notifications._scan()
        self.assertEqual(reopened.channel.store.pending(reopened.channel.owner), [])
        observed = reopened.work({"action": "get", "work_id": identifier})
        self.assertEqual(observed["revision"], changed["revision"])
        self.assertGreater(observed["revision"], before["revision"])
        self.assertEqual(observed["status"], "paused")
        self.assertEqual(
            reopened.channel_status()["wake_acknowledgment"]["outcome"],
            "acknowledged_zero",
        )
        self.assertEqual(reopened.work_items.active_attempts(), [])
        self.assertEqual(reopened.work_items.event_head(), head)

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
        # Renaming a live step is negotiable; it cannot strand the attempt because
        # activation waits for every inventory attempt to be disposed.
        pending = await self.mutate(self.claude, identifier, "propose", plan=changed)
        self.assertIsNotNone(pending["proposal"])
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(view["steps"][0]["id"], "change")
        self.assertEqual(
            view["steps"][0]["attempt"]["attempt_id"], claimed["claim"]["attempt_id"]
        )
        begun = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            pending["proposal"]["proposal_id"],
            "--note",
            "renegotiate",
        )
        self.assertEqual(begun.returncode, 0, begun.stderr)
        transition_id = json.loads(begun.stdout)["transition"]["transition_id"]
        activate = await self.daemon(
            "transition",
            identifier,
            "activate",
            "--transition",
            transition_id,
            "--note",
            "x",
        )
        self.assertNotEqual(activate.returncode, 0)
        self.assertIn("inventory_not_disposed", activate.stderr)
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(view["steps"][0]["id"], "change")
        self.assertEqual(view["steps"][0]["state"], "recovery_required")
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "heartbeat", step_id="change")
        resolved = await self.daemon(
            "transition",
            identifier,
            "resolve",
            "--transition",
            transition_id,
            "--attempt",
            claimed["claim"]["attempt_id"],
            "--confirm-stopped",
            "--note",
            "Manual holder stopped; nothing committed",
            "--evidence",
            "operator inspected the checkout",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        activated = await self.daemon(
            "transition",
            identifier,
            "activate",
            "--transition",
            transition_id,
            "--note",
            "go",
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(view["steps"][0]["id"], "renamed")
        self.assertEqual(view["plan_revision"], 2)
        self.assertEqual(view["agreements"], {})
        self.assertIsNone(view["proposal"])

    async def test_plan_transition_end_to_end_through_mcp_and_operator_cli(self):
        """Scenario 1: a plan change with live attempts, safe stop and continuation.

        Two manual attempts run through the real MCP surface; the operator drives
        the transition through the documented CLI. Managed executors are covered at
        store level with the supervisor's own teardown primitives.
        """
        identifier = await self.create(owner="claude")
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        plan = view["plan"]
        plan["steps"].append(
            {
                "id": "docs",
                "title": "Document module",
                "goal": "Explain module",
                "owner": "omp",
                "reviewer": "claude",
                "owned_files": ["docs.txt"],
                "depends_on": [],
                "acceptance": ["Docs describe the module."],
            }
        )
        plan["steps"].append(
            {
                "id": "integration",
                "title": "Integrate",
                "goal": "Combine",
                "owner": "claude",
                "reviewer": "omp",
                "owned_files": [],
                "depends_on": ["change", "docs"],
                "acceptance": ["Both parts integrated."],
            }
        )
        await self.revise(self.claude, identifier, plan)
        await self.mutate(self.claude, identifier, "agree")
        await self.mutate(self.omp, identifier, "agree")
        view = await self.call(self.claude, {"action": "get", "work_id": identifier})
        first = await self.mutate(self.claude, identifier, "claim", step_id="change")
        second = await self.mutate(self.omp, identifier, "claim", step_id="docs")
        (self.root / "module.txt").write_text("half\n")
        self.git("add", "module.txt")
        self.git("commit", "-qm", "wip")
        saved = self.git("rev-parse", "HEAD")
        changed = json.loads(json.dumps(plan))
        changed["steps"][0]["goal"] = "Update module and its schema"
        pending = await self.mutate(self.omp, identifier, "propose", plan=changed)
        self.assertEqual(pending["plan_revision"], view["plan_revision"])
        self.assertEqual(
            {item["attempt_id"] for item in pending["proposal"]["preview"]["attempts"]},
            {first["claim"]["attempt_id"], second["claim"]["attempt_id"]},
        )
        # Execution continues under the current plan while the proposal is pending.
        await self.mutate(self.claude, identifier, "heartbeat", step_id="change")
        summary = to_jsonable_python(
            (
                await self.claude.call_tool(
                    "tandem_work", {"request": {"action": "get", "work_id": identifier}}
                )
            ).data
        )
        command = next(
            item["command"]
            for item in summary["next_actions"]
            if item["action"] == "transition" and "begin" in item["command"]
        )
        self.assertIn(identifier, command)
        self.assertIn(str(self.root), command)
        inspect = await self.daemon("transition", identifier, "inspect")
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        commands = json.loads(inspect.stdout)["commands"]
        self.assertEqual([" begin " in c["command"] for c in commands].count(True), 1)
        self.assertTrue(
            any(
                " withdraw " in c["command"]
                and f"--proposal {pending['proposal']['proposal_id']}" in c["command"]
                and "--expected-revision" in c["command"]
                for c in commands
            )
        )
        self.assertFalse(any(" activate " in c["command"] for c in commands))
        stale = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            "00000000-0000-4000-8000-000000000000",
            "--note",
            "retained command for another proposal",
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("proposal_mismatch", stale.stderr)
        moved = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            pending["proposal"]["proposal_id"],
            "--expected-revision",
            str(pending["revision"] - 1),
            "--note",
            "observed an older card",
        )
        self.assertNotEqual(moved.returncode, 0)
        self.assertIn("revision", moved.stderr)
        # A retained withdraw command for a proposal that has since been replaced
        # must not discard the current one.
        retained_withdraw = await self.daemon(
            "transition",
            identifier,
            "withdraw",
            "--proposal",
            "00000000-0000-4000-8000-000000000000",
            "--note",
            "withdraw the proposal I observed earlier",
        )
        self.assertNotEqual(retained_withdraw.returncode, 0)
        self.assertIn("proposal_mismatch", retained_withdraw.stderr)
        still = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(
            still["proposal"]["proposal_id"], pending["proposal"]["proposal_id"]
        )
        begun = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            pending["proposal"]["proposal_id"],
            "--operation-id",
            "begin-once",
            "--note",
            "renegotiate",
        )
        self.assertEqual(begun.returncode, 0, begun.stderr)
        transition_id = json.loads(begun.stdout)["transition"]["transition_id"]
        repeated = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            pending["proposal"]["proposal_id"],
            "--operation-id",
            "begin-once",
            "--note",
            "renegotiate",
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(
            json.loads(repeated.stdout)["transition"]["transition_id"], transition_id
        )
        self.assertTrue(json.loads(repeated.stdout)["replayed_operation"])
        # The same operation id with a different command is a conflict, never a
        # borrowed acknowledgement.
        different = await self.daemon(
            "transition",
            identifier,
            "begin",
            "--proposal",
            "00000000-0000-4000-8000-000000000000",
            "--operation-id",
            "begin-once",
            "--note",
            "a different command",
        )
        self.assertNotEqual(different.returncode, 0)
        self.assertIn("different command", different.stderr)
        for client, step in ((self.claude, "change"), (self.omp, "docs")):
            with self.assertRaises(ToolError):
                await self.mutate(client, identifier, "heartbeat", step_id=step)
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "claim", step_id="change")
        stalled = await self.daemon(
            "transition",
            identifier,
            "activate",
            "--transition",
            transition_id,
            "--note",
            "x",
        )
        self.assertNotEqual(stalled.returncode, 0)
        report = await self.daemon("show", identifier, "--format", "markdown")
        self.assertIn("awaiting stop evidence and operator disposition", report.stdout)
        # A disposition recorded against an older card observation is refused.
        current = await self.call(self.claude, {"action": "get", "work_id": identifier})
        stale_resolve = await self.daemon(
            "transition",
            identifier,
            "resolve",
            "--transition",
            transition_id,
            "--expected-revision",
            str(current["revision"] - 1),
            "--attempt",
            first["claim"]["attempt_id"],
            "--confirm-stopped",
            "--note",
            "observed an older card",
            "--evidence",
            "stale",
        )
        self.assertNotEqual(stale_resolve.returncode, 0)
        self.assertIn("revision", stale_resolve.stderr)
        resolved = await self.daemon(
            "transition",
            identifier,
            "resolve",
            "--transition",
            transition_id,
            "--attempt",
            first["claim"]["attempt_id"],
            "--confirm-stopped",
            "--saved-commit",
            saved,
            "--note",
            "Claude stopped editing; intermediate commit kept",
            "--evidence",
            "commit inspected",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        resolved = await self.daemon(
            "transition",
            identifier,
            "resolve",
            "--transition",
            transition_id,
            "--attempt",
            second["claim"]["attempt_id"],
            "--confirm-stopped",
            "--note",
            "OMP had not started editing",
            "--evidence",
            "checkout clean for docs.txt",
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        activated = await self.daemon(
            "transition",
            identifier,
            "activate",
            "--transition",
            transition_id,
            "--note",
            "go",
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        after = await self.call(self.claude, {"action": "get", "work_id": identifier})
        self.assertEqual(after["plan_revision"], view["plan_revision"] + 1)
        self.assertEqual(
            after["plan"]["steps"][0]["goal"], "Update module and its schema"
        )
        self.assertEqual(after["agreements"], {})
        self.assertEqual(after["steps"][0]["checkpoint"]["commit"], saved)
        self.assertEqual(
            after["steps"][0]["checkpoint"]["continuation"], "operator_approved"
        )
        with self.assertRaises(ToolError):
            await self.mutate(self.claude, identifier, "heartbeat", step_id="change")
        await self.mutate(self.claude, identifier, "agree")
        await self.mutate(self.omp, identifier, "agree")
        renewed = await self.mutate(self.claude, identifier, "claim", step_id="change")
        self.assertEqual(renewed["claim"]["checkpoint"]["commit"], saved)
        report = await self.daemon("show", identifier, "--format", "markdown")
        self.assertIn("operator_attested", report.stdout)
        self.assertIn("continuation for", report.stdout)

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
