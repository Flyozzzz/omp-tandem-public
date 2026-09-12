"""Deterministic contracts of the real-OMP compatibility verifier.

These tests exercise the verifier's pure decision points (capability predicate,
acquisition preflight, evidence placement) without downloading or running OMP.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

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
            cached.parent.mkdir(parents=True)
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
