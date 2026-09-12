"""MCP tool surface and client-granted workspace access."""

# Eager annotations preserve model types in FastMCP's Context-injection wrappers.
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastmcp import Context
from pydantic import Field

from .binding import BridgeBinding, RuntimeOptions
from .bridge import Bridge
from .channel import ChannelFastMCP
from .execution import ExecutionOptions, profile_catalog
from .findings import FindingChange, FindingDraft
from .models import ArtifactInfo, TaskContract, TurnContract
from .project_context import ProjectContext
from .prompts import coordinator_instructions
from .reviews import PublicationBusy, ReviewRequest, publication_lock
from .runtime_identity import register_schemas, runtime_identity
from .runtime_models import ACTIVE, Mode, TaskSummary
from .work_items import WorkCommand, WorkPresentation
from .workspace import client_root_paths


async def granted_roots(ctx: Context):
    params = ctx.session.client_params
    if params is None or params.capabilities.roots is None:
        return ()
    # Re-read grants on each new turn, including after a client revokes a root.
    async with asyncio.timeout(5):
        return client_root_paths(await ctx.list_roots())


def _review_seen(bridge, observations):
    # Notification acknowledgements must not turn a responsive status/cancel
    # into a wait behind snapshot publication. Later observations can ack again.
    try:
        with publication_lock(bridge.reviews.scope, blocking=False):
            for task_id, status, question_id in observations:
                bridge.channel.seen(task_id, status, question_id)
    except PublicationBusy:
        pass


