"""Project data boundaries exercised through real MCP calls and local RPC peers."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Root
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.workspace import WorkerSlots, resolve_scope
from tests.helpers import make_peer


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a, self.b, self.base = (
            self.root / "A",
            self.root / "B",
            self.root / "state",
        )
        self.a.mkdir()
        self.b.mkdir()

    def test_launch_environment_not_process_cwd_selects_persistent_data(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.a)}):
            with patch.object(Path, "cwd", return_value=self.b):
                first = Bridge(self.base, "unused", None)
            snapshot = first.projects.publish(
                {"project_id": "product", "product_summary": "A private rules"}
            )
            alias = self.root / "alias"
            alias.symlink_to(self.a, target_is_directory=True)
            same = Bridge(self.base, "unused", None, project_root=alias)
            other = Bridge(self.base, "unused", None, project_root=self.b)
        self.assertEqual(
            same.projects.get(snapshot["context_id"])["context"]["product_summary"],
            "A private rules",
        )
        with self.assertRaises(ValueError):
            other.projects.get(snapshot["context_id"])
        self.assertEqual(first.scope.root, self.a)
        self.assertEqual(other.scope.root, self.b)

    def test_invalid_client_launch_root_never_falls_back_to_another_project(self):
        for value in ("", "relative-project", str(self.root / "missing")):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": value}),
                self.assertRaises(ValueError),
            ):
                resolve_scope(self.base)

    def test_foreign_database_alias_cannot_be_attached_to_this_project(self):
        first = Bridge(self.base, "unused", None, project_root=self.a)
        second = Bridge(self.base, "unused", None, project_root=self.b)
        snapshot = second.projects.publish(
            {"project_id": "product", "product_summary": "B confidential"}
        )
        first.tasks.path.unlink()
        os.link(second.tasks.path, first.tasks.path)
        with self.assertRaises(ValueError):
            Bridge(self.base, "unused", None, project_root=self.a)
        self.assertEqual(
            second.projects.get(snapshot["context_id"])["context"]["product_summary"],
            "B confidential",
        )

    def test_worker_capacity_is_shared_without_sharing_project_records(self):
        first = resolve_scope(self.base, self.a)
        second = resolve_scope(self.base, self.b)
        pools = WorkerSlots(first.base), WorkerSlots(second.base)
        with ExitStack() as stack:
            leases = [
                stack.enter_context(pools[index % 2].acquire()) for index in range(4)
            ]
            with self.assertRaises(ValueError):
                pools[1].acquire()
            leases[0].close()
            stack.enter_context(pools[1].acquire())
            with self.assertRaises(ValueError):
                pools[0].acquire()


class ProjectIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pa, self.pb = self.root / "A", self.root / "B"
        self.pa.mkdir()
        self.pb.mkdir()
        self.base = self.root / "state"
        self.peer = make_peer(self.root)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        self.a, self.b = self.bridge(self.pa), self.bridge(self.pb)
        self.grants = [self.pa]
        self.ca = await self.stack.enter_async_context(
            Client(
                build_server(self.a),
                roots=lambda _: [Root(uri=path.as_uri()) for path in self.grants],
            )
        )
        self.cb = await self.stack.enter_async_context(Client(build_server(self.b)))

    def bridge(self, root):
        return Bridge(
            self.base,
            str(self.peer),
            "unused",
            project_root=root,
            channel_enabled=False,
        )

    async def call(self, client, tool_name, **arguments):
        result = await client.call_tool(tool_name, arguments)
        return to_jsonable_python(result.data)

    async def start_question(self):
        job = await self.call(
            self.ca, "tandem_start", cwd=str(self.pa), prompt="question", mode="think"
        )
        waiting = await self.call(
            self.ca, "tandem_result", task_id=job["task_id"], wait_seconds=5
        )
        self.assertEqual(waiting["status"], "waiting_input", waiting)
        return job, waiting["question"]["question_id"]

    async def answer(self, job, question_id, answer="PROJECT_A_PRIVATE"):
        await self.call(
            self.ca,
            "tandem_reply",
            task_id=job["task_id"],
            question_id=question_id,
            answer=answer,
        )
        result = await self.call(
            self.ca,
            "tandem_result",
            task_id=job["task_id"],
            wait_seconds=5,
            details=True,
        )
        self.assertEqual(result["answer"], answer, result)
        return result

    async def test_other_project_cannot_read_reply_cancel_or_continue_a_conversation(
        self,
    ):
        job, question_id = await self.start_question()
        for name, arguments in (
            ("tandem_result", {"task_id": job["task_id"], "details": True}),
            ("tandem_wait", {"task_ids": [job["task_id"]]}),
            (
                "tandem_reply",
                {
                    "task_id": job["task_id"],
                    "question_id": question_id,
                    "answer": "corrupt",
                },
            ),
            ("tandem_cancel", {"task_id": job["task_id"]}),
            (
                "tandem_continue",
                {"conversation_id": job["conversation_id"], "prompt": "steal history"},
            ),
            (
                "tandem_publish_artifact",
                {
                    "conversation_id": job["conversation_id"],
                    "name": "foreign",
                    "content": "corrupt",
                },
            ),
        ):
            with self.subTest(tool=name), self.assertRaises(ToolError):
                await self.call(self.cb, name, **arguments)
        result = await self.answer(job, question_id)
        self.assertEqual(await self.call(self.cb, "tandem_list"), [])
        artifact_id = result["artifacts"][0]["artifact_id"]
        with self.assertRaises(ToolError):
            await self.call(self.cb, "tandem_read_artifact", artifact_id=artifact_id)
        event = self.a.channel.emit(
            "webhook", {"source": "fixture", "message": "A_ONLY"}
        )
        pending = await self.call(
            self.cb, "tandem_channel", action="pending", include_previous=True
        )
        self.assertEqual(pending["events"], [])
        for arguments in ({"task_id": job["task_id"]}, {"event_id": event["event_id"]}):
            with self.assertRaises(ToolError):
                await self.call(
                    self.cb, "tandem_channel", action="recover", **arguments
                )

    async def test_same_launch_project_can_coordinate_across_clients(self):
        job, question_id = await self.start_question()
        same = self.bridge(self.pa)
        client = await self.stack.enter_async_context(Client(build_server(same)))
        listed = await self.call(client, "tandem_list")
        self.assertEqual([row["task_id"] for row in listed], [job["task_id"]])
        await self.call(
            client,
            "tandem_reply",
            task_id=job["task_id"],
            question_id=question_id,
            answer="Coordinated",
        )
        result = await self.call(
            self.ca, "tandem_result", task_id=job["task_id"], wait_seconds=5
        )
        self.assertEqual(result["answer"], "Coordinated")
        self.assertEqual(await self.call(self.cb, "tandem_list"), [])

    async def test_task_cwd_and_symlink_do_not_change_scope_or_grant_access(self):
        escape = self.pa / "escape"
        escape.symlink_to(self.pb, target_is_directory=True)
        for cwd in (self.pb, escape):
            with self.subTest(cwd=cwd), self.assertRaises(ToolError):
                await self.call(
                    self.ca,
                    "tandem_start",
                    cwd=str(cwd),
                    prompt="paragraph",
                    mode="think",
                )
        self.assertEqual(await self.call(self.ca, "tandem_list"), [])
        self.assertEqual(
            (await self.call(self.ca, "tandem_scope"))["project_root"], str(self.pa)
        )

    async def test_client_directory_grant_allows_work_but_not_foreign_history(self):
        self.grants.append(self.pb)
        job = await self.call(
            self.ca, "tandem_start", cwd=str(self.pb), prompt="paragraph", mode="think"
        )
        result = await self.call(
            self.ca, "tandem_result", task_id=job["task_id"], wait_seconds=5
        )
        self.assertEqual(result["outcome"], "success", result)
        with self.assertRaises(ToolError):
            await self.call(self.cb, "tandem_result", task_id=job["task_id"])
        self.grants.remove(self.pb)
        with self.assertRaises(ToolError):
            await self.call(
                self.ca,
                "tandem_continue",
                conversation_id=job["conversation_id"],
                prompt="paragraph",
            )
        self.assertEqual(len(await self.call(self.ca, "tandem_list")), 1)

    async def test_product_ids_do_not_share_snapshots_without_explicit_transfer(self):
        job, question_id = await self.start_question()
        result = await self.answer(job, question_id, "Selected evidence for project A")
        artifact_id = result["artifacts"][0]["artifact_id"]
        source = await self.call(
            self.ca,
            "tandem_project_context",
            action="publish",
            context={
                "project_id": "product",
                "product_summary": "A rules",
                "artifact_ids": [artifact_id],
                "decisions": [
                    {
                        "id": "D-1",
                        "text": "Keep A behavior",
                        "status": "accepted",
                        "source": "Fixture",
                        "evidence_artifact_ids": [artifact_id],
                    }
                ],
            },
        )
        own = await self.call(
            self.cb,
            "tandem_project_context",
            action="publish",
            context={
                "project_id": "product",
                "product_summary": "B rules",
            },
        )
        self.assertEqual(own["revision"], 1)
        with self.assertRaises(ToolError):
            await self.call(
                self.cb,
                "tandem_project_context",
                action="get",
                context_id=source["context_id"],
            )
        with self.assertRaises(ToolError):
            await self.call(
                self.cb,
                "tandem_start",
                cwd=str(self.pb),
                prompt="paragraph",
                project_context_id=source["context_id"],
            )
        with self.assertRaises(ToolError):
            await self.call(
                self.cb,
                "tandem_project_context",
                action="publish",
                expected_revision=1,
                context={
                    "project_id": "product",
                    "product_summary": "Unapproved evidence",
                    "artifact_ids": [artifact_id],
                },
            )
        exported = await self.call(
            self.ca,
            "tandem_export_context",
            context_id=source["context_id"],
            target_project_root=str(self.pb),
        )
        with self.assertRaises(ToolError):
            await self.call(
                self.ca, "tandem_import_context", transfer_id=exported["transfer_id"]
            )
        imported = await self.call(
            self.cb,
            "tandem_import_context",
            transfer_id=exported["transfer_id"],
            expected_revision=1,
        )
        self.assertEqual(imported["revision"], 2)
        self.assertNotEqual(imported["context_id"], source["context_id"])
        evidence_id = imported["context"]["artifact_ids"][0]
        self.assertNotEqual(evidence_id, artifact_id)
        evidence = await self.call(
            self.cb, "tandem_read_artifact", artifact_id=evidence_id
        )
        self.assertEqual(evidence["content"], "Selected evidence for project A")
        self.assertEqual(
            imported["context"]["decisions"][0]["evidence_artifact_ids"], [evidence_id]
        )
        with self.assertRaises(ToolError):
            await self.call(self.cb, "tandem_read_artifact", artifact_id=artifact_id)
        self.assertEqual(await self.call(self.cb, "tandem_list"), [])
        repeated = await self.call(
            self.cb,
            "tandem_import_context",
            transfer_id=exported["transfer_id"],
            expected_revision=1,
        )
        self.assertEqual(repeated["context_id"], imported["context_id"])
        self.assertEqual(
            (
                await self.call(
                    self.ca,
                    "tandem_project_context",
                    action="get",
                    context_id=source["context_id"],
                )
            )["context"]["artifact_ids"],
            [artifact_id],
        )


if __name__ == "__main__":
    unittest.main()
