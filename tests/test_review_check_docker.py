"""Opt-in namespace proof using a cached Python image, never credentials or models.

Unset OMP_TANDEM_TEST_DOCKER_IMAGE skips this module. Setting it opts into real
Docker: an unavailable daemon, missing image, or unsupported image is a failure.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import docker

from omp_tandem.review_checks import ReviewChecks
from omp_tandem.review_containers import resolve_container
from omp_tandem.work_workspace import WorkWorkspace
from tests import test_work_items as fixtures


@unittest.skipUnless(
    "OMP_TANDEM_TEST_DOCKER_IMAGE" in os.environ,
    "Set OMP_TANDEM_TEST_DOCKER_IMAGE to an already cached Python image",
)
class RealDockerReviewChecksTests(unittest.TestCase):
    def setUp(self):
        # Deliberately do not catch resolver/daemon failures and turn them into skips.
        self.container = resolve_container(os.environ["OMP_TANDEM_TEST_DOCKER_IMAGE"])
        self.engine = docker.APIClient(
            base_url="unix://" + self.container["socket"],
            version=self.container["api_version"],
            timeout=5,
        )
        self.engine.trust_env = False
        self.addCleanup(self.engine.close)
        self.case = fixtures.WorkItemsTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.workspace = WorkWorkspace(self.case.scope)
        self.original_head = self.case._git_commit(
            "backend.py", "VALUE = -1\n", "Original repository sentinel"
        )

    def prepare(self, code):
        definition = fixtures.plan()
        step = definition["steps"][0]
        definition["steps"] = [step]
        step["owned_files"] = ["backend.py", ".gitignore"]
        step["review_requirements"] = {"requires_shell": True}
        self.command = shlex.join(["python", "-B", "-c", textwrap.dedent(code).strip()])
        step["review_verification"] = {
            "checks": [
                {
                    "id": "behavior",
                    "criterion": "Exercise the exact submitted bytes inside Docker",
                    "command": self.command,
                }
            ]
        }
        self.case.revise(definition)
        self.case.agreed()
        self.case.authorize(
            allow_review_checks=True,
            review_check_container=self.container,
            review_check_timeout=15,
        )
        implementation = self.case.reserve()
        prepared = self.workspace.prepare(implementation, self.case.view()["plan"], [])
        self.case.store.started(
            implementation["attempt_id"], workspace=prepared["path"]
        )
        Path(prepared["path"], "backend.py").write_text("VALUE = 42\n")
        Path(prepared["path"], ".gitignore").write_text("cache/\n")
        output = self.workspace.finish(
            implementation, self.case.view()["plan"], prepared
        )
        self.case.store.confirm_stopped(implementation["attempt_id"])
        self.case.store.finish_attempt(
            implementation["attempt_id"],
            outcome="success",
            answer="Synthetic candidate ready",
            evidence=["Immutable synthetic Git capture"],
            output=output,
            cost_usd=0,
        )
        self.attempt = self.case.reserve(actor="claude", kind="review")
        self.case.report(self.attempt)
        self.checker = ReviewChecks(
            self.case.store, self.workspace, self.attempt["attempt_id"]
        )
        # unittest cleanups run LIFO: let the runner stop first, then remove only
        # a leftover whose complete identity belongs to this synthetic attempt.
        self.addCleanup(self.cleanup_owned_containers)
        self.addCleanup(self.checker.close)
        self.checkout = self.checker.directory / "checkout"
        return output

    def cleanup_owned_containers(self):
        runs = self.section()["runs"]
        if not runs:
            return
        self.assertEqual(self.engine.info()["ID"], self.container["daemon_id"])
        for run in runs:
            name = "omp-tandem-check-" + run["run_id"]
            try:
                remaining = self.engine.inspect_container(name)
            except docker.errors.NotFound:
                continue
            self.assertEqual(remaining["Name"], "/" + name)
            self.assertEqual(remaining["Image"], self.container["image_id"])
            self.assertEqual(
                remaining["Config"]["Labels"],
                {
                    "org.omp-tandem.scope": self.case.scope.key,
                    "org.omp-tandem.attempt": self.attempt["attempt_id"],
                    "org.omp-tandem.run": run["run_id"],
                },
                "Refusing cleanup of a container not owned by this test attempt",
            )
            container_id = remaining["Id"]
            self.assertRegex(container_id, r"^[0-9a-f]{64}$")
            self.engine.remove_container(container_id, force=True, v=True)
            with self.assertRaises(docker.errors.NotFound):
                self.engine.inspect_container(container_id)

    def section(self, section="verification"):
        return self.case.store.review_check_section(
            {
                "action": "get",
                "work_id": self.case.work_id,
                "step_id": "backend",
            },
            actor="operator",
            section=section,
            limit=4000,
        )

    def wait_finished(self):
        deadline = time.monotonic() + 30
        while self.checker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(self.checker.poll(), "Docker verifier did not finish")
        runs = self.section()["runs"]
        self.assertEqual(len(runs), 1)
        self.observed = self.section(runs[0]["section"])
        return self.observed

    def assert_removed(self, observed):
        container_id = observed["execution"]["container_id"]
        self.assertRegex(container_id, r"^[0-9a-f]{64}$")
        with self.assertRaises(docker.errors.NotFound):
            self.engine.inspect_container(container_id)
        self.assertTrue(observed["observation"]["process_confirmed_gone"])

    def assert_original_unchanged(self):
        self.assertEqual((self.case.root / "backend.py").read_text(), "VALUE = -1\n")
        self.assertFalse((self.case.root / "cache").exists())
        self.assertFalse((self.case.root / ".gitignore").exists())
        head = subprocess.run(
            ["git", "-C", str(self.case.root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        self.assertEqual(head, self.original_head)
        status = subprocess.run(
            ["git", "-C", str(self.case.root), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        self.assertEqual(status, "")

    def test_exact_input_stdout_stderr_and_no_inherited_provider_environment(self):
        output = self.prepare("""
            import os
            import sys
            from pathlib import Path
            assert Path.cwd() == Path('/workspace')
            assert Path('backend.py').read_bytes() == b'VALUE = 42\\n'
            assert 'PROVIDER_CANARY' not in os.environ
            assert 'HTTP_PROXY' not in os.environ
            assert 'http_proxy' not in os.environ
            namespace = {}
            exec(Path('backend.py').read_text(), namespace)
            assert namespace['VALUE'] == 42
            print('exact input stdout', flush=True)
            print('exact input stderr', file=sys.stderr, flush=True)
        """)
        config = self.case.root.parent / "docker-config"
        config.mkdir()
        (config / "config.json").write_text(
            json.dumps(
                {
                    "proxies": {
                        "default": {"httpProxy": "http://proxy-canary.invalid:8080"}
                    }
                }
            )
        )
        with patch.dict(
            os.environ,
            {"PROVIDER_CANARY": "host-only-canary", "DOCKER_CONFIG": str(config)},
        ):
            self.checker.start()
            observed = self.wait_finished()
        self.assertEqual(observed["observation"]["result"], "passed", observed)
        self.assertIn("exact input stdout", observed["content"])
        self.assertIn("exact input stderr", observed["content"])
        self.assertNotIn("host-only-canary", observed["content"])
        self.assertEqual(observed["check"]["command"], self.command)
        self.assertEqual(observed["scope"]["digest"], output["commit"])
        self.assertTrue(observed["observation"]["input_unchanged"])
        self.assert_removed(observed)
        self.assert_original_unchanged()

    def test_double_fork_setsid_helper_cannot_write_after_container_exit(self):
        self.prepare("""
            import os
            import time
            from pathlib import Path
            cache = Path('cache')
            cache.mkdir()
            child = os.fork()
            if child == 0:
                os.setsid()
                if os.fork() != 0:
                    os._exit(0)
                for fd in (0, 1, 2):
                    os.close(fd)
                (cache / 'armed').write_text('detached with all standard streams closed')
                time.sleep(3)
                (cache / 'late').write_text('escaped container lifetime')
                os._exit(0)
            os.waitpid(child, 0)
            deadline = time.monotonic() + 5
            while not (cache / 'armed').exists():
                assert time.monotonic() < deadline, 'helper did not arm'
                time.sleep(0.01)
            print('detached helper armed', flush=True)
        """)
        self.checker.start()
        observed = self.wait_finished()
        self.assertEqual(observed["observation"]["result"], "passed", observed)
        self.assertIn("detached helper armed", observed["content"])
        self.assertEqual(
            (self.checkout / "cache" / "armed").read_text(),
            "detached with all standard streams closed",
        )
        self.assert_removed(observed)
        # Outwait the helper's intended write, even if the runner finished instantly.
        time.sleep(3.2)
        self.assertFalse((self.checkout / "cache" / "late").exists())
        self.assert_original_unchanged()

    def test_cancellation_removes_the_running_owned_container(self):
        self.prepare("""
            import time
            from pathlib import Path
            Path('cache').mkdir()
            Path('cache/started').write_text('running inside the container')
            time.sleep(60)
        """)
        self.checker.start()
        deadline = time.monotonic() + 10
        while not (self.checkout / "cache" / "started").exists():
            self.assertIsNone(self.checker.poll(), "Command exited before cancellation")
            self.assertLess(
                time.monotonic(), deadline, "Container did not become ready"
            )
            time.sleep(0.02)
        runs = self.section()["runs"]
        self.assertEqual(len(runs), 1)
        running = self.engine.inspect_container("omp-tandem-check-" + runs[0]["run_id"])
        self.assertTrue(running["State"]["Running"])
        self.checker.cancel()
        observed = self.wait_finished()
        self.assertEqual(observed["execution"]["container_id"], running["Id"])
        self.assertEqual(observed["observation"]["result"], "interrupted", observed)
        self.assert_removed(observed)
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])
        self.assert_original_unchanged()

    def test_source_mutation_never_passes_or_changes_the_original_repository(self):
        self.prepare("""
            from pathlib import Path
            assert Path('backend.py').read_bytes() == b'VALUE = 42\\n'
            Path('backend.py').write_text('VALUE = 99\\n')
            print('modified only the disposable submitted-copy input', flush=True)
        """)
        self.checker.start()
        observed = self.wait_finished()
        self.assertEqual((self.checkout / "backend.py").read_text(), "VALUE = 99\n")
        self.assertFalse(observed["observation"]["input_unchanged"])
        self.assertNotEqual(observed["observation"]["result"], "passed")
        self.assertNotEqual(self.section()["status"], "passed")
        self.assert_removed(observed)
        self.assertIsNone(self.case.view()["steps"][0]["acceptance"])
        self.assert_original_unchanged()
