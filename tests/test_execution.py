"""Computation and per-turn accounting regressions, without provider credentials."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from pydantic import ValidationError

from omp_tandem.bridge import Bridge
from omp_tandem.execution import (
    ExecutionOptions,
    TurnUsage,
    conversation_usage,
    resolve_execution,
)
from tests.helpers import make_peer


class ExecutionTests(unittest.TestCase):
    def test_explicit_timeout_and_override_precedence_survive_continuation(self):
        first = resolve_execution(
            {
                "profile": "quick",
                "thinking": "medium",
                "model": "provider/model",
                "timeout_seconds": 20,
            },
            timeout_seconds=7,
        )
        self.assertEqual(first["effective"]["thinking"], "medium")
        self.assertEqual(first["effective"]["timeout_seconds"], 7)
        inherited = resolve_execution(previous=first["effective"])
        self.assertEqual(inherited["effective"], first["effective"])
        changed = resolve_execution({"profile": "deep"}, previous=first["effective"])
        self.assertEqual(changed["effective"]["model"], "provider/model")
        self.assertEqual(changed["effective"]["timeout_seconds"], 3600)
        self.assertEqual(changed["effective"]["thinking"], "high")

    def test_unsupported_thinking_and_permission_options_are_rejected(self):
        with self.assertRaises(ValidationError):
            ExecutionOptions(thinking="ultra")
        with self.assertRaises(ValidationError):
            ExecutionOptions.model_validate({"profile": "deep", "mode": "work"})
        with self.assertRaises(ValidationError):
            resolve_execution(timeout_seconds=True)

    def test_tool_rounds_and_terminal_replay_are_counted_once(self):
        usage = TurnUsage(lambda _: None)
        messages = [
            {
                "role": "assistant",
                "responseId": str(index),
                "provider": "local",
                "model": "peer",
                "usage": {
                    "input": 10,
                    "output": 2,
                    "cacheRead": 5,
                    "cacheWrite": 3,
                    "totalTokens": 20,
                    "cost": {"total": 0.2},
                },
            }
            for index in range(2)
        ]
        for message in messages:
            usage.message_end(SimpleNamespace(message=message))
        event = SimpleNamespace(messages=messages, is_terminal=True)
        usage.agent_end(event)
        usage.agent_end(event)
        actual = usage.snapshot()
        self.assertEqual(actual["response_count"], 2)
        self.assertEqual(actual["tokens"]["total"]["value"], 40)
        self.assertEqual(actual["tokens"]["cache_read"]["value"], 10)
        self.assertAlmostEqual(actual["cost"]["value"], 0.4)

    def test_unknown_and_failed_usage_remain_partial_in_conversation(self):
        usage = TurnUsage(lambda _: None)
        usage.message_end(
            SimpleNamespace(
                message={
                    "role": "assistant",
                    "usage": {
                        "input": 8,
                        "output": 2,
                        "totalTokens": 10,
                        "cost": {"total": 0.1},
                    },
                }
            )
        )
        usage.message_end(
            SimpleNamespace(
                message={
                    "role": "assistant",
                    "stopReason": "error",
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "totalTokens": 0,
                        "cost": {"total": 0},
                    },
                }
            )
        )
        task = {
            "accounting_json": json.dumps(usage.snapshot(interrupted=True)),
            "duration_seconds": 2,
        }
        result = conversation_usage([task, {}])
        self.assertIsNone(result["tokens"]["input"]["value"])
        self.assertEqual(result["tokens"]["input"]["known_subtotal"], 8)
        self.assertEqual(result["cost"]["status"], "partial")
        self.assertAlmostEqual(result["cost"]["known_subtotal"], 0.1)
        self.assertIsNone(result["cost"]["value"])
        self.assertIsNone(result["duration_seconds"]["value"])


PEER = r"""
import json
import sys
from pathlib import Path
from uuid import uuid4

def emit(value):
    print(json.dumps(value), flush=True)

def respond(command, data=None):
    emit({'type': 'response', 'id': command.get('id'), 'command': command['type'], 'success': True, 'data': data or {}})

