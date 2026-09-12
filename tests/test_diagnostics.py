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

    async def test_identity_matches_registered_mcp_and_native_schemas(self):
        import hashlib
        import json

        from omp_tandem.models import outcome_schema
        from omp_tandem.work_items import WorkCommand

        def digest(schema):
            return hashlib.sha256(
                json.dumps(
                    schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()

        scope = await self.call("tandem_scope")
        identity = scope["runtime_identity"]
        schemas = identity["schema_digests"]
        self.assertEqual(schemas["tandem_finish"], digest(outcome_schema()))
        self.assertEqual(
            schemas["tandem_work"], digest(WorkCommand.model_json_schema())
        )
        for tool in await self.client.list_tools():
            self.assertEqual(
                schemas["surfaces"]["mcp"][tool.name], digest(tool.inputSchema)
            )
        tools = self.bridge.runtime.worker.worker_tools(
            {"task_id": "schema-observation"}
        )
        for tool in tools:
            self.assertEqual(
                schemas["surfaces"]["native"][tool.name], digest(tool.parameters)
            )
        local = await self.call("tandem_diagnose")
        self.assertEqual(local["runtime_identity"], identity)
        self.assertEqual(await self.call("tandem_list"), [])
        original = identity["loaded_module_path"]
        identity["loaded_module_path"] = "consumer-mutated"
        self.assertEqual(
            (await self.call("tandem_scope"))["runtime_identity"]["loaded_module_path"],
            original,
        )

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
