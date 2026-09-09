"""Observable task/profile/coordination transitions through the real MCP interface."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastmcp.exceptions import ToolError

from tests import helpers


def product(
    project_id="sample-product", rule_text="Standard uploads need no extra confirmation"
):
    return {
        "project_id": project_id,
        "product_summary": "A test document product with a predictable upload workflow",
        "rules": [
            {"id": "UX-01", "text": rule_text, "source": "Approved test requirement"}
        ],
        "decisions": [
            {
                "id": "DEC-01",
                "text": "Use substring-only command matching as a security boundary",
                "status": "rejected",
                "source": "Approved test decision",
            }
        ],
    }


class ProductCoordinationTests(helpers.RpcHarness):
    async def publish(self, context=None, **args):
        return await self.call(
            "tandem_project_context",
            action="publish",
            context=context or product(),
            **args,
        )

    async def test_running_task_and_followups_keep_pinned_rules_until_explicit_update(
        self,
    ):
        first_profile = await self.publish()
        contract = {
            "goal": "question",
            "scope": {"owned_files": ["owned.py"]},
            "constraints": ["Keep dependencies unchanged"],
            "acceptance": ["Original audit matrix"],
        }
        job = await self.call(
            "tandem_start",
            cwd=str(self.root),
            mode="work",
            contract=contract,
            project_context_id=first_profile["context_id"],
            timeout_seconds=20,
        )
        waiting = await self.result(job["task_id"])
        second_profile = await self.publish(
            product(rule_text="New approved workflow"), expected_revision=1
        )
        unchanged = await self.call(
            "tandem_result", task_id=job["task_id"], details=True
        )
        self.assertEqual(unchanged["project_context"]["revision"], 1)
        await self.call(
            "tandem_reply",
            task_id=job["task_id"],
            question_id=waiting["question"]["question_id"],
            answer="green",
        )
        first = await self.result(job["task_id"], details=True)
        inherited = await self.call(
            "tandem_continue",
            conversation_id=job["conversation_id"],
            prompt="blocked",
            timeout_seconds=10,
        )
        inherited_result = await self.result(inherited["task_id"], details=True)
        self.assertEqual(inherited_result["project_context"]["revision"], 1)
        self.assertEqual(inherited_result["outcome"], "blocked")
        self.assertEqual(inherited_result["current_task"]["acceptance"], [])
        self.assertEqual(inherited_result["work_policy"], first["work_policy"])
        updated = await self.call(
            "tandem_continue",
            conversation_id=job["conversation_id"],
            contract={
                "goal": "blocked",
                "acceptance": ["Advice only"],
                "constraints": ["No shell in this turn"],
            },
            project_context_id=second_profile["context_id"],
            timeout_seconds=10,
        )
        updated_result = await self.result(updated["task_id"], details=True)
        self.assertEqual(updated_result["project_context"]["revision"], 2)
        self.assertEqual(
            updated_result["project_context_changed_from"], first_profile["context_id"]
        )
        self.assertEqual(updated_result["work_policy"], first["work_policy"])
        next_turn = await self.call(
            "tandem_continue",
            conversation_id=job["conversation_id"],
            prompt="blocked",
            timeout_seconds=10,
        )
        next_result = await self.result(next_turn["task_id"], details=True)
        self.assertEqual(next_result["current_task"]["constraints"], [])
        self.assertEqual(next_result["work_policy"], first["work_policy"])

    async def test_context_cannot_cross_products_or_broaden_followup_scope(self):
        one = await self.publish()
        other = await self.publish(product("other-product"))
        job = await self.start("blocked", project_context_id=one["context_id"])
        await self.result(job["task_id"])
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=job["conversation_id"],
                prompt="blocked",
                project_context_id=other["context_id"],
            )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=job["conversation_id"],
                contract={"goal": "blocked", "scope": {"owned_files": ["new.py"]}},
            )

    async def test_unknown_rule_reference_cannot_be_recorded_as_success(self):
        profile = await self.publish()
        job = await self.start("unknown-rule", project_context_id=profile["context_id"])
        result = await self.result(job["task_id"], details=True)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["outcome"])
        self.assertIsNone(result["report"])

    async def test_group_wait_returns_another_tasks_question_without_waiting_for_first(
        self,
    ):
        busy = await self.start("hold", timeout_seconds=30)
        question = await self.start("question", timeout_seconds=30)
        result = await asyncio.wait_for(
            self.call(
                "tandem_wait",
                task_ids=[busy["task_id"], question["task_id"]],
                wait_seconds=25,
            ),
            timeout=4,
        )
        self.assertEqual(result["pending"], [busy["task_id"]])
        self.assertEqual(
            [item["task_id"] for item in result["ready"]], [question["task_id"]]
        )
        self.assertEqual(result["ready"][0]["next_action"], "reply")
        await self.call(
            "tandem_reply",
            task_id=question["task_id"],
            question_id=result["ready"][0]["question"]["question_id"],
            answer="blue",
        )
        ready = await asyncio.wait_for(
            self.call(
                "tandem_wait",
                task_ids=[busy["task_id"], question["task_id"]],
                wait_seconds=25,
            ),
            timeout=4,
        )
        self.assertEqual(ready["ready"][0]["status"], "completed")
        self.assertNotIn("answer", ready["ready"][0])

    async def test_checkpoint_remains_readable_when_final_report_never_arrives(self):
        job = await self.start("checkpoint-failure")
        result = await self.result(job["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["outcome"])
        checkpoint = next(
            item
            for item in result["provisional_artifacts"]
            if item["name"] == "checkpoint"
        )
        content = await self.call(
            "tandem_read_artifact", artifact_id=checkpoint["artifact_id"]
        )
        self.assertEqual(content["content"], "Unconfirmed preliminary finding")
        self.assertNotIn(
            checkpoint["artifact_id"],
            {item["artifact_id"] for item in result["artifacts"]},
        )

    async def test_worker_cannot_replace_coordinator_question_deadline(self):
        job = await self.start(
            "short-worker-timeout", question_timeout_seconds=8, timeout_seconds=20
        )
        waiting = await self.result(job["task_id"])
        self.assertEqual(waiting["status"], "waiting_input")
        await asyncio.sleep(1.2)
        still_waiting = await self.call("tandem_result", task_id=job["task_id"])
        self.assertEqual(still_waiting["status"], "waiting_input")
        await self.call(
            "tandem_reply",
            task_id=job["task_id"],
            question_id=still_waiting["question"]["question_id"],
            answer="green",
        )
        result = await self.result(job["task_id"])
        self.assertEqual(result["outcome"], "success")

    async def test_unknown_evidence_cannot_create_a_product_snapshot(self):
        invalid = product()
        invalid["artifact_ids"] = [str(uuid4())]
        with self.assertRaises(ToolError):
            await self.publish(invalid)
        listed = await self.call("tandem_project_context", action="list")
        self.assertEqual(listed["contexts"], [])
