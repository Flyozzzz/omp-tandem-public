"""Synthetic native peer: independent execution and metadata-only probe processes."""

import json
import signal
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path.cwd()
ARGS = sys.argv[1:]
PROBE = "--no-session" in ARGS
ROLE = "probe" if PROBE else "execution"
CONTROL = ROOT / "routing-peer-control.json"
OPTIONS = json.loads(CONTROL.read_text()) if CONTROL.exists() else {}


def log(kind, **data):
    with (ROOT / "routing-peer-events.jsonl").open("a") as stream:
        stream.write(json.dumps({"role": ROLE, "kind": kind, **data}) + "\n")


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


def terminate(signum, frame):
    log("stopped")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, terminate)
log("spawn", args=ARGS)
if PROBE and OPTIONS.get("startup_stall"):
    time.sleep(30)

THINKING = ARGS[ARGS.index("--thinking") + 1]
SELECTOR = (
    ARGS[ARGS.index("--model") + 1]
    if "--model" in ARGS
    else OPTIONS.get("baseline_selector", "synthetic/baseline")
)
PROVIDER, MODEL = SELECTOR.split("/", 1)
BASELINE = {
    "id": MODEL,
    "provider": PROVIDER,
    "name": "Local baseline",
    "api": "local",
    "baseUrl": "https://PRIVATE_BASE_URL.invalid",
    "headers": {"Authorization": "PRIVATE_CATALOG_TOKEN"},
    "reasoning": True,
    "thinking": {"mode": "effort", "efforts": ["high"], "requiresEffort": False},
    "input": ["text", "image"],
    "contextWindow": 200000,
    "maxTokens": 20000,
    "cost": {"input": 1, "output": 2},
}
CATALOG = [BASELINE, {**BASELINE, "id": "alternative", "name": "Local alternative"}]
if OPTIONS.get("single_candidate"):
    CATALOG = CATALOG[:1]

SESSION = None
if not PROBE:
    directory = Path(ARGS[ARGS.index("--session-dir") + 1])
    directory.mkdir(parents=True, exist_ok=True)
    SESSION = (
        Path(ARGS[ARGS.index("--resume") + 1])
        if "--resume" in ARGS
        else directory / f"{uuid4()}.jsonl"
    )
    SESSION.touch()

READY = {"type": "ready", "protocolVersion": 1}
if PROBE and OPTIONS.get("negotiation_stall"):
    READY.update(
        supportedProtocolVersions=[1, 2],
        maxFrameBytes=1048576,
        maxReassembledFrameBytes=67108864,
    )
emit(READY)
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    log(kind, message=command.get("message"))
    if kind == "negotiate_protocol":
        time.sleep(30)
    elif kind == "get_available_models":
        if not PROBE:
            log("execution_catalog_violation")
            raise SystemExit(2)
        if OPTIONS.get("catalog_stall"):
            time.sleep(30)
        respond(command, {"models": CATALOG})
    elif kind == "get_state":
        respond(
            command,
            {
                "model": None if OPTIONS.get("missing_baseline") else BASELINE,
                "thinkingLevel": THINKING,
                "isStreaming": False,
                "isCompacting": False,
                "sessionId": "synthetic-session",
                "sessionFile": str(SESSION),
                "messageCount": 0,
                "queuedMessageCount": 0,
            },
        )
    elif kind == "set_host_tools":
        respond(command, {"toolNames": [tool["name"] for tool in command["tools"]]})
    elif kind == "prompt":
        if PROBE:
            log("probe_inference_violation")
            raise SystemExit(3)
        respond(command)
        emit({"type": "agent_start"})
        emit(
            {
                "type": "host_tool_call",
                "id": "finish",
                "toolCallId": "finish-call",
                "toolName": "tandem_finish",
                "arguments": {
                    "outcome": "success",
                    "summary": "Baseline completed",
                    "answer": "Unchanged baseline answer",
                },
            }
        )
    elif kind == "host_tool_result" and command["id"] == "finish":
        emit(
            {
                "type": "agent_end",
                "isTerminal": True,
                "messages": [
                    {
                        "role": "assistant",
                        "provider": PROVIDER,
                        "model": MODEL,
                        "content": [
                            {"type": "text", "text": "Unchanged baseline answer"}
                        ],
                        "stopReason": "stop",
                        "usage": {
                            "input": 10,
                            "output": 2,
                            "totalTokens": 12,
                            "cost": {"total": 0.1},
                        },
                    }
                ],
            }
        )
    else:
        respond(command)
log("stopped")
