"""Deterministic contracts of the real-OMP compatibility verifier.

These tests exercise the verifier's pure decision points (capability predicate,
acquisition preflight, evidence placement) without downloading or running OMP.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import verify_omp

PIN = "0" * 64
MANIFEST = {
    "omp_version": "18.1.13",
    "download_base": "https://example.invalid/releases",
    "assets": {"test-arch": {"name": "omp-test", "sha256": PIN}},
}


def _alive(**overrides):
    observed = {
        "requests_before_release": 52,
        "requests_after_release": 52,
        "steps_left_after_release": 2,
        "parent_alive": True,
    }
    observed.update(overrides)
    return observed


class AbortGateTests(unittest.TestCase):
    def test_gate_requires_live_parent_before_cleanup(self):
        self.assertTrue(verify_omp.abort_gate_supported(_alive()))
        # A parent that no longer answers cannot prove that the abort suppressed
        # late delivery: silence from a dead process is not abort semantics.
        self.assertFalse(verify_omp.abort_gate_supported(_alive(parent_alive=False)))
        self.assertFalse(verify_omp.abort_gate_supported(_alive(parent_alive=None)))
        missing = _alive()
        del missing["parent_alive"]
        self.assertFalse(verify_omp.abort_gate_supported(missing))

    def test_gate_still_needs_no_late_requests_and_unconsumed_script(self):
        self.assertFalse(
            verify_omp.abort_gate_supported(_alive(requests_after_release=53))
        )
        self.assertFalse(
            verify_omp.abort_gate_supported(_alive(steps_left_after_release=0))
        )


class AcquisitionPreflightTests(unittest.TestCase):
    def _args(self, root, omp=None):
        return argparse.Namespace(
            cache_dir=root / "cache", report=root / "report.json", omp=omp
        )

    def test_supplied_binary_is_preflighted_without_acquisition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supplied = root / "omp"
            supplied.write_bytes(b"binary")
            report = {"checks": []}
            plan = verify_omp.preflight(
                self._args(root, omp=supplied), MANIFEST, report, key="test-arch"
            )
            self.assertEqual(plan["source"], "provided")
            self.assertFalse(plan["acquisition_required"])
            self.assertEqual(plan["target"], supplied.resolve())
            self.assertEqual(report["preflight"]["expected_sha256"], PIN)
            self.assertEqual(report["preflight"]["asset"], "omp-test")
            self.assertEqual(report["checks"][-1]["status"], "passed")

    def test_cached_binary_is_reused_and_missing_cache_requires_acquisition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {"checks": []}
            plan = verify_omp.preflight(
                self._args(root), MANIFEST, report, key="test-arch"
            )
            self.assertEqual(plan["source"], "official_release_cache")
            self.assertTrue(plan["acquisition_required"])
            cached = root / "cache" / "18.1.13" / "omp-test"
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"cached")
            plan = verify_omp.preflight(
                self._args(root), MANIFEST, {"checks": []}, key="test-arch"
            )
            self.assertFalse(plan["acquisition_required"])
            self.assertEqual(plan["target"], cached.resolve())

    def test_unpinned_platform_is_an_environment_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {"checks": []}
            with self.assertRaises(RuntimeError):
                verify_omp.preflight(self._args(root), MANIFEST, report, key="other")
            self.assertEqual(report["checks"][-1]["status"], "failed")
            self.assertEqual(verify_omp.failure_class(report), "environment")

    def test_wrong_supplied_digest_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supplied = root / "omp"
            supplied.write_bytes(b"not the pinned bytes")
            report = {"checks": []}
            args = self._args(root, omp=supplied)
            plan = verify_omp.preflight(args, MANIFEST, report, key="test-arch")
            with self.assertRaises(RuntimeError):
                verify_omp.verify_binary(plan, report)
            self.assertEqual(report["omp"]["expected_sha256"], PIN)
            self.assertNotEqual(report["omp"]["sha256"], PIN)
            self.assertEqual(report["checks"][-1]["name"], "official_binary_sha256")
            self.assertEqual(report["checks"][-1]["status"], "failed")
            self.assertEqual(verify_omp.failure_class(report), "acquisition")

    def test_acquisition_timeout_leaves_probes_not_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {"checks": []}
            plan = verify_omp.preflight(
                self._args(root), MANIFEST, report, key="test-arch"
            )

            def stalled(_request, timeout):
                raise TimeoutError("Compatibility command exceeded its deadline")

            with self.assertRaises(TimeoutError):
                verify_omp.acquire(plan, report, opener=stalled)
            self.assertEqual(
                report["checks"][-1]["name"], "official_binary_acquisition"
            )
            self.assertEqual(report["checks"][-1]["status"], "failed")
            self.assertEqual(verify_omp.failure_class(report), "acquisition")
            self.assertEqual(verify_omp.probe_status(report), "not_run")
            self.assertFalse(plan["target"].exists())


class EnvironmentClassificationTests(unittest.TestCase):
    """Reviewer counterexamples: setup failures must never read as probe results."""

    def _args(self, root, omp=None):
        return argparse.Namespace(
            cache_dir=root / "cache", report=root / "report.json", omp=omp
        )

    def test_cache_path_that_is_a_file_is_an_environment_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "cache").write_text("not a directory")
            report = {"checks": []}
            with self.assertRaises(RuntimeError) as failure:
                verify_omp.preflight(
                    self._args(root), MANIFEST, report, key="test-arch"
                )
            self.assertIn("not a directory", str(failure.exception))
            self.assertEqual(
                [c["name"] for c in report["checks"]], ["binary_preflight"]
            )
            self.assertEqual(report["checks"][0]["status"], "failed")
            self.assertEqual(verify_omp.failure_class(report), "environment")
            self.assertEqual(verify_omp.probe_status(report), "not_run")

    def test_failure_outside_any_check_is_classified_by_phase(self):
        passed_setup = [
            {"name": "binary_preflight", "status": "passed"},
            {"name": "official_binary_sha256", "status": "passed"},
            {"name": "sdk_pin", "status": "passed"},
        ]
        setup_failure = {"status": "failed", "phase": "setup", "checks": passed_setup}
        self.assertEqual(verify_omp.failure_class(setup_failure), "environment")
        self.assertEqual(verify_omp.probe_status(setup_failure), "not_run")
        acquisition_failure = {
            "status": "failed",
            "phase": "acquisition",
            "checks": passed_setup[:1],
        }
        self.assertEqual(verify_omp.failure_class(acquisition_failure), "acquisition")
        self.assertEqual(verify_omp.probe_status(acquisition_failure), "not_run")
        probe_failure = {"status": "failed", "phase": "probes", "checks": passed_setup}
        self.assertEqual(verify_omp.failure_class(probe_failure), "probe")
        self.assertEqual(verify_omp.probe_status(probe_failure), "not_run")
        version_failure = {
            "status": "failed",
            "phase": "setup",
            "checks": [
                *passed_setup,
                {"name": "actual_omp_version", "status": "failed"},
            ],
        }
        self.assertEqual(verify_omp.failure_class(version_failure), "environment")
        passed = {"status": "passed", "phase": "complete", "checks": passed_setup}
        self.assertIsNone(verify_omp.failure_class(passed))

    def test_main_reports_environment_failure_before_probes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supplied = root / "omp"
            supplied.write_bytes(b"pinned stand-in")
            report_path = root / "compat.json"
            argv = [
                "verify_omp.py",
                "--cache-dir",
                str(root / "cache"),
                "--omp",
                str(supplied),
                "--report",
                str(report_path),
            ]
            manifest = json.loads(
                (verify_omp.ROOT / "config" / "omp-compatibility.json").read_text()
            )
            pinned = manifest["assets"][verify_omp.platform_key()]["sha256"]
            with (
                patch.object(sys, "argv", argv),
                patch.object(verify_omp, "digest", return_value=pinned),
                patch.object(
                    verify_omp.tempfile,
                    "TemporaryDirectory",
                    side_effect=OSError("isolated environment unavailable"),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                exit_code = verify_omp.main()
            report = json.loads(report_path.read_text())
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["phase"], "setup")
            self.assertEqual(report["failure_class"], "environment")
            self.assertEqual(report["probes"], "not_run")
            self.assertFalse(
                any(c["name"] not in verify_omp.SETUP_CHECKS for c in report["checks"]),
                "no probe check may be recorded when OMP never ran",
            )


class ProbeStatusTests(unittest.TestCase):
    def test_probe_failure_and_unsupported_capability_are_distinct(self):
        failed_probe = {
            "checks": [
                {"name": "official_binary_sha256", "status": "passed"},
                {"name": "helper_scout_child_boundary", "status": "failed"},
            ]
        }
        self.assertEqual(verify_omp.failure_class(failed_probe), "probe")
        self.assertEqual(verify_omp.probe_status(failed_probe), "failed")
        unsupported = {
            "status": "passed",
            "checks": [
                {"name": "official_binary_sha256", "status": "passed"},
                {"name": "helper_scout_child_boundary", "status": "passed"},
            ],
            "delegation": {"available": False, "unsatisfied_gates": ["x"]},
        }
        self.assertIsNone(verify_omp.failure_class(unsupported))
        self.assertEqual(verify_omp.probe_status(unsupported), "passed")


class EvidencePlacementTests(unittest.TestCase):
    def test_failed_run_never_overwrites_a_passing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compat.json"
            path.write_text(json.dumps({"status": "passed"}))
            failed = {"status": "failed", "checks": []}
            destination = verify_omp.evidence_destination(path, failed, now=1700000000)
            self.assertNotEqual(destination, path)
            self.assertEqual(destination.parent, path.parent)
            self.assertTrue(destination.name.startswith("compat.failed-"))
            self.assertEqual(json.loads(path.read_text())["status"], "passed")

    def test_passing_or_first_run_uses_the_requested_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compat.json"
            passed = {"status": "passed", "checks": []}
            self.assertEqual(verify_omp.evidence_destination(path, passed), path)
            failed = {"status": "failed", "checks": []}
            self.assertEqual(verify_omp.evidence_destination(path, failed), path)
            path.write_text(json.dumps({"status": "failed"}))
            self.assertEqual(verify_omp.evidence_destination(path, failed), path)
            path.write_text("not json")
            self.assertNotEqual(verify_omp.evidence_destination(path, failed), path)


if __name__ == "__main__":
    unittest.main()
