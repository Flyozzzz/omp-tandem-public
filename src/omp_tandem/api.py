"""MCP tool surface and client-granted workspace access."""

# Eager annotations preserve model types in FastMCP's Context-injection wrappers.
import asyncio
import time
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated, Literal

from fastmcp import Context
from pydantic import Field

from .binding import BridgeBinding, RuntimeOptions
from .bridge import Bridge
from .channel import ChannelFastMCP
from .models import ArtifactInfo, TaskContract, TurnContract
from .project_context import ProjectContext
from .prompts import INSTRUCTIONS
from .runtime_models import ACTIVE, Mode, TaskSummary
from .workspace import client_root_paths


async def granted_roots(ctx: Context):
    params = ctx.session.client_params
    if params is None or params.capabilities.roots is None:
        return ()
    # Re-read grants on each new turn, including after a client revokes a root.
    async with asyncio.timeout(5):
        return client_root_paths(await ctx.list_roots())


def build_server(configuration: Bridge | RuntimeOptions):
    runtime = BridgeBinding(configuration)

    @asynccontextmanager
    async def lifespan(_server):
        await runtime.start()
        try:
            yield {}
        finally:
            await runtime.close()

    mcp = ChannelFastMCP(
        "omp-tandem",
        version=version("omp-tandem"),
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        binding=runtime,
    )

    @mcp.tool()
    async def tandem_scope(ctx: Context) -> dict:
        """Inspect this MCP instance's immutable launch-project boundary and legacy import status.

        Separate launch folders have separate data even with one user-wide MCP registration.
        Additional client-granted directories permit working there, not reading their MCP history.
        This is data isolation, not an OS filesystem sandbox.
        """
        bridge = await runtime.get(ctx)
        return {**bridge.scope.info(), "migration": bridge.migration}

    @mcp.tool()
    async def tandem_start(
        cwd: str,
        ctx: Context,
        prompt: str | None = None,
        contract: TaskContract | None = None,
        mode: Mode = "analyze",
        timeout_seconds: Annotated[int, Field(ge=1, le=7200)] = 1800,
        project_context_id: str | None = None,
        question_timeout_seconds: Annotated[int, Field(ge=1, le=1800)] = 300,
    ) -> dict:
        """Start a task with exactly one of prompt or structured contract. Returns immediately.

        Base constraints/owned files stay fixed; each follow-up has a new goal and criteria.
        project_context_id pins product rules/decisions. Only the coordinator sets question timeout.
        Think: collaboration; analyze: read/search; work: edits/shell, NOT sandboxed.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        result = await asyncio.to_thread(
            bridge.start,
            prompt,
            cwd,
            mode,
            timeout_seconds=timeout_seconds,
            contract=contract,
            project_context_id=project_context_id,
            question_timeout_seconds=question_timeout_seconds,
            granted_roots=await granted_roots(ctx),
        )
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_continue(
        conversation_id: str,
        ctx: Context,
        prompt: str | None = None,
        contract: TurnContract | None = None,
        timeout_seconds: Annotated[int, Field(ge=1, le=7200)] = 1800,
        project_context_id: str | None = None,
        question_timeout_seconds: Annotated[int | None, Field(ge=1, le=1800)] = None,
    ) -> dict:
        """Start a NEW current goal after completion; supply exactly one of prompt or turn contract.

        History/base policy/mode/cwd persist, but old goals/acceptance do not. The product snapshot
        stays pinned unless explicitly changed to another revision of the same project.
        Question timeout is inherited unless explicitly set here. For waiting_input use tandem_reply.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        result = await asyncio.to_thread(
            bridge.start,
            prompt,
            conversation_id=conversation_id,
            timeout_seconds=timeout_seconds,
            contract=contract,
            project_context_id=project_context_id,
            question_timeout_seconds=question_timeout_seconds,
            granted_roots=await granted_roots(ctx),
        )
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_result(
        task_id: str,
        ctx: Context,
        wait_seconds: Annotated[int, Field(ge=0, le=25)] = 0,
        details: bool = False,
    ) -> dict:
        """Get the actual answer, work outcome and next_action. Questions return immediately.

        wait_seconds: 0..25. completed is turn completion, not proof of success.
        answer holds the requested text; summary only describes the work. When answer_truncated,
        read answer_artifact_id. details=true includes the full answer/report/contract/diagnostics.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await asyncio.to_thread(bridge.view, task_id, details)
            if (
                result["status"] == "waiting_input"
                or result["status"] not in ACTIVE
                or bridge.channel.confirmed
                or time.monotonic() >= deadline
            ):
                await asyncio.to_thread(
                    bridge.channel.seen,
                    task_id,
                    result["status"],
                    result.get("question", {}).get("question_id"),
                )
                return bridge.channel.decorate(result)
            await asyncio.sleep(0.15)

    @mcp.tool()
    async def tandem_reply(
        task_id: str, question_id: str, answer: str, ctx: Context
    ) -> dict:
        """Answer exactly the pending clarification, resuming the same worker. Expired/stale IDs fail.

        Duplicate identical answers are idempotent; different second answers are rejected.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        result = await asyncio.to_thread(bridge.reply, task_id, question_id, answer)
        await asyncio.to_thread(
            bridge.channel.seen, task_id, "waiting_input", question_id
        )
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_cancel(task_id: str, ctx: Context) -> dict:
        """Stop a task, including while it waits for clarification. Does not undo edits."""
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        return bridge.channel.decorate(await asyncio.to_thread(bridge.cancel, task_id))

    @mcp.tool()
    async def tandem_list(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[TaskSummary]:
        """List recent task/conversation IDs, statuses, outcomes and next actions without large bodies."""
        bridge = await runtime.get(ctx)
        return await asyncio.to_thread(bridge.recent, limit)

    @mcp.tool()
    async def tandem_publish_artifact(
        conversation_id: str,
        name: str,
        content: str,
        ctx: Context,
        media_type: Literal[
            "text/plain", "text/markdown", "application/json"
        ] = "text/plain",
    ) -> ArtifactInfo:
        """Publish immutable shared context; each same-name publication creates a new version.

        Return artifact_id to the partner in a follow-up or contract.artifact_ids. Content max 4MiB UTF8.
        Logical names are not filesystem paths; no file is read or modified by this tool.
        """
        bridge = await runtime.get(ctx)
        return ArtifactInfo.model_validate(
            await asyncio.to_thread(
                bridge.publish, conversation_id, name, content, media_type
            )
        )

    @mcp.tool()
    async def tandem_read_artifact(
        artifact_id: str,
        ctx: Context,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=50000)] = 16000,
    ) -> dict:
        """Read an immutable artifact version shared by the coordinator and OMP; page using next_offset.

        Offsets count Unicode characters; limit 1..50000. IDs never resolve arbitrary local files.
        """
        bridge = await runtime.get(ctx)
        return await asyncio.to_thread(
            bridge.artifacts.read, artifact_id, offset, limit
        )

    @mcp.tool()
    async def tandem_wait(
        task_ids: Annotated[list[str], Field(min_length=1, max_length=32)],
        ctx: Context,
        wait_seconds: Annotated[int, Field(ge=0, le=25)] = 20,
    ) -> dict:
        """Wait for ANY selected task to finish or ask a question, not 25s per task.

        Returns ready IDs/questions, not full answers. Read ready terminal results with tandem_result
        and remove them from later wait sets; reply directly to a returned question. Does not
        acknowledge terminal events or rerun work. Confirmed push returns await_event immediately.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await asyncio.to_thread(bridge.wait_snapshot, task_ids)
            if (
                result["ready"]
                or result["delivery"] == "push"
                or time.monotonic() >= deadline
            ):
                return result
            await asyncio.sleep(0.15)

    @mcp.tool()
    async def tandem_project_context(
        ctx: Context,
        action: Literal["publish", "get", "list"],
        context: ProjectContext | None = None,
        context_id: str | None = None,
        project_id: str | None = None,
        expected_revision: Annotated[int | None, Field(ge=0)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict:
        """Coordinator-only product knowledge: source-backed rules, examples and settled decisions.

        publish creates an immutable snapshot; updating a project requires its current expected_revision.
        get uses an exact context_id; list optionally filters project_id. Pass context_id to
        tandem_start/continue explicitly. Publishing never changes active tasks. Workers may propose
        changes in their answer but do not get this publishing tool. Do not approve invented rules.
        """
        bridge = await runtime.get(ctx)
        if action == "publish":
            if context is None:
                raise ValueError("publish requires context")
            for identifier in (
                *context.artifact_ids,
                *(
                    identifier
                    for decision in context.decisions
                    for identifier in decision.evidence_artifact_ids
                ),
            ):
                await asyncio.to_thread(bridge.artifacts.info, identifier)
            return await asyncio.to_thread(
                bridge.projects.publish,
                context,
                expected_revision,
                publisher="coordinator:" + bridge.channel.owner,
            )
        if action == "get":
            if context_id is None:
                raise ValueError("get requires context_id")
            return await asyncio.to_thread(bridge.projects.get, context_id)
        return {
            "contexts": await asyncio.to_thread(bridge.projects.list, project_id, limit)
        }

    @mcp.tool()
    async def tandem_export_context(
        context_id: str, target_project_root: str, ctx: Context
    ) -> dict:
        """Explicitly offer this project's selected rules and referenced evidence to one recipient.

        Use only when the user intends cross-project sharing. No tasks/history are shared.
        The returned transfer_id is a private capability, not a public link. The recipient must
        separately call tandem_import_context in its own coordinator session. This grants no permissions.
        """
        bridge = await runtime.get(ctx)
        return await asyncio.to_thread(
            bridge.transfers.export, context_id, target_project_root
        )

    @mcp.tool()
    async def tandem_import_context(
        transfer_id: str,
        ctx: Context,
        expected_revision: Annotated[int | None, Field(ge=0)] = None,
    ) -> dict:
        """Explicitly accept a product snapshot offered to this launch project.

        Only recipient-bound exports can be imported; no foreign database is browsed.
        Creates fresh local context/evidence IDs, preserving source provenance, not granting approval.
        Existing product revisions require expected_revision. Retrying one transfer is idempotent.
        """
        bridge = await runtime.get(ctx)
        return await asyncio.to_thread(
            bridge.transfers.import_context,
            transfer_id,
            expected_revision,
            publisher="coordinator:" + bridge.channel.owner,
        )

    @mcp.tool()
    async def tandem_channel(
        ctx: Context,
        action: Literal["status", "probe", "ack", "pending", "recover"] = "status",
        event_id: str | None = None,
        probe_token: str | None = None,
        task_id: str | None = None,
        include_previous: bool = False,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Manage optional Claude Code Channels push delivery; ordinary polling always remains available.

        probe sends a receipt challenge only through the channel; ack with its probe_token confirms
        delivery. ack with event_id acknowledges a webhook. pending lists unacknowledged events
        (include_previous explicitly includes earlier sessions). recover replays one event_id or a
        completed task_id; never changes task outcomes or reruns work. Token-file contents are secret.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx, auto_probe=False)
        if action == "status":
            return bridge.channel.status()
        if action == "probe":
            return await bridge.channel.probe(resend=True)
        if action == "ack":
            if (event_id is None) == (probe_token is None):
                raise ValueError("ack requires exactly one of event_id or probe_token")
            if probe_token is not None:
                return await bridge.channel.confirm(probe_token)
            changed = await asyncio.to_thread(
                bridge.channel.store.acknowledge, bridge.channel.owner, event_id
            )
            return {
                "acknowledged": True,
                "new_acknowledgment": changed,
                "event_id": event_id,
            }
        if action == "pending":
            events = await asyncio.to_thread(
                bridge.channel.store.pending,
                None if include_previous else bridge.channel.owner,
                limit,
            )
            return {"events": events, "delivery": bridge.channel.status()["delivery"]}
        if (event_id is None) == (task_id is None):
            raise ValueError("recover requires exactly one of event_id or task_id")
        if event_id is not None:
            event = await asyncio.to_thread(bridge.channel.store.get, event_id)
            task_id = event["task_id"]
        if task_id is not None:
            task = await asyncio.to_thread(bridge.tasks.get, task_id)
            if task["status"] in ACTIVE:
                raise ValueError(
                    "Cannot recover events of an active task; use its owning session"
                )
        if event_id is not None:
            recovered = await asyncio.to_thread(
                bridge.channel.store.adopt_event, event_id, bridge.channel.owner
            )
            result = {"recovered": 1, "event_id": recovered["event_id"]}
        else:
            count = await asyncio.to_thread(
                bridge.channel.store.adopt, task_id, bridge.channel.owner
            )
            result = {"recovered": count, "task_id": task_id}
        bridge.channel.signal()
        return {**result, "delivery": bridge.channel.status()["delivery"]}

    return mcp
