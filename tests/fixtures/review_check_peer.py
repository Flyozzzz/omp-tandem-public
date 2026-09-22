"""Local Claude-CLI/OMP-RPC peer that performs a real snapshot review workflow."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4


def context_from_prompt(text):
    return json.loads(text.rsplit("\nSaved attempt context:\n", 1)[1])


def review_actions(context):
    work_id, step_id = context["work_id"], context["step_id"]
    submission_id = context["submission"]["submission_id"]

    def get(**fields):
        return "tandem_work", {
            "request": {"action": "get", "work_id": work_id, "step_id": step_id},
            **fields,
        }

    def mutation(action, revision, **fields):
        return "tandem_work", {
            "request": {
                "action": action,
                "work_id": work_id,
                "step_id": step_id,
                "expected_revision": revision,
                "operation_id": str(uuid4()),
                **fields,
            }
        }

    state = yield get()
    state = yield mutation("heartbeat", state["revision"])
    selected = yield "tandem_review_read", {"section": "selected", "path": "backend.py"}
    if "VALUE = 42" not in json.dumps(selected):
        raise ValueError("Reviewer did not receive the exact submitted file")
    state = yield mutation(
        "report",
        state["revision"],
        submission_id=submission_id,
        resolution="success",
        note="Read exact candidate before execution evidence",
        evidence=["Pinned backend.py contains VALUE = 42"],
    )
    until = time.monotonic() + 35
    while True:
        checks = yield get(section="verification")
        if checks["settled"]:
            break
        if time.monotonic() >= until:
            raise TimeoutError("Verifier did not settle")
        yield get(wait_seconds=1, after_revision=checks["revision"])
    if checks["output_visible"]:
        raise ValueError("Execution output leaked into independent review")
    state = yield get()
    state = yield mutation("compare", state["revision"])
    checks = yield get(section="verification")
    if not checks["output_visible"]:
        raise ValueError("Comparison did not expose verification evidence")
    for run in checks["runs"]:
        output = yield get(section=run["section"])
        if output.get("content") is None:
            raise ValueError("Missing bounded verification output")
    state = yield get()
    verdict = "accept" if checks["status"] == "passed" else "reject"
    yield mutation(
        verdict,
        state["revision"],
        submission_id=submission_id,
        note="Decision after separate code-owned verification",
        evidence=["Read bounded machine-recorded command output"],
    )
    return {
        "outcome": "success",
        "answer": f"Review delivered {verdict}",
        "evidence": ["Exact candidate and trusted check output inspected"],
    }


async def claude():
    from fastmcp import Client
    from pydantic_core import to_jsonable_python

    config = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
    if sys.argv[sys.argv.index("--tools") + 1]:
        raise ValueError("Independent reviewer received native tools")
    context = context_from_prompt(sys.stdin.read())
    actions = review_actions(context)
    action = next(actions)
    async with Client(config) as client:
        tool_names = [tool.name for tool in await client.list_tools()]
        while True:
            name, arguments = action
            actual_name = next(value for value in tool_names if value.endswith(name))
            result = await client.call_tool(actual_name, arguments)
            value = to_jsonable_python(result.data)
            try:
                action = actions.send(value)
            except StopIteration as finished:
                output = finished.value
                break
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": sys.argv[sys.argv.index("--session-id") + 1],
                "total_cost_usd": 0,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "structured_output": output,
            }
        ),
        flush=True,
    )


def omp():
    actions = None
    session = Path(sys.argv[sys.argv.index("--session-dir") + 1]) / "peer.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.touch()
    message = {
        "role": "assistant",
        "responseId": "local-review",
        "provider": "local",
        "model": "peer",
        "content": [{"type": "text", "text": "Synthetic review"}],
        "stopReason": "toolUse",
        "usage": {
            "input": 1,
            "output": 1,
            "totalTokens": 2,
            "cacheRead": 0,
            "cacheWrite": 0,
            "cost": {"total": 0.001},
        },
    }

    def emit(value):
        print(json.dumps(value), flush=True)

    def respond(command, data=None):
        emit(
            {
                "type": "response",
                "id": command.get("id"),
                "command": command["type"],
                "success": True,
                "data": data or {},
            }
        )

    def call(action):
        tool, arguments = action
        emit(
            {
                "type": "host_tool_call",
                "id": "action",
                "toolCallId": str(uuid4()),
                "toolName": tool,
                "arguments": arguments,
            }
        )

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        if kind == "get_state":
            respond(
                command,
                {
                    "model": {
                        "id": "peer",
                        "name": "Peer",
                        "api": "local",
                        "provider": "local",
                        "baseUrl": "http://invalid",
                        "reasoning": True,
                    },
                    "thinkingLevel": "high",
                    "isStreaming": False,
                    "isCompacting": False,
                    "sessionId": "local-peer",
                    "sessionFile": str(session),
                    "messageCount": 1,
                    "queuedMessageCount": 0,
                },
            )
        elif kind == "set_host_tools":
            respond(command, {"toolNames": [tool["name"] for tool in command["tools"]]})
        elif kind == "prompt":
            respond(command)
            emit({"type": "agent_start"})
            emit({"type": "message_end", "message": message})
            task = json.loads(command["message"])
            actions = review_actions(context_from_prompt(task["task"]["goal"]))
            call(next(actions))
        elif kind == "host_tool_result":
            if command.get("isError"):
                emit(
                    {
                        "type": "agent_end",
                        "isTerminal": True,
                        "messages": [
                            {
                                **message,
                                "stopReason": "error",
                                "errorMessage": json.dumps(command["result"]),
                            }
                        ],
                    }
                )
                continue
            if command["id"] == "finish":
                emit(
                    {
                        "type": "agent_end",
                        "isTerminal": True,
                        "messages": [
                            message,
                            {
                                **message,
                                "responseId": "local-review-final",
                                "stopReason": "stop",
                            },
                        ],
                    }
                )
                continue
            text = command["result"]["content"][0]["text"]
            try:
                call(actions.send(json.loads(text)))
            except StopIteration as finished:
                output = finished.value
                emit(
                    {
                        "type": "host_tool_call",
                        "id": "finish",
                        "toolCallId": "finish",
                        "toolName": "tandem_finish",
                        "arguments": {
                            "outcome": output["outcome"],
                            "answer": output["answer"],
                            "summary": "Review complete",
                        },
                    }
                )
        elif kind == "abort":
            respond(command)
        else:
            respond(command)


if __name__ == "__main__":
    if "--mode" in sys.argv:
        omp()
    else:
        asyncio.run(claude())