args = sys.argv[1:]
thinking = args[args.index('--thinking') + 1]
requested_model = args[args.index('--model') + 1]
directory = Path(args[args.index('--session-dir') + 1])
directory.mkdir(parents=True, exist_ok=True)
path = Path(args[args.index('--resume') + 1]) if '--resume' in args else directory / (str(uuid4()) + '.jsonl')
path.touch()
model = {'id': 'peer', 'name': 'Peer', 'api': 'local', 'provider': 'local', 'baseUrl': 'http://invalid', 'reasoning': True}
messages = []
emit({'type': 'ready', 'protocolVersion': 1})
for line in sys.stdin:
    command = json.loads(line)
    kind = command['type']
    if kind == 'set_host_tools':
        respond(command, {'toolNames': [tool['name'] for tool in command['tools']]})
    elif kind == 'get_state':
        respond(command, {'model': model, 'thinkingLevel': 'low' if requested_model == 'clamped' else thinking,
                          'isStreaming': False, 'isCompacting': False, 'sessionId': 'peer',
                          'sessionFile': str(path), 'messageCount': 999, 'queuedMessageCount': 0})
    elif kind == 'prompt':
        respond(command)
        emit({'type': 'agent_start'})
        goal = json.loads(command['message'])['task']['goal']
        for index in range(2):
            usage = {'input': 10, 'output': 2, 'cacheRead': 5, 'cacheWrite': 3, 'totalTokens': 20, 'cost': {'total': 0.2}}
            message = {'role': 'assistant', 'responseId': str(index), 'provider': 'local', 'model': 'peer',
                       'content': [{'type': 'text', 'text': 'working'}], 'stopReason': 'toolUse', 'usage': usage}
            messages.append(message)
            emit({'type': 'message_end', 'message': message})
        if goal == 'fail':
            emit({'type': 'agent_end', 'isTerminal': True, 'messages': messages + [
                {'role': 'assistant', 'stopReason': 'error', 'errorMessage': 'provider failed', 'content': []}]})
        else:
            emit({'type': 'host_tool_call', 'id': 'finish', 'toolCallId': 'finish-call', 'toolName': 'tandem_finish',
                  'arguments': {'outcome': 'success', 'summary': 'done', 'answer': json.dumps(args)}})
    elif kind == 'host_tool_result':
        emit({'type': 'agent_end', 'isTerminal': True, 'messages': messages})
    elif kind == 'abort':
        respond(command)
