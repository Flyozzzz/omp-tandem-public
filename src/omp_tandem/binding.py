"""Bind MCP data to client-origin workspace information, never tool arguments."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .bridge import Bridge
from .work_notifications import WorkNotifications
from .workspace import client_root_paths

CODEX_SCOPE_CAPABILITY = "codex/sandbox-state-meta"


def codex_workspace(context) -> Path | None:
    request = context.request_context
    metadata = request.meta if request is not None else None
    state = (
        metadata.get(CODEX_SCOPE_CAPABILITY)
        if isinstance(metadata, dict)
        else getattr(metadata, CODEX_SCOPE_CAPABILITY, None)
    )
    if state is None:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("sandboxCwd"), str):
        raise TypeError("Invalid client workspace metadata")
    uri = urlsplit(state["sandboxCwd"])
    if (
        uri.scheme != "file"
        or uri.netloc not in ("", "localhost")
        or uri.query
        or uri.fragment
    ):
        raise ValueError("Client workspace must be a local file URI")
    path = Path(unquote(uri.path, errors="strict"))
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Client workspace is unavailable on this MCP host")
    return path.resolve()


@dataclass(frozen=True)
class RuntimeOptions:
    state_dir: Path
    executable: str
    model: str | None = None
    project_root: Path | None = None
    channel_enabled: bool = True
    webhook_enabled: bool = True
    webhook_port: int = 0
    migrate_legacy: bool = True
    work_participant: str = "claude"
    work_token_file: Path | None = None


class BridgeBinding:
    def __init__(self, options: RuntimeOptions | Bridge):
        self.options = options if isinstance(options, RuntimeOptions) else None
        self.bridge = options if isinstance(options, Bridge) else None
        self.launch_cwd = Path.cwd().resolve()
        self.claude_root = os.environ.get("CLAUDE_PROJECT_DIR")
        self.source = "operator_override" if self.bridge is not None else None
        self.plugin_context = any(
            os.environ.get(name)
            for name in (
                "PLUGIN_ROOT",
                "PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA",
            )
        )
        self.lock = asyncio.Lock()
        self.started = False
        self.closed = False
        self.work_notifications = None

    async def start(self):
        if self.bridge is not None and not self.started:
            await self.bridge.channel.start()
            self.started = True
            self.work_notifications = WorkNotifications(self.bridge)
            await self.work_notifications.start()

    async def _create(self, root: Path, source: str):
        options = self.options
        bridge = await asyncio.to_thread(
            Bridge,
            options.state_dir,
            options.executable,
            options.model,
            project_root=root,
            project_source=source,
            channel_enabled=options.channel_enabled,
            webhook_enabled=options.webhook_enabled,
            webhook_port=options.webhook_port,
            migrate_legacy=options.migrate_legacy,
            work_participant=options.work_participant,
            work_token_file=options.work_token_file,
        )
        self.bridge = bridge
        self.source = source
        await self.start()
        return bridge

    def _configured_root(self):
        if self.options is not None and self.options.project_root is not None:
            return self.options.project_root, "operator_override"
        if self.claude_root is not None:
            path = Path(self.claude_root)
            if not self.claude_root or not path.is_absolute():
                raise ValueError("CLAUDE_PROJECT_DIR must be an absolute directory")
            return path, "claude_project_dir"
        return None

    async def discover(self, session):
        """Preserve Claude's startup channel probe without guessing a Codex workspace."""
        params = session.client_params
        is_claude = params is not None and "claude" in params.clientInfo.name.lower()
        async with self.lock:
            if self.closed:
                return
            if self.bridge is None and is_claude:
                configured = self._configured_root()
                if configured is not None:
                    await self._create(*configured)
                elif not self.plugin_context and params.capabilities.roots is None:
                    await self._create(self.launch_cwd, "launch_cwd")
            if self.bridge is not None:
                await self.bridge.channel.bind_session(session, discovery=True)

    async def get(self, context) -> Bridge:
        async with self.lock:
            if self.closed:
                raise RuntimeError("MCP workspace is closing")
            if self.bridge is not None and self.source == "operator_override":
                return self.bridge
            if self.options.project_root is not None:
                return await self._create(
                    self.options.project_root, "operator_override"
                )
            supplied = codex_workspace(context)
            if self.bridge is not None:
                if (
                    self.source == "codex_request"
                    and supplied != self.bridge.scope.root
                ):
                    raise ValueError(
                        "Client workspace changed or is missing; reconnect MCP for the intended project"
                    )
                if supplied is not None and supplied != self.bridge.scope.root:
                    raise ValueError(
                        "Client workspace conflicts with the bound project"
                    )
                return self.bridge
            if supplied is not None:
                return await self._create(supplied, "codex_request")
            params = context.session.client_params
            if params is not None and "codex" in params.clientInfo.name.lower():
                raise ValueError(
                    "Codex workspace metadata is missing; use a supported client or explicit --project-root"
                )
            configured = self._configured_root()
            if configured is not None:
                return await self._create(*configured)
            if params is not None and params.capabilities.roots is not None:
                async with asyncio.timeout(5):
                    roots = client_root_paths(await context.list_roots())
                if len(roots) == 1:
                    return await self._create(roots[0], "client_roots")
                if len(roots) > 1:
                    raise ValueError(
                        "Multiple client roots need an explicit --project-root; no primary root will be guessed"
                    )
            if self.plugin_context:
                raise ValueError(
                    "Plugin directory is not a project; configure trusted workspace metadata or --project-root"
                )
            return await self._create(self.launch_cwd, "launch_cwd")

    async def close(self):
        async with self.lock:
            self.closed = True
            if self.work_notifications is not None:
                await self.work_notifications.close()
            if self.bridge is not None:
                try:
                    await asyncio.to_thread(self.bridge.shutdown)
                finally:
                    await self.bridge.channel.close()
