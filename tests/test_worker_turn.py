"""Deterministic local RPC peers; no providers, SDK patches, or history scans."""

from __future__ import annotations

import logging
import queue
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from omp_rpc import (
    RpcClient,
    RpcCommandError,
    RpcError,
    RpcProcessExitError,
    RpcProtocolError,
    RpcTimeoutError,
)

from omp_tandem.worker_turn import TurnCancelled, TurnResult, wait_for_turn

logger = logging.getLogger(__name__)

PEER = r"""
import json
import os
import sys


def emit(value):
    print(json.dumps(value), flush=True)


def reply(request, **extra):
    emit({"type": "response", "id": request["id"],
          "command": request["type"], "success": True, **extra})


def answer(text, reason="stop"):
    return {"role": "assistant", "content": [{"type": "text", "text": text}],
            "stopReason": reason}


def end(messages=(), **extra):
    emit({"type": "agent_end", "messages": list(messages), **extra})


emit({"type": "ready"})
mode = None
prompt = None
for line in sys.stdin:
    request = json.loads(line)
    command = request["type"]
    if command == "prompt":
        prompt = request
        mode = request["message"]
        if mode == "request_error":
            reply(request, success=False, error="prompt refused")
            continue
        if mode not in ("pending_ack", "terminal_before_ack"):
            reply(request)
        if mode == "overflow":
            emit({"type": "message_end", "message": answer("preserved answer", "toolUse")})
            for index in range(100):
                emit({"type": "turn_start"})
            emit({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "error", "errorMessage": "final failure",
                "content": [{"type": "thinking", "thinking": "secret reasoning"}]}})
            end(isTerminal=True)
        elif mode == "terminal_messages":
            end([
                {"role": "user", "content": "not the answer"},
                {"role": "assistant", "stopReason": "stop", "content": [
                    {"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "visible "},
                    {"type": "redactedThinking", "data": "private"},
                    {"type": "text", "text": "answer"}]},
                {"role": "toolResult", "content": [{"type": "text", "text": "tool output"}]}
            ])
        elif mode == "thinking_only":
            end([{"role": "assistant", "stopReason": "stop", "content": [
                {"type": "thinking", "thinking": "never reveal"}]}])
        elif mode == "phased":
            end([answer("intermediate")], isTerminal=False)
        elif mode == "terminal_before_ack":
            end([answer("complete before ack")], isTerminal=True)
        elif mode == "malformed_terminal":
            emit({"type": "agent_end", "messages": "invalid", "isTerminal": True})
        emit({"type": "agent_start"})
    elif command == "release":
        reply(request)
        if mode == "phased":
            end([answer("final answer")], isTerminal=True)
            end([answer("must not replace completed turn")], isTerminal=True)
        elif mode == "late_error":
            reply(prompt, success=False, error="late scheduling failure")
        elif mode == "terminal_before_ack":
            reply(prompt)
    elif command == "exit":
        reply(request)
        os._exit(7)
    elif command == "get_state":
        if mode == "pending_ack":
            emit({"type": "turn_start"})
        elif mode == "state_error":
            reply(request, success=False, error="state request failed")
        else:
            reply(request, data={"sessionId": "local-fault-peer", "isStreaming": False})
    else:
        reply(request)
"""


