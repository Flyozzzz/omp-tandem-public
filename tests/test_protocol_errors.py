"""MCP refusals stay errors and provide usable, non-replaying repair data."""

from uuid import uuid4

from tests.helpers import RpcHarness


class ProtocolErrorTests(RpcHarness):
    async def test_missing_run_identifier_returns_executable_observation_repair(self):
        result = await self.client.call_tool(
            "tandem_team", {"action": "status"}, raise_on_error=False
        )
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "missing_fields")
        self.assertIn("run_id", error["fields"])
        self.assertFalse(error["retryable"])
        repair = error["next_request"]
        observed = await self.call(repair["tool"], **repair["arguments"])
        self.assertEqual(observed["runs"], [])
        self.assertEqual(await self.call("tandem_list"), [])

    async def test_nested_work_request_reports_missing_identity_not_unknown_work(self):
        result = await self.client.call_tool(
            "tandem_work", {"request": {"action": "get"}}, raise_on_error=False
        )
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "missing_fields")
        self.assertTrue(any(field.endswith("work_id") for field in error["fields"]))
        self.assertFalse(error["retryable"])
        self.assertEqual(
            (await self.call("tandem_work", request={"action": "list"}))["items"], []
        )

    async def test_schema_validation_preserves_error_and_field_location(self):
        result = await self.client.call_tool(
            "tandem_result",
            {"task_id": str(uuid4()), "wait_seconds": -1},
            raise_on_error=False,
        )
        self.assertTrue(result.is_error)
        error = result.structured_content["error"]
        self.assertEqual(error["code"], "invalid_arguments")
        self.assertIn("wait_seconds", error["fields"])
        self.assertFalse(error["retryable"])
        self.assertEqual(await self.call("tandem_list"), [])
