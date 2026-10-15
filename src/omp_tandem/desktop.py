"""Optional, session-owned MCP adapter for the external Desktop QA job ledger.

The configured backend is operator supplied. Desktop interaction belongs to an
already authorized client; this adapter neither launches a GUI nor grants access.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Literal, Self

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream, move_on_after
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import (
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .channel import ChannelDelivery

_REPORT_OUTCOMES = frozenset({"passed", "failed", "blocked"})
_TERMINAL = _REPORT_OUTCOMES | {"cancelled"}
_PROTOCOL = "desktop-qa/2"
_BACKEND_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    sqlite3.Error,
    McpError,
    ExceptionGroup,
    BrokenResourceError,
    ClosedResourceError,
    EndOfStream,
)


class DesktopQAConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str
    args: list[str]
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        return value

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("cwd must be absolute")
        return value


def load_desktop_config(path: str | Path | None) -> DesktopQAConfig | None:
    """Load only an explicitly supplied operator configuration; never discover one."""
    if path is None:
        return None
    return DesktopQAConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


class DesktopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["status", "submit", "get", "cancel", "next", "finish", "list"]
    request_id: str = ""
    app: str = ""
    scenario: str = ""
    expected: str = ""
    launch_instructions: str = ""
    build: str = ""
    job_id: str = ""
    reason: str = ""
    worker_id: str = ""
    claim_id: str = ""
    apps: list[str] = Field(default_factory=list)
    report: dict | None = None
    claim_token: str = Field(default="", repr=False)
    limit: int = Field(default=20, ge=1)
    wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        required = {
            "status": set(),
            "submit": {"request_id", "app", "scenario", "expected"},
            "get": {"job_id"},
            "cancel": {"job_id", "reason"},
            "next": {"worker_id", "claim_id", "apps"},
            "finish": {"job_id", "report", "claim_token"},
            "list": set(),
        }[self.action]
        optional = {
            "submit": {"launch_instructions", "build"},
            "get": {"wait_seconds"},
            "next": {"wait_seconds"},
            "list": {"limit"},
        }.get(self.action, set())
        unexpected = self.model_fields_set - required - optional - {"action"}
        if unexpected:
            raise ValueError(
                f"Fields inappropriate for {self.action}: {', '.join(sorted(unexpected))}"
            )
        for name in required:
            value = getattr(self, name)
            if (
                value is None
                or value == ""
                or value == []
                or (isinstance(value, str) and not value.strip())
            ):
                raise ValueError(f"{name} is required for {self.action}")
        if self.action == "next" and any(not app.strip() for app in self.apps):
            raise ValueError("apps must contain nonblank exact application names")
        if self.action == "finish":
            report = self.report
            outcome = report.get("outcome")
            if not isinstance(outcome, str) or outcome not in _REPORT_OUTCOMES:
                raise ValueError("report.outcome must be passed, failed or blocked")
            observations = report.get("observations")
            if not isinstance(observations, str) or not observations.strip():
                raise ValueError("report.observations must be nonblank")
            for name in ("steps_performed", "evidence"):
                values = report.get(name)
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() for value in values
                ):
                    raise ValueError(
                        f"report.{name} must be an array of nonblank strings"
                    )
            if report["outcome"] != "blocked" and not report["steps_performed"]:
                raise ValueError(
                    "passed and failed reports require actual steps_performed"
                )
        return self


def _decode(result) -> dict | list:
    if result.isError:
        message = "\n".join(item.text for item in result.content if item.type == "text")
        raise RuntimeError(message or "Desktop QA backend rejected the request")
    value = result.structuredContent
    if value is None:
        texts = [item.text for item in result.content if item.type == "text"]
        if len(texts) != 1:
            raise ValueError("Desktop QA backend returned no unambiguous JSON result")
        value = json.loads(texts[0])
    # FastMCP wraps non-object structured results in a result property.
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if not isinstance(value, (dict, list)):
        raise TypeError("Desktop QA backend result must be an object or array")
    return value


class _CancellableSession(ClientSession):
    cancellation_uncertain = False

    async def send_request(self, request, result_type, *args, **kwargs):
        # SDK 1.x does not notify the server when a waiting caller is cancelled.
        # Its allocator increments this value synchronously before the first
        # await, so the id belongs to this request even with concurrent calls.
        request_id = self._request_id
        try:
            return await super().send_request(request, result_type, *args, **kwargs)
        except asyncio.CancelledError:
            # MCP hosts use level cancellation: shield the bounded cleanup so a
            # cancelled waiter cannot remain active and claim unseen work later.
            # Loss after a backend commit still requires reconciliation, not replay.
            delivered = False
            try:
                with move_on_after(2, shield=True):
                    await self.send_notification(
                        ClientNotification(
                            root=CancelledNotification(
                                method="notifications/cancelled",
                                params=CancelledNotificationParams(
                                    requestId=request_id, reason="Caller cancelled"
                                ),
                            )
                        )
                    )
                    delivered = True
            finally:
                if not delivered:
                    self.cancellation_uncertain = True
            raise


class DesktopQA:
    def __init__(
        self, config: DesktopQAConfig | None, scope: str, channel: ChannelDelivery
    ):
        self.config = config
        self.scope = scope
        self.channel = channel
        self._owner: asyncio.Task | None = None
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connection_error: Exception | None = None
        self._backend_status: dict | None = None
        self._calls: set[asyncio.Task] = set()
        self._observers: dict[str, asyncio.Task] = {}
        self._observer_errors: dict[str, Exception] = {}
        self._seen: set[str] = set()
        self._notified: set[str] = set()
        self._publish_requested: set[str] = set()
        self._pending_events: dict[str, tuple[str, str]] = {}
        self._event_errors: dict[str, str] = {}
        self._event_lock = asyncio.Lock()
        self._event_retry: asyncio.Task | None = None
        self._retired_reason: str | None = None
        self._closed = False
        self._closing: asyncio.Task | None = None

    async def _own_connection(self):
        """Enter and exit SDK/AnyIO scopes in this task, never in callers."""
        try:
            parameters = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                cwd=self.config.cwd,
                env=self.config.env,
            )
            async with (
                stdio_client(parameters) as (reader, writer),
                _CancellableSession(reader, writer) as session,
            ):
                await session.initialize()
                status = _decode(
                    await session.call_tool("desktop_qa_status", {"scope": self.scope})
                )
                if not isinstance(status, dict):
                    raise TypeError("Desktop QA status must be an object")
                self._backend_status = status
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except _BACKEND_ERRORS as exc:
            self._connection_error = exc
        finally:
            self._session = None
            self._ready.set()

    async def _connect(self) -> ClientSession:
        if self._closed:
            raise RuntimeError("Desktop QA adapter is closed")
        if self.config is None:
            raise RuntimeError("Desktop QA backend is not configured")
        if self._owner is None:
            self._owner = asyncio.create_task(self._own_connection())
        # Cancelling a caller cannot cancel the shared owner or its initialization.
        await self._ready.wait()
        if self._closed:
            raise RuntimeError("Desktop QA adapter is closed")
        if self._session is None:
            raise RuntimeError(
                "Desktop QA connection unavailable; no operation was automatically replayed"
            ) from self._connection_error
        return self._session

    async def _call(self, name: str, arguments: dict) -> dict | list:
        session = await self._connect()
        if name != "desktop_qa_status" and (
            self._backend_status.get("protocol") != _PROTOCOL
            or self._backend_status.get("waiting_supported") is not True
            or self._backend_status.get("gui_execution") != "client_owned"
        ):
            raise RuntimeError(
                "Desktop QA backend does not support desktop-qa/2 client-owned waiting"
            )
        call = asyncio.create_task(
            session.call_tool(name, {**arguments, "scope": self.scope})
        )
        self._calls.add(call)
        try:
            return _decode(await asyncio.shield(call))
        except asyncio.CancelledError:
            # Forward cancellation once; a level-cancelled host must not keep
            # interrupting the SDK task while it notifies the remote waiter.
            call.cancel()
            with move_on_after(3, shield=True):
                await asyncio.shield(asyncio.gather(call, return_exceptions=True))
            if session.cancellation_uncertain or not call.done():
                self._retired_reason = "cancellation_delivery_uncertain"
                if not self._closed:
                    # Closing the queue transport stops its abandoned waiters.
                    # Already committed claims/effects still need reconciliation.
                    with move_on_after(10, shield=True):
                        await self.close()
            raise
        finally:
            self._calls.discard(call)

    async def _terminal(self, job: dict, *, observed: bool) -> dict:
        key = f"desktop:{job['id']}:{job['status']}"
        if observed:
            self._seen.add(key)
        else:
            self._publish_requested.add(key)
        self._pending_events[key] = (job["id"], job["status"])
        return await self._flush_terminal(key)

    async def _flush_terminal(self, key: str) -> dict:
        async with self._event_lock:
            pending = self._pending_events.get(key)
            if pending is None:
                return {"outcome": "recorded", "reason": None}
            job_id, status = pending
            publish = key in self._publish_requested and key not in self._notified
            acknowledge = key in self._seen
            try:
                result = await asyncio.to_thread(
                    self.channel.store.record_desktop_result,
                    self.channel.owner,
                    job_id,
                    status,
                    publish=publish,
                    acknowledge=acknowledge,
                )
            except _BACKEND_ERRORS as exc:
                # The external report remains authoritative even if the outbox fails.
                self._event_errors[key] = type(exc).__name__
                self._pending_events.pop(key, None)
                return {"outcome": "failed", "reason": type(exc).__name__}
            if result["outcome"] == "deferred":
                self._event_errors[key] = result["reason"]
            else:
                self._event_errors.pop(key, None)
                if publish:
                    self._notified.add(key)
                    self.channel.signal()
                # Seen/publish intent may advance while the short transaction runs.
                if (key not in self._seen or acknowledge) and (
                    key not in self._publish_requested or key in self._notified
                ):
                    self._pending_events.pop(key, None)
            if (
                self._pending_events
                and not self._closed
                and (self._event_retry is None or self._event_retry.done())
            ):
                self._event_retry = asyncio.create_task(self._retry_events())
            return result

    async def _retry_events(self):
        while self._pending_events and not self._closed:
            await asyncio.sleep(0.5)
            for key in tuple(self._pending_events):
                await self._flush_terminal(key)

    @staticmethod
    def _job(value: dict | list, job_id: str | None = None) -> dict:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not value["id"]
            or not isinstance(value.get("status"), str)
            or (job_id is not None and value["id"] != job_id)
        ):
            raise ValueError(
                "Desktop QA backend returned an invalid job identity or status"
            )
        return value

    def _observe(self, job_id: str):
        previous = self._observers.get(job_id)
        if previous is not None and not previous.done():
            return
        self._observer_errors.pop(job_id, None)
        self._observers[job_id] = asyncio.create_task(self._watch(job_id))

    async def _watch(self, job_id: str):
        try:
            job = self._job(
                await self._call(
                    "wait_desktop_test", {"job_id": job_id, "timeout_seconds": None}
                ),
                job_id,
            )
            if job["status"] not in _TERMINAL:
                raise ValueError(
                    "Desktop QA indefinite wait returned a nonterminal job"
                )
            await self._terminal(job, observed=False)
        except _BACKEND_ERRORS as exc:
            # Keep failures inspectable without leaking report content or claim tokens.
            # A later explicit get can reestablish observation; no mutation is replayed.
            self._observer_errors[job_id] = exc

    async def perform(self, request: DesktopRequest) -> dict | list:
        action = request.action
        if action == "status":
            if self.config is None:
                return {"configured": False, "gui_execution": "client_owned"}
            if self._retired_reason:
                return {
                    "configured": True,
                    "connection": "retired",
                    "reason": self._retired_reason,
                    "reconciliation_required": True,
                    "backend_stopped": self._closing is not None
                    and self._closing.done(),
                    "gui_execution": "client_owned",
                }
            status = await self._call("desktop_qa_status", {})
            if not isinstance(status, dict):
                raise ValueError("Desktop QA status must be an object")
            return {
                **status,
                "configured": True,
                "notification_errors": [
                    {
                        "event_key": key,
                        "reason": reason,
                        "deferred": key in self._pending_events,
                    }
                    for key, reason in self._event_errors.items()
                ],
                "observer_errors": [
                    {"job_id": job_id, "error": type(error).__name__}
                    for job_id, error in self._observer_errors.items()
                ],
            }
        if action == "submit":
            job = self._job(
                await self._call(
                    "submit_desktop_test",
                    {
                        name: getattr(request, name)
                        for name in (
                            "request_id",
                            "app",
                            "scenario",
                            "expected",
                            "launch_instructions",
                            "build",
                        )
                    },
                )
            )
            self._observe(job["id"])
            return job
        if action == "get":
            arguments = {"job_id": request.job_id}
            if request.wait_seconds == 0:
                name = "get_desktop_test"
            else:
                name = "wait_desktop_test"
                arguments["timeout_seconds"] = request.wait_seconds
            job = self._job(await self._call(name, arguments), request.job_id)
            if job["status"] in _TERMINAL:
                job["notification_bookkeeping"] = await self._terminal(
                    job, observed=True
                )
            else:
                self._observe(job["id"])
            return job
        if action == "cancel":
            job = self._job(
                await self._call(
                    "cancel_desktop_test",
                    {"job_id": request.job_id, "reason": request.reason},
                ),
                request.job_id,
            )
            if job["status"] != "cancelled":
                raise ValueError("Desktop QA cancellation returned a noncancelled job")
            job["notification_bookkeeping"] = await self._terminal(job, observed=True)
            return job
        if action == "next":
            return await self._call(
                "next_desktop_test",
                {
                    "worker_id": request.worker_id,
                    "claim_id": request.claim_id,
                    "apps": request.apps,
                    "timeout_seconds": request.wait_seconds,
                },
            )
        if action == "finish":
            return await self._call(
                "finish_desktop_test",
                {
                    "job_id": request.job_id,
                    "report": request.report,
                    "claim_token": request.claim_token,
                },
            )
        return await self._call("list_desktop_tests", {"limit": request.limit})

    async def _shutdown(self):
        pending = [*self._observers.values(), *self._calls]
        if self._event_retry is not None:
            pending.append(self._event_retry)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._stop.set()
        if self._owner is not None:
            # Initialization may itself be waiting on a dead backend.
            if not self._ready.is_set():
                self._owner.cancel()
            await asyncio.gather(self._owner, return_exceptions=True)

    async def close(self):
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._closing)
