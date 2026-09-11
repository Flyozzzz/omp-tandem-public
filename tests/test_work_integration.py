"""Shared task capability, recovery and manual submission through actual MCP/CLI."""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.work_notifications import WorkNotifications
from omp_tandem.work_supervisor import WorkSupervisor


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

    async def create(self):
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
                            "owner": "claude",
                            "reviewer": "omp",
                            "owned_files": ["module.txt"],
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
            self.assertEqual({tool.name for tool in tools}, {"tandem_work"})
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
