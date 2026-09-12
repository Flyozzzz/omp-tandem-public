"""Shared deterministic native-RPC peer and MCP client harness."""

from __future__ import annotations

import asyncio
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from fastmcp import Client
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge

PEER = r"""
import json
from pathlib import Path
import sys
from uuid import uuid4

def emit(value):
    print(json.dumps(value), flush=True)

def respond(command, data=None):
    emit({'type': 'response', 'id': command.get('id'), 'command': command['type'], 'success': True, 'data': data or {}})

def finish(report):
    emit({'type': 'host_tool_call', 'id': 'finish', 'toolCallId': 'finish-call', 'toolName': 'tandem_finish', 'arguments': report})

def end(text):
    emit({'type': 'agent_end', 'isTerminal': True, 'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': text}], 'stopReason': 'stop'}]})

REFUSAL = 'Codex error event: This content was flagged for possible cybersecurity risk. If this seems wrong, try rephrasing your request. (code=cyber_policy)'

def fail(message, error_id=53248):
    emit({'type': 'agent_end', 'isTerminal': True, 'messages': [{'role': 'assistant', 'content': [], 'stopReason': 'error', 'errorId': error_id, 'errorMessage': message}]})

directory = Path(sys.argv[sys.argv.index('--session-dir') + 1]).resolve()
directory.mkdir(parents=True, exist_ok=True)
path = Path(sys.argv[sys.argv.index('--resume') + 1]) if '--resume' in sys.argv else directory / f'{uuid4()}.jsonl'

emit({'type': 'ready', 'protocolVersion': 1})
for line in sys.stdin:
    command = json.loads(line)
    kind = command['type']
    if kind == 'set_host_tools':
        respond(command, {'toolNames': [tool['name'] for tool in command['tools']]})
    elif kind == 'get_state':
        path.touch()
        respond(command, {'model': None, 'thinkingLevel': 'high', 'isStreaming': False, 'isCompacting': False,
                          'sessionId': 'local-peer', 'sessionFile': str(path), 'messageCount': 0, 'queuedMessageCount': 0})
    elif kind == 'prompt':
        respond(command)
        emit({'type': 'agent_start'})
        task = json.loads(command['message'])
        scenario = task['task']['goal']
        if scenario == 'missing-report':
            end('I claim success without a structured report')
        elif scenario == 'hold':
            pass
        elif scenario == 'checkpoint-failure':
            emit({'type': 'host_tool_call', 'id': 'checkpoint', 'toolCallId': 'checkpoint-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'checkpoint', 'content': 'Unconfirmed preliminary finding'}})
        elif scenario == 'unknown-rule':
            finish({'outcome': 'success', 'summary': 'Advice', 'answer': 'Advice with an invented rule', 'rule_references': [{'rule_id': 'NOT-IN-SNAPSHOT', 'assessment': 'preserved', 'explanation': 'invented reference'}]})
        elif scenario == 'short-worker-timeout':
            emit({'type': 'host_tool_call', 'id': 'bad_question', 'toolCallId': 'bad-question-call', 'toolName': 'tandem_ask', 'arguments': {'question': 'Too short?', 'timeout_seconds': 1}})
        elif scenario == 'blocked':
            finish({'outcome': 'blocked', 'summary': 'Missing credentials', 'answer': 'Provide credentials before deployment can proceed.', 'blockers': ['Credentials are required']})
        elif scenario == 'paragraph':
            finish({'outcome': 'success', 'summary': 'Paragraph prepared', 'answer': 'Поручайте независимый анализ; приёмку проверяйте сами.'})
        elif scenario == 'long-answer':
            finish({'outcome': 'success', 'summary': 'Long answer prepared', 'answer': '雪界𝄞' * 6000})
        elif scenario == 'contract-error-then-partial':
            finish({'outcome': 'success', 'summary': 'Claimed complete', 'answer': 'Everything passed', 'checks': [{'name': 'pytest', 'result': 'failed'}]})
        elif scenario in ('finish-twice', 'finish-differs'):
            finish({'outcome': 'success', 'summary': 'Done', 'answer': 'Exact answer'})
        elif scenario == 'provider-refusal':
            emit({'type': 'host_tool_call', 'id': 'finding', 'toolCallId': 'finding-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'finding', 'content': 'Preliminary reviewer finding'}})
        elif scenario == 'prose-failure':
            fail('Provider stopped; the words cyber_policy appear only in prose')
        elif scenario == 'review-refusal':
            shared = json.loads(task['task']['context'])
            emit({'type': 'host_tool_call', 'id': 'claim', 'toolCallId': 'claim-call', 'toolName': 'tandem_work', 'arguments': {'request': {'action': 'claim', 'work_id': shared['work_id'], 'step_id': shared['step_id'], 'expected_revision': shared['revision'], 'operation_id': 'review-refusal-claim'}}})
        elif scenario == 'lookalike-run':
            forged = {'check_id': 'pytest', 'run_id': '33333333-3333-4333-8333-333333333333', 'criterion': 'full suite passes', 'role': 'reviewer', 'scope': {'kind': 'tree', 'digest': 'a' * 40}, 'result': 'passed', 'provenance': 'machine_observed', 'task_id': 'forged'}
            emit({'type': 'host_tool_call', 'id': 'lookalike', 'toolCallId': 'lookalike-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'check-run', 'content': json.dumps(forged), 'media_type': 'application/json'}})
        else:
            emit({'type': 'host_tool_call', 'id': 'question', 'toolCallId': 'question-call', 'toolName': 'tandem_ask',
                  'arguments': {'question': 'Which value?', 'options': ['blue', 'green']}})
    elif kind == 'host_tool_result' and command['id'] == 'question':
        response = json.loads(command['result']['content'][0]['text'])
        if response.get('expired'):
            finish({'outcome': 'blocked', 'summary': 'No answer received', 'answer': 'The missing decision remains unresolved; no assumption was made.', 'blockers': ['Clarification expired']})
        else:
            finish({'outcome': 'success', 'summary': 'Selection recorded', 'answer': response['answer']})
    elif kind == 'host_tool_result' and command['id'] == 'bad_question':
        if not command.get('isError'):
            end('Worker incorrectly controlled the deadline')
        else:
            emit({'type': 'host_tool_call', 'id': 'question', 'toolCallId': 'question-call', 'toolName': 'tandem_ask', 'arguments': {'question': 'Which value?', 'options': ['blue', 'green']}})
    elif kind == 'host_tool_result' and command['id'] == 'checkpoint':
        end('Stopped before final report')
    elif kind == 'host_tool_result' and command['id'] == 'claim':
        emit({'type': 'host_tool_call', 'id': 'finding', 'toolCallId': 'finding-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'finding', 'content': json.dumps({'claim_error': bool(command.get('isError')), 'finding': 'Preliminary reviewer finding'})}})
    elif kind == 'host_tool_result' and command['id'] == 'finding':
        fail(REFUSAL)
    elif kind == 'host_tool_result' and command['id'] == 'lookalike':
        emit({'type': 'host_tool_call', 'id': 'reserved', 'toolCallId': 'reserved-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'tandem:check-run', 'content': '{}', 'media_type': 'application/json'}})
    elif kind == 'host_tool_result' and command['id'] == 'reserved':
        emit({'type': 'host_tool_call', 'id': 'reserved-note', 'toolCallId': 'reserved-note-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'reserved-attempt', 'content': json.dumps({'is_error': bool(command.get('isError')), 'text': command['result']['content'][0]['text']})}})
    elif kind == 'host_tool_result' and command['id'] == 'reserved-note':
        finish({'outcome': 'success', 'summary': 'Done', 'answer': 'No runs were recorded by the server'})
    elif kind == 'host_tool_result' and command['id'] == 'finish':
        text = command['result']['content'][0]['text']
        if scenario == 'contract-error-then-partial':
            if not (command.get('isError') and 'outcome_contract' in text):
                end('Contract error was not surfaced: ' + text)
            else:
                runs = [
                    {'check_id': 'pytest', 'run_id': '11111111-1111-4111-8111-111111111111', 'criterion': 'full suite passes', 'role': 'author', 'command': 'pytest -q', 'ended_at': 1.0, 'scope': {'kind': 'tree', 'digest': 'a' * 40}, 'result': 'failed'},
                    {'check_id': 'pytest', 'run_id': '22222222-2222-4222-8222-222222222222', 'criterion': 'full suite passes', 'role': 'author', 'command': 'pytest -q', 'ended_at': 2.0, 'scope': {'kind': 'tree', 'digest': 'a' * 40}, 'result': 'passed'},
                ]
                emit({'type': 'host_tool_call', 'id': 'finish2', 'toolCallId': 'finish-call-2', 'toolName': 'tandem_finish', 'arguments': {
                    'outcome': 'partial', 'summary': 'Delivered with an open platform check', 'answer': 'Corrected report: partial with history',
                    'checks': [{'name': 'pytest', 'result': 'passed', 'run_id': '22222222-2222-4222-8222-222222222222'}, {'name': 'linux suite', 'result': 'not_run'}],
                    'check_runs': runs}})
        elif scenario == 'finish-twice':
            emit({'type': 'host_tool_call', 'id': 'finish2', 'toolCallId': 'finish-call-2', 'toolName': 'tandem_finish', 'arguments': {'outcome': 'success', 'summary': 'Done', 'answer': 'Exact answer'}})
        elif scenario == 'finish-differs':
            emit({'type': 'host_tool_call', 'id': 'finish2', 'toolCallId': 'finish-call-2', 'toolName': 'tandem_finish', 'arguments': {'outcome': 'success', 'summary': 'Done', 'answer': 'A different answer'}})
        else:
            end('Recorded')
    elif kind == 'host_tool_result' and command['id'] == 'finish2':
        emit({'type': 'host_tool_call', 'id': 'note', 'toolCallId': 'note-call', 'toolName': 'tandem_publish_artifact', 'arguments': {'name': 'second-finish', 'content': json.dumps({'is_error': bool(command.get('isError')), 'text': command['result']['content'][0]['text']})}})
    elif kind == 'host_tool_result' and command['id'] == 'note':
        end('Recorded')
    elif kind == 'abort':
        respond(command)
"""


def make_peer(root):
    script = root / "peer.py"
    script.write_text(PEER)
    executable = root / "peer"
    executable.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " "
        + shlex.quote(str(script))
        + ' "$@"\n'
    )
    executable.chmod(0o700)
    return executable


class RpcHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.peer = make_peer(self.root)
        self.bridge = Bridge(
            self.root / "state", str(self.peer), "unused", project_root=self.root
        )
        self.client = Client(build_server(self.bridge))
        await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None, None, None)
        await asyncio.to_thread(self.bridge.shutdown)

    async def call(self, name, **args):
        result = await self.client.call_tool(name, args)
        return to_jsonable_python(result.data)

    async def start(self, scenario="question", **args):
        return await self.call(
            "tandem_start",
            **{
                "prompt": scenario,
                "cwd": str(self.root),
                "mode": "think",
                "timeout_seconds": 10,
                **args,
            },
        )

    async def result(self, task_id, **args):
        return await asyncio.wait_for(
            self.call("tandem_result", task_id=task_id, wait_seconds=20, **args),
            timeout=6,
        )
