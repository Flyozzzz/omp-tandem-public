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
    elif kind == 'host_tool_result' and command['id'] == 'finish':
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