def build_server(configuration: Bridge | RuntimeOptions):
    runtime = BridgeBinding(configuration)
    channels_enabled = (
        configuration.channel.enabled
        if isinstance(configuration, Bridge)
        else configuration.channel_enabled
    )
    restricted_work = bool(
        configuration.work_token
        if isinstance(configuration, Bridge)
        else configuration.work_token_file
    )

    @asynccontextmanager
    async def lifespan(_server):
        await runtime.start()
        tools = await _server.get_tools()
        register_schemas("mcp", {name: tool.parameters for name, tool in tools.items()})
        try:
            yield {}
        finally:
            await runtime.close()

    mcp = ChannelFastMCP(
        "omp-tandem",
        version=runtime_identity()["package_version"],
        instructions=coordinator_instructions(channels_enabled),
        lifespan=lifespan,
        binding=runtime,
    )

    @mcp.tool()
    async def tandem_work(
        request: WorkCommand,
        ctx: Context,
        wait_seconds: Annotated[int, Field(ge=0, le=25)] = 0,
        view: Literal["summary", "plan", "step", "full"] = "summary",
        format: Literal["json", "markdown"] = "json",
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: str | None = None,
        include_snapshots: bool = False,
    ) -> dict:
        """Maintain one durable shared task and its role-bound checklist.

        To create, send action="create", expected_revision=0, a unique operation_id,
        and plan; omit work_id. The plan must have one final integration step that
        depends directly or transitively on every other step, including investigations.
        For later mutations, get the card first and use its current revision as
        expected_revision. Use a new operation_id for a new or corrected request;
        reuse an ID only for an exact retry. list/get/history need no operation_id.
        Plan agreement, worker submission and independent acceptance are separate.
        Waiting observes committed changes, not permission to start an agent.
        Autonomous grants and uncertain-attempt reconciliation are operator CLI actions.
        """
        bridge = await runtime.get(ctx)
        if not restricted_work:
            await bridge.channel.bind(ctx)
        options = WorkPresentation(
            view=view,
            format=format,
            limit=limit,
            cursor=cursor,
            include_snapshots=include_snapshots,
        )
        if wait_seconds and request.action == "get" and request.work_id:
            state = await asyncio.to_thread(bridge.work_observation, request)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
                observed = await asyncio.to_thread(
                    bridge.work_items.progress, request.work_id
                )
                if observed != state:
                    break
        result = await asyncio.to_thread(bridge.work, request, presentation=options)
        # Each session observes the same durable card; channel delivery only hints.
        result["delivery"] = bridge.channel.delivery
        result["delivery_instructions"] = (
            "Read current shared-task state after a wake. Use bounded tandem_work get "
            "waiting when idle; do not replay claims or launches. Explicit pause remains sticky."
        )
        return result

    if restricted_work:

        @mcp.tool()
        async def tandem_review_read(
            ctx: Context,
            section: Literal[
                "manifest",
                "requirements",
                "criteria",
                "diff",
                "selected",
                "base",
                "staged",
                "checks",
                "author",
            ] = "manifest",
            path: str | None = None,
            offset: Annotated[int, Field(ge=0)] = 0,
            limit: Annotated[int, Field(ge=1, le=50000)] = 16000,
        ) -> dict:
            """Read the immutable snapshot pinned to this review attempt, never live files.

            The author section opens only after the attempt's independent report was
            recorded and comparison was opened through tandem_work; the stage is taken
            from the stored attempt, not from a request argument.
            """
            bridge = await runtime.get(ctx)
            attempt = await asyncio.to_thread(
                bridge.work_items.authenticate, bridge.work_token
            )
            review_id = attempt.get("review_id")
            if attempt.get("kind") != "review" or not review_id:
                raise ValueError("This attempt has no pinned review snapshot")
            reveal = bool(attempt.get("comparison_opened_at"))
            return await asyncio.to_thread(
                bridge.reviews.read,
                review_id,
                section=section,
                path=path,
                offset=offset,
                limit=limit,
                reveal_author=reveal,
            )

        return mcp

    @mcp.tool()
    async def tandem_scope(ctx: Context) -> dict:
        """Inspect the immutable project boundary and available computation profile defaults.

        Separate launch folders have separate data even with one user-wide MCP registration.
        Additional client-granted directories permit working there, not reading their MCP history.
        This is data isolation, not an OS filesystem sandbox. execution_profiles describes defaults,
        not effective/actual settings after overrides; deep means more time, not higher thinking.
        """
        bridge = await runtime.get(ctx)
        return bridge.channel.decorate(
            {
                **bridge.scope.info(),
                "migration": bridge.migration,
                "execution_profiles": profile_catalog(),
                "runtime_identity": runtime_identity(),
                "host_owner": bridge.channel.owner,
            }
        )

    @mcp.tool()
    async def tandem_start(
        cwd: str,
        ctx: Context,
        prompt: str | None = None,
        contract: TaskContract | None = None,
        mode: Mode = "analyze",
        timeout_seconds: Annotated[int | None, Field(ge=1, le=7200)] = None,
        project_context_id: str | None = None,
        question_timeout_seconds: Annotated[int, Field(ge=1, le=1800)] = 300,
        execution: ExecutionOptions | None = None,
        review_id: str | None = None,
        review_stage: Literal["independent", "comparison"] | None = None,
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
            execution=execution,
            review_id=review_id,
            review_stage=review_stage,
        )
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_continue(
        conversation_id: str,
        ctx: Context,
        prompt: str | None = None,
        contract: TurnContract | None = None,
        timeout_seconds: Annotated[int | None, Field(ge=1, le=7200)] = None,
        project_context_id: str | None = None,
        question_timeout_seconds: Annotated[int | None, Field(ge=1, le=1800)] = None,
        execution: ExecutionOptions | None = None,
        review_id: str | None = None,
        review_stage: Literal["independent", "comparison"] | None = None,
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
            execution=execution,
            review_id=review_id,
            review_stage=review_stage,
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
            task = await asyncio.to_thread(bridge.tasks.get, task_id)
            if (
                task["status"] == "waiting_input"
                or task["status"] not in ACTIVE
                or bridge.channel.can_await([task_id])
                or time.monotonic() >= deadline
            ):
                result = await asyncio.to_thread(bridge.view, task_id, details)
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
        acknowledge terminal events or rerun work. Await events only with a live independent watchdog.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        deadline = time.monotonic() + wait_seconds
        while True:
            result = await asyncio.to_thread(bridge.wait_snapshot, task_ids)
            if (
                result["ready"]
                or bridge.channel.can_await(result["pending"])
                or time.monotonic() >= deadline
            ):
                return bridge.channel.decorate(result)
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
        watchdog_token: str | None = None,
        task_id: str | None = None,
        include_previous: bool = False,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Manage optional Claude Code Channels push delivery; ordinary polling always remains available.

        probe sends a receipt challenge only through the channel; ack with its probe_token confirms
        delivery. watchdog_token acknowledges an actual independent hook wake, never a tool-response
        assertion. ack with event_id acknowledges a webhook. pending lists unacknowledged events
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
            if (
                sum(
                    value is not None
                    for value in (event_id, probe_token, watchdog_token)
                )
                != 1
            ):
                raise ValueError(
                    "ack requires exactly one of event_id, probe_token or watchdog_token"
                )
            if watchdog_token is not None:
                return await bridge.channel.confirm_watchdog(watchdog_token)
            if probe_token is not None:
                return await bridge.channel.confirm(probe_token)
            changed = await asyncio.to_thread(
                bridge.channel.store.acknowledge, bridge.channel.owner, event_id
            )
            return bridge.channel.decorate(
                {
                    "acknowledged": True,
                    "new_acknowledgment": changed,
                    "event_id": event_id,
                }
            )
        if action == "pending":
            events = await asyncio.to_thread(
                bridge.channel.store.pending,
                None if include_previous else bridge.channel.owner,
                limit,
            )
            return bridge.channel.decorate({"events": events})
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
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_review(
        ctx: Context,
        action: Literal["create", "read", "assess"],
        request: ReviewRequest | None = None,
        review_id: str | None = None,
        section: Literal[
            "manifest",
            "requirements",
            "criteria",
            "diff",
            "selected",
            "base",
            "staged",
            "checks",
            "author",
        ] = "manifest",
        path: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=50000)] = 16000,
        reveal_author: bool = False,
    ) -> dict:
        """Capture or read an immutable review bundle, or compare it with current selected files.

        request.source selects base-to-worktree (default) or base-to-staged Git index material.
        Staged captures exclude unstaged/untracked content; source also governs applicability checks.
        Capture includes requirements, supplied checks and boundaries without executing tests. Bind review_id
        to a think task for snapshot-only review. Author rationale is withheld unless explicitly
        revealed in a comparison turn. Read pages by next_offset, not the live working directory.
        assess reports applicability at observation time, not whole-system correctness.
        """
        bridge = await runtime.get(ctx)
        if action == "create":
            if request is None or review_id is not None:
                raise ValueError("create requires request and no review_id")
            return await asyncio.to_thread(bridge.reviews.create, request)
        if review_id is None or request is not None:
            raise ValueError("read/assess require review_id and no request")
        if action == "assess":
            return await asyncio.to_thread(bridge.reviews.assess, review_id)
        return await asyncio.to_thread(
            bridge.reviews.read,
            review_id,
            section=section,
            path=path,
            offset=offset,
            limit=limit,
            reveal_author=reveal_author,
        )

    @mcp.tool()
    async def tandem_review_run(
        ctx: Context,
        action: Literal["start", "status", "reply", "cancel"],
        request_key: Annotated[
            str | None, Field(min_length=1, max_length=200, pattern=r"\S")
        ] = None,
        request: ReviewRequest | None = None,
        run_id: str | None = None,
        execution: ExecutionOptions | None = None,
        budget_seconds: Annotated[int | None, Field(ge=10, le=7200)] = None,
        compare: bool | None = None,
        wait_seconds: Annotated[int, Field(ge=0, le=25)] = 25,
        question_id: str | None = None,
        answer: Annotated[str | None, Field(min_length=1, max_length=60000)] = None,
    ) -> dict:
        """Run one read-only review scenario without manually coordinating its native turns.

        start: supply ReviewRequest and a stable request_key for this logical request. Reusing
        the key returns the same run; a different payload conflicts. Code captures the snapshot,
        runs independent think review, then at most one comparison if author material was supplied
        and the independent report succeeded. Total budget defaults to 600 seconds, including
        startup, both stages and questions. It never edits files or runs supplied test commands.
        status/reply/cancel: use the returned run_id, never start again to wait. Replies require
        the current question_id. Full stage answers, findings, applicability and peer-only usage
        are assembled by code. Claims are not automatically accepted or applied. Missing context
        needs an explicit new capture (context_paths), not hidden live reads.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        if action == "start":
            if request is None or request_key is None or run_id is not None:
                raise ValueError(
                    "start requires request_key and request, without run_id"
                )
            if question_id is not None or answer is not None:
                raise ValueError("Question fields are only valid for reply")
            result = await asyncio.to_thread(
                bridge.review_runs.start,
                request_key,
                request,
                execution=execution,
                budget_seconds=600 if budget_seconds is None else budget_seconds,
                compare=True if compare is None else compare,
            )
            run_id = result["run_id"]
        else:
            if run_id is None:
                raise ValueError("status/reply/cancel require run_id")
            if any(
                value is not None
                for value in (
                    request_key,
                    request,
                    execution,
                    budget_seconds,
                    compare,
                )
            ):
                raise ValueError("Creation options are only valid for start")
            if action == "reply":
                if question_id is None or answer is None:
                    raise ValueError("reply requires question_id and answer")
                result = await asyncio.to_thread(
                    bridge.review_runs.reply,
                    run_id,
                    question_id,
                    answer,
                )
                await asyncio.to_thread(
                    _review_seen,
                    bridge,
                    [(result["replied_task_id"], "waiting_input", question_id)],
                )
            elif question_id is not None or answer is not None:
                raise ValueError("Question fields are only valid for reply")
            elif action == "cancel":
                await asyncio.to_thread(bridge.review_runs.cancel, run_id)
        deadline = time.monotonic() + wait_seconds
        while action != "cancel":
            status = await asyncio.to_thread(bridge.review_runs.state, run_id)
            if (
                status == "waiting_input"
                or status not in ACTIVE
                or time.monotonic() >= deadline
            ):
                break
            await asyncio.sleep(0.15)
        result = await asyncio.to_thread(bridge.review_runs.view, run_id)
        await asyncio.to_thread(
            _review_seen,
            bridge,
            [
                (
                    observed["task_id"],
                    observed["status"],
                    observed.get("question", {}).get("question_id"),
                )
                for stage in ("independent", "comparison")
                if (observed := result.get(stage))
            ],
        )
        if result["status"] in ("starting", "running"):
            result["stage_statuses"] = {
                stage: {
                    key: value
                    for key, value in result[stage].items()
                    if key in ("task_id", "status", "outcome")
                }
                if result.get(stage)
                else None
                for stage in ("independent", "comparison")
            }
            for field in ("independent", "comparison", "findings"):
                result.pop(field, None)
            result["full_result_pending"] = True
        return bridge.channel.decorate(result)

    @mcp.tool()
    async def tandem_findings(
        ctx: Context,
        action: Literal["create", "update", "get", "list"],
        conversation_id: str | None = None,
        review_id: str | None = None,
        finding_id: str | None = None,
        number: Annotated[int | None, Field(ge=1)] = None,
        finding: FindingDraft | None = None,
        change: FindingChange | None = None,
        expected_revision: Annotated[int | None, Field(ge=1)] = None,
        task_id: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict:
        """Track version-bound review findings without rewriting their history.

        Validity and resolution are separate. A claimed fix is not verified; verify_fixed needs
        evidence and a completed verification task for its snapshot. Update requires the current
        expected_revision. Get by stable finding_id or conversation_id plus human number.
        """
        bridge = await runtime.get(ctx)
        store = bridge.findings
        if action == "create":
            if conversation_id is None or review_id is None or finding is None:
                raise ValueError(
                    "create requires conversation_id, review_id and finding"
                )
            return await asyncio.to_thread(
                store.create,
                conversation_id,
                review_id,
                finding,
                task_id=task_id,
            )
        if action == "update":
            if finding_id is None or change is None or expected_revision is None:
                raise ValueError(
                    "update requires finding_id, change and expected_revision"
                )
            return await asyncio.to_thread(
                store.update,
                finding_id,
                change,
                expected_revision,
                task_id=task_id,
            )
        if action == "list":
            if task_id is not None:
                if conversation_id is not None or review_id is not None:
                    raise ValueError("List by task_id OR conversation/review filters")
                items = await asyncio.to_thread(
                    store.for_task, task_id, limit=limit, offset=offset
                )
            else:
                items = await asyncio.to_thread(
                    store.list,
                    conversation_id=conversation_id,
                    review_id=review_id,
                    limit=limit,
                    offset=offset,
                )
            return {
                "findings": items,
                "next_offset": offset + len(items) if len(items) == limit else None,
            }
        if finding_id is not None:
            if number is not None or conversation_id is not None:
                raise ValueError("Get by finding_id OR conversation_id and number")
            return await asyncio.to_thread(
                store.get, finding_id, history_offset=offset, history_limit=limit
            )
        if conversation_id is None or number is None:
            raise ValueError("get requires finding_id OR conversation_id and number")
        return await asyncio.to_thread(
            store.get_by_number,
            conversation_id,
            number,
            history_offset=offset,
            history_limit=limit,
        )

    @mcp.tool()
    async def tandem_diagnose(
        ctx: Context,
        live: bool = False,
        task_id: str | None = None,
        expected_project: str | None = None,
        wait_seconds: Annotated[int, Field(ge=0, le=25)] = 25,
        timeout_seconds: Annotated[int, Field(ge=10, le=300)] = 90,
    ) -> dict:
        """Diagnose this client session's project, OMP execution, actual model and delivery.

        live=true starts one short provider request and may incur a charge: use only for a
        user-requested live check. Default checks local state without contacting a provider.
        If still running, inspect the returned task_id instead of starting another check.
        A separate CLI process cannot certify this session's push receipt.
        """
        bridge = await runtime.get(ctx)
        await bridge.channel.bind(ctx)
        return await bridge.diagnostics.run(
            live=live,
            task_id=task_id,
            expected_project=expected_project,
            wait_seconds=wait_seconds,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    async def tandem_receipt(
        ctx: Context,
        task_id: str,
        action: Literal["status", "claim", "complete"] = "status",
        token: str | None = None,
    ) -> dict:
        """Gate result processing separately from reading/acknowledging notification events.

        Claim before applying a terminal result; only an authorized fresh claim may proceed.
        Complete with its token after handling. Duplicate reads do not authorize repeated effects.
        A stranded claim is uncertain, never automatically released: reconcile external state.
        Claims grant no filesystem/external permissions. External effects still need their own
        idempotency/transaction boundary.
        """
        bridge = await runtime.get(ctx)
        if action == "status":
            return await asyncio.to_thread(bridge.receipts.status, task_id)
        if action == "claim":
            return await asyncio.to_thread(
                bridge.receipts.claim,
                task_id,
                bridge.channel.owner,
            )
        if token is None:
            raise ValueError("complete requires the token from the authorized claim")
        return await asyncio.to_thread(
            bridge.receipts.complete,
            task_id,
            bridge.channel.owner,
            token,
        )

    return mcp