class WorkerTurnTests(unittest.TestCase):
    @contextmanager
    def peer(self, *, request_timeout=3):
        with (
            tempfile.TemporaryDirectory(prefix="tandem rpc peer ") as directory,
            patch("omp_tandem.worker_turn.LIVENESS_INTERVAL", 0.1),
        ):
            script = Path(directory) / "fault peer.py"
            script.write_text(PEER)
            client = RpcClient(
                command=[sys.executable, str(script)],
                cwd=directory,
                max_event_history=3,
                startup_timeout=2,
                request_timeout=request_timeout,
            )
            try:
                client.start()
                yield client
            finally:
                client.stop()

    def launch(self, client, message, *, timeout=8, cancelled=lambda: False):
        received = threading.Event()
        remove = client.on_agent_start(lambda event: received.set())
        self.addCleanup(remove)
        results = queue.Queue(maxsize=1)

        def run():
            try:
                results.put(
                    wait_for_turn(client, message, timeout=timeout, cancelled=cancelled)
                )
            except Exception as exc:
                logger.debug("Fault peer turn ended with an exception", exc_info=True)
                results.put(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(received.wait(2), "peer did not receive prompt")
        return results

    def test_history_rollover_preserves_visible_answer_and_final_error(self):
        with self.peer() as client:
            result = wait_for_turn(
                client, "overflow", timeout=3, cancelled=lambda: False
            )
        self.assertEqual(result.assistant_text, "preserved answer")
        self.assertEqual(result.assistant_message["stopReason"], "error")
        self.assertEqual(result.assistant_message["errorMessage"], "final failure")

    def test_terminal_payload_supports_legacy_terminal_and_visible_blocks_only(self):
        with self.peer() as client:
            result = wait_for_turn(
                client, "terminal_messages", timeout=3, cancelled=lambda: False
            )
        self.assertEqual(result.assistant_text, "visible answer")

    def test_thinking_only_message_has_no_visible_answer(self):
        with self.peer() as client:
            result = wait_for_turn(
                client, "thinking_only", timeout=3, cancelled=lambda: False
            )
        self.assertEqual(result.assistant_text, "")

    def test_nonterminal_end_and_idle_state_do_not_complete_turn(self):
        with self.peer() as client:
            results = self.launch(client, "phased")
            self.assertFalse(client.get_state().is_streaming)
            with self.assertRaises(queue.Empty):
                results.get(timeout=0.1)
            client.request_raw("release")
            result = results.get(timeout=2)
        self.assertIsInstance(result, TurnResult)
        self.assertEqual(result.assistant_text, "final answer")

    def test_late_prompt_scheduling_error_after_ack_is_not_lost(self):
        with self.peer() as client:
            results = self.launch(client, "late_error")
            with self.assertRaises(queue.Empty):
                results.get(timeout=0.1)
            client.request_raw("release")
            error = results.get(timeout=2)
        self.assertIsInstance(error, RpcProtocolError)
        self.assertEqual(error.command, "prompt")

    def test_process_exit_after_ack_is_reported_without_terminal_event(self):
        with self.peer() as client:
            results = self.launch(client, "exit_after_ack")
            client.request_raw("exit")
            error = results.get(timeout=6)
        self.assertIsInstance(error, RpcProcessExitError)

    def test_cancellation_while_prompt_and_monitor_ack_are_withheld(self):
        cancel = threading.Event()
        monitor_received = threading.Event()
        with self.peer() as client:
            remove = client.on_turn_start(lambda event: monitor_received.set())
            try:
                results = self.launch(client, "pending_ack", cancelled=cancel.is_set)
                self.assertTrue(
                    monitor_received.wait(2), "monitor did not request state"
                )
                started = time.monotonic()
                cancel.set()
                error = results.get(timeout=0.7)
                self.assertLess(time.monotonic() - started, 0.7)
            finally:
                remove()
        self.assertIsInstance(error, TurnCancelled)

    def test_outer_deadline_does_not_wait_for_prompt_ack_timeout(self):
        with self.peer() as client:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                wait_for_turn(
                    client, "pending_ack", timeout=0.15, cancelled=lambda: False
                )
            self.assertLess(time.monotonic() - started, 0.8)

    def test_terminal_event_before_ack_does_not_hide_request_failure(self):
        with self.peer(request_timeout=0.2) as client:
            results = self.launch(client, "terminal_before_ack")
            error = results.get(timeout=2)
        self.assertIsInstance(error, RpcTimeoutError)

    def test_terminal_event_before_ack_is_retained_until_ack(self):
        with self.peer() as client:
            results = self.launch(client, "terminal_before_ack")
            with self.assertRaises(queue.Empty):
                results.get(timeout=0.1)
            client.request_raw("release")
            result = results.get(timeout=2)
        self.assertIsInstance(result, TurnResult)
        self.assertEqual(result.assistant_text, "complete before ack")

    def test_immediate_prompt_request_failure_is_reported(self):
        with self.peer() as client, self.assertRaises(RpcCommandError):
            wait_for_turn(client, "request_error", timeout=3, cancelled=lambda: False)

    def test_liveness_request_failure_is_reported(self):
        with self.peer() as client:
            results = self.launch(client, "state_error")
            error = results.get(timeout=3)
        self.assertIsInstance(error, RpcCommandError)
        self.assertEqual(error.command, "get_state")

    def test_invalid_terminal_notification_fails_instead_of_waiting_for_deadline(self):
        with self.peer() as client, self.assertRaises(RpcError):
            wait_for_turn(
                client, "malformed_terminal", timeout=3, cancelled=lambda: False
            )


if __name__ == "__main__":
    unittest.main()
