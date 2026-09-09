"""Exercise plugin hook execution without a client, credentials, or installed tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "The optional diagnostic hook requires sh")
class DiagnosticHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = (
            self.root / "plugin \" $(printf injected) `printf expanded` ; ' space"
        )
        self.project = self.root / "unrelated project"
        self.bin = self.root / "bin"
        self.home = self.root / "home"
        for directory in (self.package / "scripts", self.project, self.bin, self.home):
            directory.mkdir(parents=True)
        shutil.copyfile(
            ROOT / "scripts/session-start.sh",
            self.package / "scripts/session-start.sh",
        )
        (self.bin / "sh").symlink_to("/bin/sh")
        self.invoked = self.project / "tool-was-executed"
        self.payload_marker = "PRIVATE-PROMPT-PATH-DO-NOT-EMIT"
        self.payload = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "cwd": self.payload_marker,
                "prompt": self.payload_marker
                + ' $(printf leaked) " `printf leaked`' * 100,
                "transcript_path": self.payload_marker,
            }
        )

    def provide_tool(self, name):
        executable = self.bin / name
        executable.write_text(
            "#!/bin/sh\nprintf 'unexpected execution' > tool-was-executed\nexit 91\n"
        )
        executable.chmod(0o755)

    def run_hook(self, client, *, payload=None):
        config = json.loads((ROOT / f"config/{client}-hooks.json").read_text())
        # Execute every configured handler, so an added lifecycle side effect is visible.
        self.assertEqual(set(config["hooks"]), {"SessionStart"})
        results = []
        for group in config["hooks"]["SessionStart"]:
            for hook in group["hooks"]:
                self.assertEqual(hook["type"], "command")
                if "args" in hook:
                    command = [hook["command"]] + [
                        arg.replace("${CLAUDE_PLUGIN_ROOT}", str(self.package))
                        for arg in hook["args"]
                    ]
                else:
                    # Codex exports PLUGIN_ROOT; it must not substitute raw paths into shell code.
                    command = ["/bin/sh", "-c", hook["command"]]
                result = subprocess.run(
                    command,
                    cwd=self.project,
                    env={
                        "PATH": str(self.bin),
                        "HOME": str(self.home),
                        "PLUGIN_ROOT": str(self.package),
                        "CLAUDE_PLUGIN_ROOT": str(self.package),
                    },
                    input=self.payload if payload is None else payload,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertFalse(
                    self.invoked.exists(), "Diagnostics must not execute tools"
                )
                self.assertNotIn(self.payload_marker, result.stdout)
                self.assertNotIn(str(self.project), result.stdout)
                self.assertNotIn(str(self.package), result.stdout)
                results.append(result.stdout)
        return "".join(results)

    def test_healthy_is_silent_without_executing_tools(self):
        self.provide_tool("uv")
        self.provide_tool("omp")
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.assertEqual(self.run_hook(client), "")

    def test_missing_tools_produce_only_bounded_setup_context(self):
        for available in ((), ("uv",), ("omp",)):
            for name in ("uv", "omp"):
                (self.bin / name).unlink(missing_ok=True)
            for name in available:
                self.provide_tool(name)
            for client in ("claude", "codex"):
                with self.subTest(client=client, available=available):
                    stdout = self.run_hook(client)
                    self.assertLessEqual(len(stdout.encode()), 1024)
                    document = json.loads(stdout)
                    self.assertEqual(set(document), {"hookSpecificOutput"})
                    context = document["hookSpecificOutput"]
                    self.assertEqual(
                        set(context), {"hookEventName", "additionalContext"}
                    )
                    self.assertEqual(context["hookEventName"], "SessionStart")
                    guidance = context["additionalContext"]
                    self.assertIsInstance(guidance, str)
                    self.assertIn("setup", guidance.lower())
                    for missing in {"uv", "omp"} - set(available):
                        self.assertRegex(guidance, rf"\b{missing}\b")

    def test_malformed_input_does_not_change_diagnostic(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                expected = self.run_hook(client, payload="")
                self.assertEqual(
                    self.run_hook(
                        client, payload=self.payload_marker + "\x00{not-json"
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
