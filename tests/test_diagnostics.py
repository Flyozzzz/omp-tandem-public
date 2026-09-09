"""A diagnostic is an explicit paid turn, not an implicit provider probe or rerun."""

import asyncio

from fastmcp.exceptions import ToolError

from tests.helpers import PEER, RpcHarness

DIAGNOSTIC_PEER = PEER.replace(
    "'thinkingLevel': 'high'",
    "'thinkingLevel': sys.argv[sys.argv.index('--thinking') + 1]",
).replace(
    "        if scenario == 'missing-report':",
    """        if 'OMP_TANDEM_DIAGNOSTIC_' in scenario:
            answer = scenario.split('answer exactly ', 1)[1].split('. Then', 1)[0]
            finish({'outcome': 'success', 'summary': 'Connectivity checked', 'answer': answer})
        elif scenario == 'missing-report':""",
)


class DiagnosticTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(DIAGNOSTIC_PEER)

    async def test_local_check_and_wrong_project_do_not_start_provider_work(self):
        local = await self.call("tandem_diagnose")
        self.assertEqual(local["runtime"]["authentication"], "not_checked")
        mismatch = await self.call(
            "tandem_diagnose", live=True, expected_project=str(self.root / "other")
        )
        self.assertEqual(mismatch["status"], "blocked")
        self.assertFalse(mismatch["project"]["matches_expected"])
        self.assertEqual(await self.call("tandem_list"), [])

    async def test_followup_inspects_same_probe_without_another_paid_turn(self):
        first = await self.call("tandem_diagnose", live=True, wait_seconds=0)
        for _ in range(30):
            result = await self.call(
                "tandem_diagnose", task_id=first["task_id"], wait_seconds=0
            )
            if result["status"] != "running":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["runtime"]["authentication"], "verified_by_task")
        self.assertIsNone(result["runtime"]["actual_model"])
        self.assertFalse(result["channel"]["confirmed"])
        self.assertEqual(
            [task["task_id"] for task in await self.call("tandem_list")],
            [first["task_id"]],
        )
        with self.assertRaises(ToolError):
            await self.call("tandem_diagnose", live=True, task_id=first["task_id"])

    async def test_ordinary_completed_task_is_not_authentication_proof(self):
        ordinary = await self.start("paragraph")
        await self.result(ordinary["task_id"])
        with self.assertRaises(ToolError):
            await self.call("tandem_diagnose", task_id=ordinary["task_id"])
