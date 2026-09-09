"""Launcher keeps delivery opt-in separate from tool permissions and user settings."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import launch


class LauncherTests(unittest.TestCase):
    def plan(self, *arguments):
        output = io.StringIO()
        with (
            patch.object(
                launch.shutil, "which", return_value="/tools with spaces/claude"
            ),
            patch.object(launch.os, "name", "posix"),
            patch.object(launch.os, "execvpe") as execute,
            redirect_stdout(output),
        ):
            self.assertEqual(launch.main(["--check", *arguments]), 0)
            execute.assert_not_called()
        return json.loads(output.getvalue())

    def test_push_opt_in_does_not_bypass_tool_permissions(self):
        plan = self.plan(
            "--", "--model", "model with spaces", "a prompt; not a shell command"
        )
        self.assertIn("--dangerously-load-development-channels", plan["command"])
        self.assertIn("server:omp-tandem", plan["command"])
        self.assertNotIn("--dangerously-skip-permissions", plan["command"])
        self.assertEqual(
            plan["command"][-3:],
            ["--model", "model with spaces", "a prompt; not a shell command"],
        )
        self.assertEqual(
            plan["environment_overrides"]["MCP_PROTOCOL_NEGOTIATION"], "legacy"
        )

    def test_polling_does_not_enable_any_channel(self):
        plan = self.plan("--delivery", "poll", "--", "--resume", "session")
        self.assertEqual(
            plan["command"], ["/tools with spaces/claude", "--resume", "session"]
        )
        self.assertEqual(plan["environment_overrides"], {"OMP_TANDEM_CHANNEL": "0"})

    def test_approved_plugin_uses_approved_channel_path(self):
        plan = self.plan("--approved-plugin", "omp-tandem@omp-tandem", "--no-webhook")
        self.assertIn("--channels", plan["command"])
        self.assertNotIn("--dangerously-load-development-channels", plan["command"])
        self.assertIn("plugin:omp-tandem@omp-tandem", plan["command"])
        self.assertEqual(plan["environment_overrides"]["OMP_TANDEM_WEBHOOK"], "0")

    def test_bad_port_or_plugin_cannot_launch_a_process(self):
        with (
            patch.object(launch.os, "execvpe") as execute,
            patch.object(launch.shutil, "which", return_value="claude"),
        ):
            for arguments in (
                ["--webhook-port", "65536"],
                ["--approved-plugin", "--injected"],
            ):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    launch.main(arguments)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
