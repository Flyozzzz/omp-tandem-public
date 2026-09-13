"""Deterministic Claude executable substitute; real bound MCP, no model calls."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from fastmcp import Client
from pydantic_core import to_jsonable_python


async def main():
    saved = json.loads(sys.stdin.read().split("Saved attempt context:\n", 1)[1])
    config = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
    directory = Path(os.environ["TANDEM_REPLAN_FIXTURE"])
    async with Client(config) as client:

        async def call(request):
            response = await client.call_tool(
                "tandem_work", {"request": request, "view": "full"}
            )
            return to_jsonable_python(response.data)

        view = await call({"action": "get", "work_id": saved["work_id"]})
        view = await call(
            {
                "action": "heartbeat",
                "work_id": saved["work_id"],
                "step_id": saved["step_id"],
                "expected_revision": view["revision"],
                "operation_id": str(uuid4()),
            }
        )
        module = Path("module.txt")
        # The owned checkpoint marker, NOT the external effect log, decides whether
        # this assignment continues already-performed work.
        continuing = module.read_text() == "before\neffect completed\npartial work\n"
        if continuing:
            with module.open("a") as target:
                target.write("continued work\n")
        else:
            assert module.read_text() == "before\n"
            with (directory / "effects.jsonl").open("a") as target:
                target.write(json.dumps({"effect": "performed"}) + "\n")
            module.write_text("before\neffect completed\npartial work\n")
        observation = {
            "attempt_id": saved["attempt_id"],
            "pid": os.getpid(),
            "workspace": str(Path.cwd()),
            "continuing": continuing,
            "checkpoint": view["steps"][0]["attempt"]["checkpoint"],
        }
        ready = directory / (saved["attempt_id"] + ".json")
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(json.dumps(observation))
        temporary.replace(ready)
        if not continuing:
            # No result/cost is reported for the cancelled launch. The real
            # supervisor must retain the unknown-cost pause and reap this process.
            await asyncio.Event().wait()
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": sys.argv[sys.argv.index("--session-id") + 1],
                "total_cost_usd": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "structured_output": {
                    "outcome": "success",
                    "answer": "Continued the preserved owned-file marker without repeating the effect.",
                    "evidence": ["module.txt contains saved and continued work"],
                },
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
