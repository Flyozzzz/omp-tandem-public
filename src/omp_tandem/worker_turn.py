"""Wait for one exclusive worker turn without consuming RPC event history."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from omp_rpc import (
    AgentEndEvent,
    AgentMessage,
    MessageEndEvent,
    RpcClient,
    RpcError,
    UnknownNotification,
    assistant_text,
)

LIVENESS_INTERVAL = 5.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnResult:
    assistant_message: dict | None
    assistant_text: str


class TurnCancelled(Exception):
    """The caller requested cancellation; it remains responsible for client.stop()."""


def wait_for_turn(
    client: RpcClient,
    message: str,
    *,
    timeout: float,
    cancelled: Callable[[], bool],
) -> TurnResult:
    """Send exactly one prompt and await its acknowledgment and terminal agent_end.

    The caller owns an already-started client exclusively for this turn and must
    stop it on every exit. No retained events are read. Only the latest assistant
    message and latest nonempty visible answer are retained; a thinking-only or
    error message cannot erase an earlier visible answer or hide its stopReason.

    The SDK has no public process-exit callback. A daemon issues get_state every
    five seconds, without treating idle state as completion. Detection normally
    takes at most one polling interval plus SDK EOF processing (up to a second),
    but an unresponsive transport can take the client's configured request_timeout
    as well. All request failures propagate. Cancellation and the outer deadline
    are checked at most 50ms apart regardless of blocked prompt/state requests.
    On return the monitor is signalled to stop; client.stop() releases any RPC
    already in flight. Neither daemon is joined here because RPC may be blocked.
    """
    deadline = time.monotonic() + timeout
    condition = threading.Condition()
    stopped = threading.Event()
    latest_message: dict | None = None
    latest_text = ""
    acknowledged = False
    terminal = False
    failure: Exception | None = None
    unsubscribe: list[Callable[[], None]] = []

    def remember(value: AgentMessage) -> None:
        nonlocal latest_message, latest_text
        if value.get("role") == "assistant":
            latest_message = value
            text = assistant_text(value)
            if text:
                latest_text = text

    def message_ended(event: MessageEndEvent) -> None:
        with condition:
            if not stopped.is_set() and not terminal:
                remember(event.message)

    def agent_ended(event: AgentEndEvent) -> None:
        nonlocal terminal
        with condition:
            if stopped.is_set() or terminal:
                return
            for value in event.messages:
                remember(value)
            if event.is_terminal is not False:
                terminal = True
                condition.notify_all()

    def failed(error: Exception) -> None:
        nonlocal failure
        with condition:
            if not stopped.is_set() and failure is None:
                failure = error
                condition.notify_all()

    def unknown(event: UnknownNotification) -> None:
        if event.parse_error is not None:
            failed(
                RpcError(
                    f"Invalid RPC {event.payload.get('type')}: {event.parse_error}"
                )
            )

    def send_prompt() -> None:
        nonlocal acknowledged
        if stopped.is_set():
            return
        try:
            client.prompt(message)
        except Exception as exc:
            logger.debug("Prompt request failed", exc_info=True)
            failed(exc)
        else:
            with condition:
                acknowledged = True
                condition.notify_all()

    def monitor() -> None:
        while not stopped.wait(LIVENESS_INTERVAL):
            try:
                client.get_state()
            except Exception as exc:
                logger.debug("Worker liveness request failed", exc_info=True)
                failed(exc)
                return

    def check_budget() -> float:
        if cancelled():
            raise TurnCancelled("Worker turn cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Task budget exhausted; partial edits may remain")
        return remaining

    try:
        check_budget()
        unsubscribe.append(client.on_message_end(message_ended))
        unsubscribe.append(client.on_agent_end(agent_ended))
        unsubscribe.append(client.on_protocol_error(failed))
        unsubscribe.append(client.on_unknown_notification(unknown))
        check_budget()
        threading.Thread(
            target=send_prompt, name="tandem-prompt-ack", daemon=True
        ).start()
        threading.Thread(
            target=monitor, name="tandem-turn-monitor", daemon=True
        ).start()
        with condition:
            while True:
                remaining = check_budget()
                if failure is not None:
                    raise failure
                if acknowledged and terminal:
                    return TurnResult(latest_message, latest_text)
                condition.wait(min(remaining, 0.05))
    finally:
        with condition:
            stopped.set()
        for remove in reversed(unsubscribe):
            remove()
