"""Exercise real RPC host callbacks with a deterministic local peer, no provider calls."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
from fastmcp.exceptions import ToolError

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from tests.helpers import RpcHarness


class CollaborationTests(RpcHarness):
    async def test_reply_resumes_same_task_across_mcp_connections(self):
        job = await self.start()
        waiting = await self.result(job["task_id"])
        self.assertEqual(
            (waiting["status"], waiting["next_action"]), ("waiting_input", "reply")
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=job["conversation_id"],
                prompt="concurrent",
            )
        question_id = waiting["question"]["question_id"]
        other = Bridge(
            self.root / "state", str(self.peer), "unused", project_root=self.root
        )
        async with Client(build_server(other)) as client:
            await client.call_tool(
                "tandem_reply",
                {
                    "task_id": job["task_id"],
                    "question_id": question_id,
                    "answer": "violet",
                },
            )
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"], result["answer"]),
            ("completed", "success", "violet"),
        )
        self.assertEqual(result["task_id"], job["task_id"])
        self.assertNotIn(
            "diagnostics", await self.call("tandem_result", task_id=job["task_id"])
        )
        await self.call(
            "tandem_reply",
            task_id=job["task_id"],
            question_id=question_id,
            answer="violet",
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_reply",
                task_id=job["task_id"],
                question_id=question_id,
                answer="changed",
            )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_reply",
                task_id=job["task_id"],
                question_id=str(uuid4()),
                answer="stale",
            )
        outcome_artifact = next(
            item for item in result["artifacts"] if item["name"] == "outcome"
        )
        stored = await self.call(
            "tandem_read_artifact", artifact_id=outcome_artifact["artifact_id"]
        )
        self.assertEqual(json.loads(stored["content"]), result["report"])

    async def test_question_expiry_does_not_invent_success(self):
        job = await self.start("expire", question_timeout_seconds=1)
        waiting = await self.result(job["task_id"])
        self.assertEqual(waiting["status"], "waiting_input")
        await asyncio.sleep(1.1)
        result = await self.result(job["task_id"])
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "blocked")
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_reply",
                task_id=job["task_id"],
                question_id=waiting["question"]["question_id"],
                answer="late",
            )

    async def test_cancellation_invalidates_pending_question(self):
        job = await self.start()
        waiting = await self.result(job["task_id"])
        await self.call("tandem_cancel", task_id=job["task_id"])
        result = await self.result(job["task_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["outcome"])
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_reply",
                task_id=job["task_id"],
                question_id=waiting["question"]["question_id"],
                answer="too late",
            )

    async def test_long_answer_is_explicitly_paged_without_data_loss(self):
        job = await self.start("long-answer")
        result = await self.result(job["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["answer_truncated"])
        expected = "雪界𝄞" * 6000
        self.assertEqual(result["answer"], expected[:16000])
        parts, offset = [], 0
        while True:
            page = await self.call(
                "tandem_read_artifact",
                artifact_id=result["answer_artifact_id"],
                offset=offset,
                limit=7000,
            )
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(parts), expected)
        complete = await self.call(
            "tandem_result", task_id=job["task_id"], details=True
        )
        self.assertEqual(complete["answer"], expected)
        self.assertFalse(complete["answer_truncated"])

    async def test_actual_answer_is_delivered_instead_of_acknowledgment(self):
        job = await self.start("paragraph")
        result = await self.result(job["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            result["answer"], "Поручайте независимый анализ; приёмку проверяйте сами."
        )
        reply = next(item for item in result["artifacts"] if item["name"] == "reply")
        stored = await self.call(
            "tandem_read_artifact", artifact_id=reply["artifact_id"]
        )
        self.assertEqual(stored["content"], result["answer"])

    async def test_wait_bounds_are_discoverable_and_enforced(self):
        tools = await self.client.list_tools()
        schema = next(
            tool for tool in tools if tool.name == "tandem_result"
        ).inputSchema
        wait = schema["properties"]["wait_seconds"]
        self.assertEqual((wait.get("minimum"), wait.get("maximum")), (0, 25))
        job = await self.start("blocked")
        await self.result(job["task_id"])
        for value in (-1, 26):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await self.call(
                    "tandem_result", task_id=job["task_id"], wait_seconds=value
                )
        for value in (0, 25):
            result = await self.call(
                "tandem_result", task_id=job["task_id"], wait_seconds=value
            )
            self.assertEqual(result["status"], "completed")

    async def test_missing_report_cannot_be_claimed_as_success(self):
        job = await self.start("missing-report")
        result = await self.result(job["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["outcome"])
        self.assertEqual(
            result["answer"], "I claim success without a structured report"
        )
        reply = next(item for item in result["artifacts"] if item["name"] == "reply")
        raw = await self.call("tandem_read_artifact", artifact_id=reply["artifact_id"])
        self.assertEqual(raw["content"], "I claim success without a structured report")

    async def test_completed_blocked_is_distinct_from_transport_failure(self):
        job = await self.start("blocked")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "blocked")
        )
        self.assertEqual(result["report"]["blockers"], ["Credentials are required"])
        self.assertNotIn("error", result)

    async def test_contract_survives_follow_up_and_prevents_overlapping_assignment(
        self,
    ):
        contract = {
            "goal": "question",
            "scope": {"owned_files": ["owned.py"]},
            "constraints": ["No dependencies"],
            "acceptance": ["Return the selected value"],
        }
        job = await self.call(
            "tandem_start",
            cwd=str(self.root),
            contract=contract,
            mode="work",
            timeout_seconds=10,
        )
        waiting = await self.result(job["task_id"])
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_start", cwd=str(self.root), contract=contract, mode="work"
            )
        await self.call(
            "tandem_reply",
            task_id=job["task_id"],
            question_id=waiting["question"]["question_id"],
            answer="green",
        )
        first = await self.result(job["task_id"], details=True)
        follow = await self.call(
            "tandem_continue",
            conversation_id=job["conversation_id"],
            prompt="blocked",
            timeout_seconds=10,
        )
        second = await self.result(follow["task_id"], details=True)
        self.assertEqual(second["work_policy"], first["work_policy"])
        self.assertEqual(second["current_task"]["goal"], "blocked")
        self.assertEqual(second["current_task"]["acceptance"], [])
        self.assertEqual(second["outcome"], "blocked")

    async def test_contract_rejects_escaping_symlink_and_ambiguous_input(self):
        (self.root / "outside").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_start",
                cwd=str(self.root),
                contract={
                    "goal": "question",
                    "scope": {"owned_files": ["outside/file.py"]},
                },
                mode="work",
            )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_start",
                cwd=str(self.root),
                prompt="question",
                contract={"goal": "question"},
            )


class MigrationTests(unittest.TestCase):
    def test_previous_database_keeps_history_without_inventing_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_id, conversation_id = str(uuid4()), str(uuid4())
            with closing(sqlite3.connect(root / "tasks.sqlite3")) as db, db:
                db.execute("""CREATE TABLE tasks (task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, cwd TEXT NOT NULL, mode TEXT NOT NULL,
                    model TEXT NOT NULL, prompt TEXT NOT NULL, session_file TEXT, status TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '', error TEXT, activity TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0)""")
                db.execute(
                    "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,answer) VALUES (?,?,1,1,?,'think','unused','old prompt','completed','old answer')",
                    (task_id, conversation_id, str(root)),
                )
            (root / f"{conversation_id}.lock").touch()
            bridge = Bridge(root, "unused", "unused", project_root=root)
            result = bridge.view(task_id, details=True)
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["outcome"])
            self.assertEqual(result["answer"], "old answer")
            self.assertEqual(result["answer_source"], "unstructured")
            reopened = Bridge(root, "unused", "unused", project_root=root)
            self.assertEqual(reopened.tasks.get(task_id)["prompt"], "old prompt")


class QuestionBoundaryTests(unittest.TestCase):
    def test_expired_question_is_not_offered_before_worker_wakes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = Bridge(root, "unused", "unused", project_root=root)
            task_id, conversation_id, question_id = (str(uuid4()) for _ in range(3))
            now = time.time()
            with closing(sqlite3.connect(bridge.tasks.path)) as db, db:
                db.execute(
                    "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) VALUES (?,?,?,?,?,'think','unused','question','waiting_input',?)",
                    (task_id, conversation_id, now - 5, now - 5, str(root), now + 60),
                )
                db.execute(
                    "INSERT INTO questions(question_id,task_id,question,context,options_json,created,deadline,state) VALUES (?,?,'Choice?','','[]',?,?,'pending')",
                    (question_id, task_id, now - 5, now - 1),
                )
            with bridge.tasks.lock(conversation_id):
                result = bridge.view(task_id)
                self.assertEqual(
                    (result["status"], result["next_action"]), ("running", "wait")
                )
                self.assertNotIn("question", result)
                with self.assertRaises(ValueError):
                    bridge.reply(task_id, question_id, "late")


if __name__ == "__main__":
    unittest.main()