"""


class NativeExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        peer = make_peer(self.root)
        (self.root / "peer.py").write_text(PEER)
        self.bridge = Bridge(
            self.root / "state", str(peer), "local/peer", project_root=self.root
        )
        self.addCleanup(self.bridge.shutdown)

    def wait(self, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.bridge.view(task_id)
            if result["status"] in ("completed", "failed", "cancelled"):
                return result
            time.sleep(0.02)
        self.fail("Local native peer did not finish")

    def test_real_rpc_options_permissions_and_resume_accounting(self):
        job = self.bridge.runtime.start(
            prompt="ok",
            cwd=str(self.root),
            mode="think",
            execution={"profile": "quick"},
            timeout_seconds=5,
        )
        first = self.wait(job["task_id"])
        self.assertEqual(first["status"], "completed", first)
        args = json.loads(first["answer"])
        self.assertIn("--no-tools", args)
        self.assertIn("--no-lsp", args)
        self.assertIn("--no-skills", args)
        self.assertEqual(
            first["execution"]["actual"], {"model": "local/peer", "thinking": "low"}
        )
        self.assertEqual(
            first["execution"]["models"]["observed"],
            {"value": "local/peer", "source": "native_get_state"},
        )
        self.assertIsNone(first["execution"]["models"]["requested"]["value"])
        self.assertEqual(
            first["execution"]["models"]["effective"]["value"], "local/peer"
        )
        self.assertEqual(first["execution"]["attempt"]["mode"], "manual")
        self.assertFalse(first["execution"]["attempt"]["grant"]["valid"])
        self.assertFalse(first["execution"]["attempt"]["grant"]["present"])
        self.assertEqual(first["usage"]["task"]["tokens"]["total"]["value"], 40)
        self.assertGreater(first["usage"]["task"]["duration_seconds"], 0)
        follow = self.bridge.runtime.start(
            prompt="ok",
            conversation_id=job["conversation_id"],
            execution={"profile": "deep"},
        )
        second = self.wait(follow["task_id"])
        self.assertEqual(second["status"], "completed", second)
        self.assertIn("--no-tools", json.loads(second["answer"]))
        self.assertEqual(second["usage"]["task"]["response_count"], 2)
        self.assertEqual(
            second["usage"]["conversation"]["tokens"]["total"]["value"], 80
        )
        self.assertEqual(second["usage"]["conversation"]["cost"]["value"], 0.8)

    def test_native_clamping_is_a_failure_not_applied_settings(self):
        job = self.bridge.runtime.start(
            prompt="ok",
            cwd=str(self.root),
            mode="think",
            execution={"model": "clamped", "thinking": "max"},
            timeout_seconds=5,
        )
        result = self.wait(job["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["execution"]["actual"]["thinking"], "low")
        self.assertEqual(result["execution"]["effective"]["thinking"], "max")
        self.assertIsNone(result["usage"]["task"]["cost"]["value"])

    def test_failed_turn_preserves_known_model_rounds(self):
        job = self.bridge.runtime.start(
            prompt="fail", cwd=str(self.root), mode="think", timeout_seconds=5
        )
        result = self.wait(job["task_id"])
        self.assertEqual(result["status"], "failed")
        usage = result["usage"]["task"]
        self.assertEqual(usage["coverage"], "partial")
        self.assertEqual(usage["tokens"]["total"]["known_subtotal"], 40)
        self.assertIsNone(usage["tokens"]["total"]["value"])
        self.assertAlmostEqual(usage["cost"]["known_subtotal"], 0.4)


class StopClassificationTests(unittest.TestCase):
    ENVELOPE: ClassVar[dict] = {
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorId": 53248,
        "errorMessage": (
            "Codex error event: This content was flagged for possible cybersecurity "
            "risk. If this seems wrong, try rephrasing your request. (code=cyber_policy)"
        ),
    }

    def test_machine_coded_refusal_is_preserved_and_prose_is_not_classified(self):
        from omp_tandem.execution import failure_fact, stop_record

        record = stop_record(self.ENVELOPE, now=1.0)
        self.assertEqual(record["classification"], "provider_policy_refusal")
        self.assertEqual(
            (record["error_code"], record["error_id"]), ("cyber_policy", 53248)
        )
        prose = stop_record(
            {**self.ENVELOPE, "errorMessage": "Refused: cyber_policy applies here"},
            now=1.0,
        )
        self.assertEqual(
            (prose["classification"], prose["error_code"]), ("unclassified", None)
        )
        other = stop_record(
            {**self.ENVELOPE, "errorMessage": "Rate limited (code=rate_limit)"}
        )
        self.assertEqual(
            (other["classification"], other["error_code"]),
            ("provider_error", "rate_limit"),
        )
        self.assertEqual(
            stop_record({**self.ENVELOPE, "stopReason": "length"})["classification"],
            "length",
        )
        self.assertIsNone(stop_record({"stopReason": "stop"}))
        self.assertIsNone(stop_record(None))
        settings = json.dumps({"requested": {}, "effective": {}, "stop": record})
        self.assertEqual(
            failure_fact(settings, "failed"),
            {
                "classification": "provider_policy_refusal",
                "code": "cyber_policy",
                "source": "assistant_message.errorMessage code suffix",
            },
        )
        self.assertIsNone(failure_fact(settings, "completed"))
        self.assertEqual(
            failure_fact(None, "interrupted")["classification"], "unrecorded"
        )
        self.assertEqual(failure_fact(None, "cancelled")["classification"], "cancelled")
