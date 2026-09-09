"""MCP request metadata must choose the workspace before any project state is opened."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import Root
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.binding import CODEX_SCOPE_CAPABILITY, RuntimeOptions


class ClientBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / "Project A 'quoted'"
        self.b = self.root / "Project B"
        self.plugin = self.root / "installed plugin"
        for directory in (self.a, self.b, self.plugin):
            directory.mkdir()
        self.state = self.root / "state"
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)

    def metadata(self, root):
        return {CODEX_SCOPE_CAPABILITY: {"sandboxCwd": root.as_uri()}}

    async def client(self, *, root=None, roots=None, environment=None):
        clean = {
            name: value
            for name, value in os.environ.items()
            if name
            not in (
                "CLAUDE_PROJECT_DIR",
                "PLUGIN_ROOT",
                "PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA",
            )
        }
        clean.update(environment or {"PLUGIN_ROOT": str(self.plugin)})
        with (
            patch.dict(os.environ, clean, clear=True),
            patch.object(Path, "cwd", return_value=self.plugin),
        ):
            server = build_server(
                RuntimeOptions(
                    self.state, "unused", project_root=root, channel_enabled=False
                )
            )
        return await self.stack.enter_async_context(Client(server, roots=roots))

    async def call(self, client, name, arguments=None, *, root=None, meta=None):
        if root is not None:
            meta = self.metadata(root)
        return to_jsonable_python(
            (await client.call_tool(name, arguments or {}, meta=meta)).data
        )

    async def test_discovery_and_model_cwd_do_not_open_plugin_history(self):
        client = await self.client()
        await client.list_tools()
        self.assertFalse(self.state.exists())
        with self.assertRaises(ToolError):
            await self.call(
                client,
                "tandem_start",
                {
                    "cwd": str(self.a),
                    "prompt": "Do not choose the namespace",
                    "mode": "think",
                },
            )
        with self.assertRaises(ToolError):
            await self.call(client, "tandem_scope", {"sandboxCwd": self.a.as_uri()})
        self.assertFalse(self.state.exists())

    async def test_two_client_metadata_roots_keep_product_knowledge_separate(self):
        first, second = await self.client(), await self.client()
        a = await self.call(first, "tandem_scope", root=self.a)
        b = await self.call(second, "tandem_scope", root=self.b)
        self.assertEqual(a["project_root"], str(self.a))
        self.assertEqual(b["project_root"], str(self.b))
        self.assertEqual(a["root_source"], "codex_request")
        self.assertNotEqual(a["scope_id"], b["scope_id"])
        snapshot = await self.call(
            first,
            "tandem_project_context",
            {
                "action": "publish",
                "context": {
                    "project_id": "demo",
                    "product_summary": "A-only knowledge",
                },
            },
            root=self.a,
        )
        self.assertEqual(
            (
                await self.call(
                    second, "tandem_project_context", {"action": "list"}, root=self.b
                )
            )["contexts"],
            [],
        )
        with self.assertRaises(ToolError):
            await self.call(
                second,
                "tandem_project_context",
                {"action": "get", "context_id": snapshot["context_id"]},
                root=self.b,
            )
        own = await self.call(
            second,
            "tandem_project_context",
            {
                "action": "publish",
                "context": {
                    "project_id": "demo",
                    "product_summary": "B-only knowledge",
                },
            },
            root=self.b,
        )
        self.assertEqual(own["revision"], 1)

    async def test_connection_cannot_rebind_or_drop_codex_metadata(self):
        client = await self.client()
        await self.call(client, "tandem_scope", root=self.a)
        for meta in (self.metadata(self.b), None):
            with (
                self.subTest(meta_present=meta is not None),
                self.assertRaises(ToolError),
            ):
                await self.call(
                    client, "tandem_project_context", {"action": "list"}, meta=meta
                )
        same = await self.call(client, "tandem_scope", root=self.a)
        self.assertEqual(same["project_root"], str(self.a))
        self.assertEqual(len(list((self.state / "projects").iterdir())), 1)

    async def test_nonlocal_or_malformed_metadata_never_creates_state(self):
        client = await self.client()
        for value in (
            "file://other-host/project",
            "https://example.com/project",
            "relative",
            42,
            self.root.joinpath("missing").as_uri(),
        ):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await self.call(
                    client,
                    "tandem_scope",
                    meta={CODEX_SCOPE_CAPABILITY: {"sandboxCwd": value}},
                )
        self.assertFalse(self.state.exists())

    async def test_current_codex_metadata_wins_over_an_inherited_claude_variable(self):
        client = await self.client(
            environment={
                "PLUGIN_ROOT": str(self.plugin),
                "CLAUDE_PROJECT_DIR": str(self.a),
            }
        )
        result = await self.call(client, "tandem_scope", root=self.b)
        self.assertEqual(result["project_root"], str(self.b))
        self.assertEqual(result["root_source"], "codex_request")

    async def test_explicit_operator_root_is_authoritative(self):
        client = await self.client(root=self.a)
        result = await self.call(
            client,
            "tandem_scope",
            meta={
                CODEX_SCOPE_CAPABILITY: {
                    "sandboxCwd": "file://unavailable-host/project"
                }
            },
        )
        self.assertEqual(result["project_root"], str(self.a))
        self.assertEqual(result["root_source"], "operator_override")

    async def test_single_standard_client_root_is_supported_without_codex(self):
        client = await self.client(roots=[Root(uri=self.a.as_uri())])
        result = await self.call(client, "tandem_scope")
        self.assertEqual(result["project_root"], str(self.a))
        self.assertEqual(result["root_source"], "client_roots")

    async def test_multiple_unlabelled_client_roots_are_not_guessed(self):
        client = await self.client(
            roots=[Root(uri=self.a.as_uri()), Root(uri=self.b.as_uri())]
        )
        with self.assertRaises(ToolError):
            await self.call(client, "tandem_scope")
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
