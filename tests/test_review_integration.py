"""Snapshot access and stage boundaries exercised through MCP and native host tools."""

import json

from fastmcp.exceptions import ToolError

from tests.helpers import PEER, RpcHarness

REVIEW_PEER = PEER.replace(
    "        if scenario == 'missing-report':",
    """        if scenario == 'snapshot-reader':
            emit({'type': 'host_tool_call', 'id': 'saved', 'toolCallId': 'saved-call', 'toolName': 'tandem_review_read', 'arguments': {'section': 'selected', 'path': 'sample.txt'}})
        elif scenario == 'missing-report':""",
).replace(
    "    elif kind == 'host_tool_result' and command['id'] == 'question':",
    """    elif kind == 'host_tool_result' and command['id'] == 'saved':
        saved = json.loads(command['result']['content'][0]['text'])
        emit({'type': 'host_tool_call', 'id': 'author', 'toolCallId': 'author-call', 'toolName': 'tandem_review_read', 'arguments': {'section': 'author'}})
    elif kind == 'host_tool_result' and command['id'] == 'author':
        author_visible = not command.get('isError', False)
        answer = json.dumps({'saved': saved['content'], 'author_visible': author_visible})
        finish({'outcome': 'success', 'summary': 'Snapshot read', 'answer': answer})
    elif kind == 'host_tool_result' and command['id'] == 'question':""",
)


class ReviewIntegrationTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(REVIEW_PEER)
        (self.root / "sample.txt").write_text("saved version\n")
        self.review = await self.call(
            "tandem_review",
            action="create",
            request={
                "requirements": "Review the saved sample without live file access.",
                "paths": ["sample.txt"],
                "author_proposal": "Author's proposed implementation",
                "author_rationale": "An explanation withheld until comparison",
                "external_boundaries": ["No external services were captured."],
            },
        )

    async def test_saved_content_survives_live_mutation_and_author_requires_second_stage(
        self,
    ):
        (self.root / "sample.txt").write_text("new live version\n")
        first = await self.start("snapshot-reader", review_id=self.review["review_id"])
        result = await self.result(first["task_id"])
        self.assertEqual(result["status"], "completed")
        answer = json.loads(result["answer"])
        self.assertEqual(answer, {"saved": "saved version\n", "author_visible": False})
        self.assertEqual(result["review"]["applicability"]["status"], "stale")
        followup = await self.call(
            "tandem_continue",
            conversation_id=first["conversation_id"],
            prompt="snapshot-reader",
            review_stage="comparison",
        )
        compared = await self.result(followup["task_id"])
        self.assertEqual(
            json.loads(compared["answer"]),
            {"saved": "saved version\n", "author_visible": True},
        )
        original = await self.result(first["task_id"])
        self.assertEqual(original["answer"], result["answer"])

    async def test_comparison_cannot_precede_independent_assessment(self):
        with self.assertRaises(ToolError):
            await self.start(
                "snapshot-reader",
                review_id=self.review["review_id"],
                review_stage="comparison",
            )
        self.assertEqual(await self.call("tandem_list"), [])

    async def test_snapshot_binding_does_not_enable_live_tools(self):
        with self.assertRaises(ToolError):
            await self.start(
                "snapshot-reader", review_id=self.review["review_id"], mode="work"
            )
        self.assertEqual((self.root / "sample.txt").read_text(), "saved version\n")
        self.assertEqual(await self.call("tandem_list"), [])

    async def test_new_snapshot_needs_its_own_independent_assessment(self):
        first = await self.start("snapshot-reader", review_id=self.review["review_id"])
        await self.result(first["task_id"])
        second_review = await self.call(
            "tandem_review",
            action="create",
            request={"requirements": "A new review", "paths": ["sample.txt"]},
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=first["conversation_id"],
                prompt="snapshot-reader",
                review_id=second_review["review_id"],
                review_stage="comparison",
            )
        second = await self.call(
            "tandem_continue",
            conversation_id=first["conversation_id"],
            prompt="snapshot-reader",
            review_id=second_review["review_id"],
        )
        result = await self.result(second["task_id"])
        self.assertEqual(result["review"]["stage"], "independent")
        self.assertFalse(json.loads(result["answer"])["author_visible"])
