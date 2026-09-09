"""Runtime lifecycle tests use a local fake uv, never network or real dependencies."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omp_tandem import bootstrap

FAKE_UV = """\
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
sys.stdin.read()

root = Path(sys.argv[sys.argv.index("--project") + 1])
environment = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
with open(os.environ["FAKE_UV_CALLS"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "environment": str(environment)}) + "\\n")
print("fake dependency stdout")
print("fake dependency stderr", file=sys.stderr)
if os.environ.get("FAKE_UV_FAIL"):
    (environment / "partial-install").touch()
    sys.exit(7)
venv.EnvBuilder(with_pip=False).create(environment)
python = environment / "bin" / "python"
site = subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
shutil.copytree(root / "src" / "omp_tandem", Path(site) / "omp_tandem")
"""


@unittest.skipUnless(
    os.name == "posix" and sys.version_info >= (3, 12),
    "bootstrap requires POSIX Python >=3.12",
)
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(
            self.stack.enter_context(tempfile.TemporaryDirectory())
        ).resolve()
        self.root = self.base / "plugin code ' with spaces"
        package = self.root / "src" / "omp_tandem"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text("def main():\n    return 0\n")
        (package / "__main__.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:], 'provider': os.environ.get('TEST_PROVIDER_TOKEN'), 'request': sys.stdin.read()}))\n"
        )
        (package / "bootstrap.py").write_bytes(Path(bootstrap.__file__).read_bytes())
        (self.root / "server.py").write_bytes(
            (Path(__file__).resolve().parents[1] / "server.py").read_bytes()
        )
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "omp-tandem"\nversion = "3.0.0"\nreadme = "README.md"\n'
        )
        (self.root / "README.md").write_text("fixture build input")
        (self.root / "uv.lock").write_text("fixture frozen input")
        self.cache = self.base / "private data ' with spaces"
        self.call_log = self.base / "uv-calls.jsonl"
        tools = self.base / "tools with spaces"
        tools.mkdir()
        fake_script = tools / "fake uv.py"
        fake_script.write_text(FAKE_UV)
        uv = tools / "uv"
        uv.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " -I "
            + shlex.quote(str(fake_script))
            + ' "$@"\n'
        )
        uv.chmod(0o700)
        omp = tools / "omp"
        omp.write_text("#!/bin/sh\nexit 99\n")
        omp.chmod(0o700)
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "PLUGIN_DATA": str(self.cache),
                    "CLAUDE_PLUGIN_DATA": "",
                    "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_UV_CALLS": str(self.call_log),
                    "FAKE_UV_FAIL": "",
                },
            )
        )
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))

    def calls(self):
        return (
            [json.loads(line) for line in self.call_log.read_text().splitlines()]
            if self.call_log.exists()
            else []
        )

    def test_cold_then_warm_reuses_validated_noneditable_runtime_outside_checkout(self):
        cwd = Path.cwd()
        python = bootstrap.prepare_runtime(self.root)
        self.assertTrue(python.is_file())
        self.assertTrue(python.is_relative_to(self.cache))
        self.assertEqual(bootstrap.prepare_runtime(self.root), python)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(Path(self.calls()[0]["cwd"]), cwd)
        command = self.calls()[0]["argv"]
        self.assertEqual(command[command.index("--project") + 1], str(self.root))
        self.assertIn("--frozen", command)
        self.assertIn("--no-editable", command)
        self.assertFalse((self.root / ".venv").exists())
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("fake dependency stdout", self.stderr.getvalue())

    def test_failed_install_never_publishes_ready_and_retry_can_succeed(self):
        with (
            patch.dict(os.environ, {"FAKE_UV_FAIL": "1"}),
            self.assertRaises(bootstrap.BootstrapError),
        ):
            bootstrap.prepare_runtime(self.root)
        self.assertEqual(list(self.cache.rglob("ready.json")), [])
        self.assertEqual(list(self.cache.rglob("partial-install")), [])
        python = bootstrap.prepare_runtime(self.root)
        self.assertTrue(python.is_file())
        self.assertEqual(len(self.calls()), 2)

    def test_successful_uv_with_broken_package_cannot_mark_ready(self):
        (self.root / "src" / "omp_tandem" / "cli.py").write_text(
            "raise ImportError('broken installation')\n"
        )
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.prepare_runtime(self.root)
        self.assertEqual(list(self.cache.rglob("ready.json")), [])
        self.assertIn("broken installation", self.stderr.getvalue())

    def test_concurrent_cold_starts_share_one_successful_generation(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(bootstrap.prepare_runtime, [self.root] * 4))
        self.assertEqual(len(set(paths)), 1)
        self.assertTrue(paths[0].is_file())
        self.assertEqual(len(self.calls()), 1)

    def test_build_input_changes_preserve_previous_live_generation(self):
        previous = bootstrap.prepare_runtime(self.root)
        (self.root / "README.md").write_text("changed wheel metadata input")
        current = bootstrap.prepare_runtime(self.root)
        self.assertNotEqual(current, previous)
        self.assertTrue(previous.is_file())
        self.assertEqual(len(self.calls()), 2)

    def test_corrupt_ready_runtime_is_repaired_without_overwriting_live_directory(self):
        previous = bootstrap.prepare_runtime(self.root)
        package = next(previous.parent.parent.rglob("site-packages/omp_tandem/cli.py"))
        package.write_text("raise ImportError('damaged runtime')\n")
        current = bootstrap.prepare_runtime(self.root)
        self.assertNotEqual(current, previous)
        self.assertTrue(previous.is_file())
        self.assertEqual(len(self.calls()), 2)

    def test_launcher_preserves_cwd_args_auth_environment_and_stdout_purity(self):
        invocation = self.base / "project ' with spaces"
        invocation.mkdir()
        # These hostile local modules must never replace the installed runtime.
        (invocation / "omp_tandem.py").write_text(
            "raise RuntimeError('cwd shadowed runtime')\n"
        )
        (invocation / "json.py").write_text(
            "raise RuntimeError('cwd shadowed stdlib')\n"
        )
        arguments = [
            "--model",
            "provider/model ' ; $(unsafe)",
            "--flag=value with spaces",
        ]
        result = subprocess.run(
            [sys.executable, "-I", str(self.root / "server.py"), *arguments],
            cwd=invocation,
            capture_output=True,
            text=True,
            input="client-initialize-frame",
            env={
                **os.environ,
                "TEST_PROVIDER_TOKEN": "synthetic-token",
                "PYTHONPATH": str(invocation),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "cwd": str(invocation),
                "args": arguments,
                "provider": "synthetic-token",
                "request": "client-initialize-frame",
            },
        )
        self.assertIn("fake dependency stdout", result.stderr)
        self.assertEqual(Path(self.calls()[0]["cwd"]), invocation)

    def test_prepare_emits_single_json_object_and_doctor_does_not_prepare_or_probe_auth(
        self,
    ):
        with (
            patch.object(
                bootstrap.subprocess,
                "run",
                side_effect=AssertionError("doctor must not run tools"),
            ),
            patch.object(
                bootstrap.Path,
                "home",
                side_effect=AssertionError("doctor must not inspect home"),
            ),
        ):
            self.assertEqual(bootstrap.main(self.root, ["--doctor"]), 0)
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report["authentication"], "not_checked")
        self.assertFalse(self.cache.exists())
        self.stdout.seek(0)
        self.stdout.truncate()
        self.assertEqual(bootstrap.main(self.root, ["--prepare"]), 0)
        report = json.loads(self.stdout.getvalue())
        self.assertEqual(report["status"], "prepared")
        self.assertTrue(Path(report["python"]).is_file())

    def test_missing_uv_is_actionable_without_creating_runtime(self):
        with patch.object(bootstrap.shutil, "which", return_value=None):
            self.assertEqual(bootstrap.main(self.root, []), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn(
            "https://docs.astral.sh/uv/getting-started/installation/",
            self.stderr.getvalue(),
        )
        self.assertFalse(self.cache.exists())

    def test_cache_inside_readonly_plugin_root_is_rejected(self):
        with (
            patch.dict(os.environ, {"PLUGIN_DATA": str(self.root / "data")}),
            self.assertRaises(bootstrap.BootstrapError),
        ):
            bootstrap.prepare_runtime(self.root)
        self.assertFalse((self.root / "data").exists())


if __name__ == "__main__":
    unittest.main()
