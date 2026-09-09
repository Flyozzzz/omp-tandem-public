"""Consumer regressions; local fault peers only, no model calls or production state."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastmcp import Client
from pydantic_core import to_jsonable_python

from omp_tandem.api import build_server
from omp_tandem.bridge import Bridge
from omp_tandem.runtime_models import ACTIVE


class BridgeRegressionTests(unittest.TestCase):
    def test_deadline_covers_worker_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            peer = root / "slow-omp"
            peer_script = root / "slow_peer.py"
            peer_script.write_text(
                "import os, time\n"
                "from pathlib import Path\n"
                "Path('worker.pid').write_text(str(os.getpid()))\n"
                "time.sleep(4)\n"
            )
            peer.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " "
                + shlex.quote(str(peer_script))
                + ' "$@"\n'
            )
            peer.chmod(0o700)
            bridge = Bridge(root / "state", str(peer), "unused", project_root=root)
            try:
                job = bridge.start("hello", str(root), "think", timeout_seconds=1)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    result = bridge.tasks.get(job["task_id"])
                    if result["status"] not in ACTIVE:
                        break
                    time.sleep(0.02)
                self.assertEqual(result["status"], "failed", result)
                pid = int((root / "worker.pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                bridge.shutdown()

    def test_typed_mcp_client_preserves_task_list_fields(self):
        async def scenario(root):
            bridge = Bridge(
                root / "state", str(root / "missing-omp"), "unused", project_root=root
            )
            async with Client(build_server(bridge)) as client:
                started = await client.call_tool(
                    "tandem_start",
                    {"prompt": "hello", "cwd": str(root), "mode": "think"},
                )
                job = json.loads(started.content[0].text)
                result = await client.call_tool(
                    "tandem_result", {"task_id": job["task_id"], "wait_seconds": 5}
                )
                self.assertEqual(json.loads(result.content[0].text)["status"], "failed")
                listed = await client.call_tool("tandem_list", {})
                rows = to_jsonable_python(listed.data)
                self.assertEqual(rows[0].get("task_id"), job["task_id"], rows)
                self.assertEqual(rows[0].get("status"), "failed", rows)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


if __name__ == "__main__":
    unittest.main()
