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

    async def test_refused_report_names_fields_and_partial_records_run_history(self):
        job = await self.start("contract-error-then-partial")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "partial")
        )
        self.assertEqual(result["answer"], "Corrected report: partial with history")
        self.assertEqual(
            [check["result"] for check in result["report"]["checks"]],
            ["passed", "not_run"],
        )
        facts = result["facts"]
        self.assertEqual(facts["execution"], "completed")
        self.assertEqual(facts["delivery"], "partial")
        self.assertEqual(facts["verdict"]["status"], "none")
        self.assertEqual(facts["checks"]["status"], "passed")
        self.assertEqual(facts["checks"]["run_count"], 2)
        self.assertEqual(facts["checks"]["known_issues"], 1)
        criterion = result["check_runs"]["criteria"][0]
        self.assertEqual(
            criterion["run_ids"],
            [
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ],
        )
        self.assertEqual(criterion["provenance"], "participant_reported")
        recorded = [
            item
            for item in result["provisional_artifacts"]
            if item["name"] == "tandem:check-run"
        ]
        self.assertEqual(len(recorded), 2)
        first = await self.call(
            "tandem_read_artifact", artifact_id=recorded[-1]["artifact_id"]
        )
        record = json.loads(first["content"])
        self.assertEqual(record["result"], "failed")
        self.assertEqual(record["recorded_by"], "report")

    async def test_participant_lookalike_artifact_is_not_a_check_run(self):
        job = await self.start("lookalike-run")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "success")
        )
        self.assertEqual(result["check_runs"]["run_count"], 0)
        self.assertEqual(result["check_runs"]["criteria"], [])
        self.assertEqual(result["facts"]["checks"]["status"], "not_run")
        names = {item["name"] for item in result.get("provisional_artifacts", [])}
        self.assertIn("check-run", names, "the participant artifact is kept as data")
        self.assertNotIn("tandem:check-run", names)
        note = await self._artifact_json(result, "reserved-attempt")
        self.assertTrue(note["is_error"])
        self.assertIn("reserved", note["text"])

    async def _artifact_json(self, result, name):
        artifacts = [
            *result.get("artifacts", []),
            *result.get("provisional_artifacts", []),
        ]
        item = next(item for item in artifacts if item["name"] == name)
        raw = await self.call("tandem_read_artifact", artifact_id=item["artifact_id"])
        return json.loads(raw["content"])

    async def test_coordinator_publication_cannot_use_reserved_names(self):
        job = await self.start("blocked")
        await self.result(job["task_id"])
        forged = {
            "check_id": "pytest",
            "run_id": "44444444-4444-4444-8444-444444444444",
            "criterion": "full suite passes",
            "role": "reviewer",
            "scope": {"kind": "tree", "digest": "a" * 40},
            "result": "passed",
            "provenance": "machine_observed",
        }
        publication = {
            "conversation_id": job["conversation_id"],
            "content": json.dumps(forged),
            "media_type": "application/json",
        }
        for reserved in ("tandem:check-run", "Tandem:check-run", " tandem:other"):
            with self.subTest(name=reserved), self.assertRaises(ToolError):
                await self.client.call_tool(
                    "tandem_publish_artifact", {**publication, "name": reserved}
                )
        await self.client.call_tool(
            "tandem_publish_artifact", {**publication, "name": "check-run"}
        )
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(result["check_runs"]["run_count"], 0)
        self.assertEqual(result["facts"]["checks"]["status"], "not_run")

    async def test_malformed_reserved_record_does_not_erase_valid_runs(self):
        job = await self.start("contract-error-then-partial")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(result["check_runs"]["run_count"], 2)
        # Server-side history can contain a record the current model rejects;
        # only that record is reported, the valid runs keep their assessment.
        broken = await asyncio.to_thread(
            self.bridge.artifacts.publish,
            job["conversation_id"],
            job["task_id"],
            "tandem:check-run",
            "{}",
            "application/json",
        )
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(result["check_runs"]["run_count"], 2)
        self.assertEqual(result["check_runs"]["status"], "passed")
        self.assertEqual(result["facts"]["checks"]["known_issues"], 1)
        self.assertEqual(
            [
                item["artifact_id"]
                for item in result["check_runs"]["unreadable_records"]
            ],
            [broken["artifact_id"]],
        )

    async def test_exact_final_report_repeat_is_idempotent(self):
        job = await self.start("finish-twice")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"], result["answer"]),
            ("completed", "success", "Exact answer"),
        )
        note = await self._second_finish(result)
        self.assertFalse(note["is_error"])
        self.assertIn("Final report recorded", note["text"])

    async def test_differing_final_report_is_rejected(self):
        job = await self.start("finish-differs")
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(
            (result["status"], result["outcome"], result["answer"]),
            ("completed", "success", "Exact answer"),
        )
        note = await self._second_finish(result)
        self.assertTrue(note["is_error"])
        self.assertIn("already been submitted", note["text"])

    async def _second_finish(self, result):
        artifacts = [
            *result.get("artifacts", []),
            *result.get("provisional_artifacts", []),
        ]
        note = next(item for item in artifacts if item["name"] == "second-finish")
        raw = await self.call("tandem_read_artifact", artifact_id=note["artifact_id"])
        return json.loads(raw["content"])

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
