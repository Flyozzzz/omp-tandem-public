"""Local RPC peer exercising team file effects and model selection without a provider."""

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path.cwd()
ARGS = sys.argv[1:]
PROBE = "--no-session" in ARGS
CONTROL = ROOT / "team-peer-control.json"
OPTIONS = json.loads(CONTROL.read_text()) if CONTROL.exists() else {}
SELECTED = (
    ARGS[ARGS.index("--model") + 1] if "--model" in ARGS else "openai-codex/baseline"
)
ACTUAL = OPTIONS.get("substitute_model", SELECTED) if not PROBE else SELECTED
THINKING = ARGS[ARGS.index("--thinking") + 1] if "--thinking" in ARGS else "high"


def model(selector):
    provider, identifier = selector.split("/", 1)
    return {
        "provider": provider,
        "id": identifier,
        "name": identifier,
        "api": "local",
        "baseUrl": "http://127.0.0.1",
        "reasoning": True,
        "thinking": {
            "mode": "effort",
            "efforts": ["low", "medium", "high"],
            "requiresEffort": False,
        },
        "input": ["text"],
        "contextWindow": 200000,
        "maxTokens": 20000,
        "toolCalling": True,
        "cost": {"input": 1, "output": 2},
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


def log(kind, **fields):
    with (ROOT / "team-peer-events.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"probe": PROBE, "kind": kind, "model": ACTUAL, **fields}) + "\n"
        )


def finish(answer):
    emit(
        {
            "type": "host_tool_call",
            "id": "finish",
            "toolCallId": "finish",
            "toolName": "tandem_finish",
            "arguments": {
                "outcome": "success",
                "summary": "Local task finished",
                "answer": answer,
            },
        }
    )


SESSION = None
if not PROBE:
    directory = Path(ARGS[ARGS.index("--session-dir") + 1])
    directory.mkdir(parents=True, exist_ok=True)
    SESSION = directory / f"{uuid4()}.jsonl"
    SESSION.touch()

emit({"type": "ready", "protocolVersion": 1})
answer = ""
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_available_models":
        log(kind)
        respond(
            command,
            {
                "models": [
                    model("openai-codex/baseline"),
                    model("openai-codex/alternative"),
                ]
            },
        )
    elif kind == "get_state":
        respond(
            command,
            {
                "model": model(ACTUAL),
                "thinkingLevel": THINKING,
                "isStreaming": False,
                "isCompacting": False,
                "sessionId": "team-local",
                "sessionFile": str(SESSION),
                "messageCount": 0,
                "queuedMessageCount": 0,
            },
        )
    elif kind == "set_host_tools":
        respond(command, {"toolNames": [tool["name"] for tool in command["tools"]]})
    elif kind == "prompt":
        if PROBE:
            raise RuntimeError("Metadata discovery must never send a prompt")
        respond(command)
        task = json.loads(command["message"])
        goal = task["task"]["goal"]
        log("prompt", goal=goal)
        emit({"type": "agent_start"})
        if goal == "write alpha":
            time.sleep(0.1)
            (ROOT / "alpha.txt").write_text("alpha")
            answer = "Created alpha.txt"
        elif goal == "write beta":
            time.sleep(0.1)
            (ROOT / "beta.txt").write_text("beta")
            answer = "Created beta.txt"
        elif goal == "integrate files":
            answer = (
                (ROOT / "alpha.txt").read_text() + "|" + (ROOT / "beta.txt").read_text()
            )
        else:
            answer = "Consulted the available models; use baseline for exploration and alternative for verification."
        ACTUAL = OPTIONS.get("model_after_prompt", ACTUAL)
        finish(answer)
    elif kind == "host_tool_result" and command["id"] == "finish":
        emit(
            {
                "type": "agent_end",
                "isTerminal": True,
                "messages": [
                    {
                        "role": "assistant",
                        "provider": ACTUAL.split("/", 1)[0],
                        "model": ACTUAL.split("/", 1)[1],
                        "content": [{"type": "text", "text": answer}],
                        "stopReason": "stop",
                        "usage": {
                            "input": 10,
                            "output": 2,
                            "totalTokens": 12,
                            "cost": {"total": 0.01},
                        },
                    }
                ],
            }
        )
    else:
        respond(command)
