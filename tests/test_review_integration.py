"""Snapshot access and stage boundaries exercised through MCP and native host tools."""

import json
import subprocess
from contextlib import closing

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

    async def test_staged_mcp_snapshot_native_reader_excludes_worktree(self):
        def git(*args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=15,
            ).stdout

        git("init", "-q")
        git("config", "user.email", "review@example.invalid")
        git("config", "user.name", "Review Integration")
        git("add", "sample.txt")
        git("commit", "-qm", "base")
        (self.root / "sample.txt").write_text("index version\n")
        git("add", "sample.txt")
        (self.root / "sample.txt").write_text("excluded worktree version\n")
        (self.root / "untracked.txt").write_text("excluded untracked file")
        review = await self.call(
            "tandem_review",
            action="create",
            request={"requirements": "Review the index only.", "source": "staged"},
        )
        self.assertEqual(review["source"], "staged")
        self.assertEqual(review["file_count"], 1)
        (self.root / "sample.txt").unlink()
        (self.root / "sample.txt").symlink_to(self.root / "missing-target")
        first = await self.start("snapshot-reader", review_id=review["review_id"])
        result = await self.result(first["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            json.loads(result["answer"]),
            {"saved": "index version\n", "author_visible": False},
        )
        self.assertEqual(
            result["review"]["applicability"]["status"], "current_selected_state"
        )
        self.assertEqual(result["review"]["applicability"]["source"], "staged")
        (self.root / "sample.txt").unlink()
        (self.root / "sample.txt").write_text("next index version\n")
        git("add", "sample.txt")
        refreshed = await self.result(first["task_id"])
        self.assertEqual(refreshed["review"]["applicability"]["status"], "stale")
        self.assertEqual(json.loads(refreshed["answer"])["saved"], "index version\n")

    async def test_corrective_snapshot_requires_fresh_assessment_without_author_access(
        self,
    ):
        first = await self.start("snapshot-reader", review_id=self.review["review_id"])
        await self.result(first["task_id"])
        (self.root / "sample.txt").write_text("corrected version\n")
        corrected = await self.call(
            "tandem_review",
            action="create",
            request={
                "requirements": "Reassess the corrected sample",
                "paths": ["sample.txt"],
                "author_rationale": "Private correction rationale",
                "corrective": {
                    "previous_review_id": self.review["review_id"],
                    "previous_code_fingerprint": self.review["code_fingerprint"],
                    "open_findings": [],
                },
            },
        )
        self.assertEqual(corrected["exposure"], "prior_exposed")
        self.assertEqual(
            corrected["corrective"]["applicability"]["review_id"],
            corrected["review_id"],
        )
        with self.assertRaises(ToolError):
            await self.call(
                "tandem_continue",
                conversation_id=first["conversation_id"],
                prompt="snapshot-reader",
                review_id=corrected["review_id"],
                review_stage="comparison",
            )
        second = await self.start("snapshot-reader", review_id=corrected["review_id"])
        result = await self.result(second["task_id"])
        self.assertEqual(
            json.loads(result["answer"]),
            {"saved": "corrected version\n", "author_visible": False},
        )


VERIFICATION_PEER = PEER.replace(
    "        if scenario == 'missing-report':",
    """        if scenario == 'verification-omission':
            finish({'outcome': 'success', 'summary': 'Unverified', 'answer': 'Missing required verification'})
        elif scenario == 'missing-report':""",
).replace(
    "        if scenario in ('not-run-outcome', 'blocked-without-reason'):",
    """        if scenario == 'verification-omission':
            if not command.get('isError'):
                end('Omission incorrectly accepted')
            else:
                runs = [
                    {'check_id': 'boundary', 'run_id': '11111111-1111-4111-8111-111111111111', 'criterion': 'Boundary preserved', 'role': 'reviewer', 'scope': {'kind': 'content', 'digest': 'a' * 64}, 'result': 'failed'},
                    {'check_id': 'boundary', 'run_id': '22222222-2222-4222-8222-222222222222', 'criterion': 'Boundary preserved', 'role': 'reviewer', 'scope': {'kind': 'content', 'digest': 'b' * 64}, 'result': 'passed'},
                ]
                scenario = 'verification-corrected'
                finish({'outcome': 'success', 'summary': 'Verified', 'answer': 'Omission refused; current checklist accepted',
                        'checks': [{'name': 'Boundary preserved', 'check_id': 'boundary', 'result': 'passed', 'run_id': runs[-1]['run_id']}],
                        'check_runs': runs})
        elif scenario in ('not-run-outcome', 'blocked-without-reason'):""",
)


class VerificationIntegrationTests(RpcHarness):
    async def test_current_contract_refuses_omission_and_records_failed_history(self):
        (self.root / "peer.py").write_text(VERIFICATION_PEER)
        started = await self.start(
            contract={
                "goal": "verification-omission",
                "verification": {
                    "checks": [
                        {
                            "id": "boundary",
                            "criterion": "Boundary preserved",
                            "estimated_seconds": 1,
                            "scope": {"kind": "content", "digest": "b" * 64},
                        }
                    ],
                },
            },
            prompt=None,
        )
        result = await self.result(started["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["answer"], "Omission refused; current checklist accepted"
        )
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(result["facts"]["checks"]["status"], "passed")
        self.assertEqual(result["check_runs"]["status"], "failed")
        with closing(self.bridge.tasks.connect()) as db:
            saved = json.loads(
                db.execute(
                    "SELECT report_json FROM tasks WHERE task_id=?",
                    (started["task_id"],),
                ).fetchone()["report_json"]
            )
        self.assertEqual(
            [run["result"] for run in saved["check_runs"]], ["failed", "passed"]
        )
