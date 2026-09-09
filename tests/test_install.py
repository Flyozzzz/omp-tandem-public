"""Standalone installation respects client configuration and argument boundaries."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import install


class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.package = root / "package with spaces ' and ; chars"
        self.invocation = root / "calling project with spaces"
        self.invocation.mkdir()
        (self.package / "src" / "omp_tandem").mkdir(parents=True)
        for name in (
            "server.py",
            "pyproject.toml",
            "uv.lock",
            "src/omp_tandem/__init__.py",
        ):
            (self.package / name).touch()
        self.config = self.invocation / "client-config.json"
        self.config.write_text('{"keep": true}')
        self.tools = {
            name: str(root / "tools with spaces" / name)
            for name in ("uv", "omp", "claude", "codex")
        }
        self.stack.enter_context(
            patch.object(
                install, "__file__", str(self.package / "scripts" / "install.py")
            )
        )
        self.stack.enter_context(
            patch.object(install.Path, "cwd", return_value=self.invocation)
        )
        self.stack.enter_context(
            patch.object(install.shutil, "which", side_effect=self.tools.get)
        )
        self.prepare = self.stack.enter_context(
            patch.object(install, "prepare_runtime", side_effect=self.fake_prepare)
        )
        self.run = self.stack.enter_context(
            patch.object(install.subprocess, "run", side_effect=self.fake_run)
        )
        self.calls = []
        self.register_exit = 0
        self.registry = []
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))

    def fake_prepare(self, root):
        print("dependency diagnostic", file=install.sys.stderr)
        return root.parent / "private cache" / "bin" / "python"

    def fake_run(self, command, *, cwd, check, capture_output, text):
        self.calls.append((command, cwd))
        if command[1:] == ["mcp", "list", "--json"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(self.registry), ""
            )
        if not self.register_exit:
            self.config.write_text(json.dumps({"registered": command}))
        return subprocess.CompletedProcess(
            command, self.register_exit, "registration diagnostic\n", ""
        )

    def report(self, *arguments):
        result = install.main(["--json", *arguments])
        return result, json.loads(self.stdout.getvalue())

    def test_claude_local_registration_preserves_paths_model_and_invocation(self):
        model = "provider/model ' ; $(touch unsafe)"
        result, report = self.report("--scope", "local", "--model", model)
        self.assertEqual(result, 0)
        command, cwd = self.calls[0]
        self.assertEqual(cwd, self.invocation)
        self.assertEqual(
            command[:8],
            [
                self.tools["claude"],
                "mcp",
                "add",
                "--scope",
                "local",
                "--transport",
                "stdio",
                "omp-tandem",
            ],
        )
        self.assertEqual(
            command[9:], [report["mcp"]["command"], *report["mcp"]["args"]]
        )
        self.assertIn(str(self.package / "server.py"), command)
        self.assertEqual(command[-2:], ["--model", model])
        self.assertNotIn("dependency diagnostic", self.stdout.getvalue())
        self.assertIn("registration diagnostic", self.stderr.getvalue())

    def test_codex_registration_uses_its_supported_user_command(self):
        result, report = self.report("--client", "codex", "--scope", "user")
        self.assertEqual(result, 0)
        command, cwd = self.calls[-1]
        self.assertEqual(
            command[:5], [self.tools["codex"], "mcp", "add", "omp-tandem", "--"]
        )
        self.assertNotIn("--scope", command)
        self.assertEqual(cwd, self.invocation)
        self.assertEqual(report["scope"], "user")

    def test_codex_existing_registration_is_not_overwritten_or_logged(self):
        original = self.config.read_bytes()
        self.registry = [
            {"name": "omp-tandem", "env": {"TOKEN": "private-registry-marker"}}
        ]
        result, report = self.report("--client", "codex")
        self.assertEqual(result, 4)
        self.assertEqual(report["code"], "registration_exists")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertNotIn(
            "private-registry-marker", self.stdout.getvalue() + self.stderr.getvalue()
        )

    def test_unrecognized_codex_registry_fails_without_changing_configuration(self):
        original = self.config.read_bytes()
        self.registry = {"unexpected": "shape"}
        result, report = self.report("--client", "codex")
        self.assertEqual(result, 4)
        self.assertEqual(report["code"], "registration_inspection_failed")
        self.assertEqual(self.config.read_bytes(), original)

    def test_unsupported_codex_scope_is_not_silently_registered(self):
        result, report = self.report("--client", "codex", "--scope", "local")
        self.assertEqual(result, 2)
        self.assertEqual(report["code"], "unsupported_scope")
        self.prepare.assert_not_called()
        self.run.assert_not_called()

    def test_check_cannot_install_or_change_client_configuration(self):
        original = self.config.read_bytes()
        result, report = self.report("--check")
        self.assertEqual(result, 0)
        self.assertEqual(report["authentication"], "not_checked")
        self.prepare.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)

    def test_no_register_prepares_without_requiring_client_or_mutating_configuration(
        self,
    ):
        del self.tools["claude"]
        original = self.config.read_bytes()
        result, report = self.report("--no-register")
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "prepared")
        self.assertIn("private cache", report["python"])
        self.assertEqual(report["commands"][0]["argv"][0], "claude")
        self.run.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)

    def test_other_clients_receive_exact_mcp_plan_without_registration(self):
        del self.tools["claude"]
        del self.tools["codex"]
        original = self.config.read_bytes()
        result, report = self.report("--client", "none", "--model", "provider/model")
        self.assertEqual(result, 0)
        self.assertEqual(
            report["mcp"],
            {
                "command": self.tools["uv"],
                "args": [
                    "run",
                    "--no-project",
                    "--python",
                    ">=3.12",
                    "python",
                    "-I",
                    str(self.package / "server.py"),
                    "--omp",
                    self.tools["omp"],
                    "--model",
                    "provider/model",
                ],
                "env": {},
            },
        )
        self.assertEqual(report["commands"], [])
        self.run.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)

    def test_missing_tools_block_without_authentication_or_config_access(self):
        del self.tools["uv"]
        del self.tools["omp"]
        with patch.object(
            install.Path, "home", side_effect=AssertionError("must not probe auth")
        ):
            result, report = self.report("--check")
        self.assertEqual(result, 2)
        self.assertEqual(set(report["missing"]), {"uv", "omp"})
        self.prepare.assert_not_called()
        self.run.assert_not_called()

    def test_dependency_failure_cannot_register_unusable_runtime(self):
        self.prepare.side_effect = install.BootstrapError("uv failed")
        original = self.config.read_bytes()
        result, report = self.report()
        self.assertEqual(result, 3)
        self.assertEqual(report["code"], "dependency_install_failed")
        self.run.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)

    def test_registration_conflict_never_removes_existing_entry(self):
        self.register_exit = 1
        original = self.config.read_bytes()
        result, report = self.report()
        self.assertEqual(result, 4)
        self.assertEqual(report["code"], "registration_failed")
        self.assertEqual([command[1:3] for command, _ in self.calls], [["mcp", "add"]])
        self.assertEqual(self.config.read_bytes(), original)

    def test_omp_registration_preserves_upgradeable_symlink(self):
        launcher = Path(self.tools["omp"])
        launcher.parent.mkdir()
        target = launcher.parent / "omp-version-1"
        target.touch()
        launcher.symlink_to(target)
        result, report = self.report("--no-register")
        self.assertEqual(result, 0)
        command = report["mcp"]["args"]
        configured = Path(command[command.index("--omp") + 1])
        replacement = launcher.parent / "omp-version-2"
        replacement.touch()
        launcher.unlink()
        launcher.symlink_to(replacement)
        target.unlink()
        self.assertEqual(configured.resolve(), replacement)


if __name__ == "__main__":
    unittest.main()
