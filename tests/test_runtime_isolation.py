"""Operator selection and state safety; no provider or real dependency requests."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omp_tandem import bootstrap
from omp_tandem.cli import main
from omp_tandem.runtime_guard import (
    STATE_SCHEMA_VERSION,
    StateCompatibilityError,
    guard_database,
)
from omp_tandem.task_store import initialize_database
from omp_tandem.workspace import resolve_scope
from tests import test_bootstrap as fixtures


@unittest.skipUnless(
    os.name == "posix" and sys.version_info >= (3, 12), "POSIX Python >=3.12"
)
class RuntimeSelectionTests(unittest.TestCase):
    setUp = fixtures.BootstrapTests.setUp

    def observed_version(self, python):
        return subprocess.check_output(
            [str(python), "-I", "-c", "import omp_tandem; print(omp_tandem.VERSION)"],
            text=True,
        ).strip()

    def test_pin_survives_source_update_and_refresh_changes_only_future_selection(self):
        source = self.root / "src/omp_tandem/__init__.py"
        source.write_text("VERSION = 'old'\n")
        old = bootstrap.prepare_runtime(self.root)
        old_key = old.parents[2].name
        bootstrap.pin_runtime(self.root, old_key)
        source.write_text("VERSION = 'candidate'\n")
        candidate = bootstrap.select_runtime(self.root, candidate=True)
        self.assertNotEqual(candidate, old)
        self.assertEqual(bootstrap.select_runtime(self.root), old)
        self.assertEqual(self.observed_version(old), "old")
        self.assertEqual(self.observed_version(candidate), "candidate")
        inspection = bootstrap.runtime_selection(self.root)
        self.assertEqual(inspection["pinned"]["key"], old_key)
        self.assertEqual(inspection["candidate"]["key"], candidate.parents[2].name)
        bootstrap.pin_runtime(self.root)
        self.assertEqual(bootstrap.select_runtime(self.root), candidate)
        self.assertEqual(self.observed_version(old), "old")
        bootstrap.unpin_runtime(self.root)
        source.write_text("VERSION = 'normal-update'\n")
        self.assertEqual(
            self.observed_version(bootstrap.select_runtime(self.root)), "normal-update"
        )

    def test_stale_pin_stops_instead_of_falling_forward(self):
        python = bootstrap.prepare_runtime(self.root)
        bootstrap.pin_runtime(self.root, python.parents[2].name)
        package = next(python.parents[1].rglob("site-packages/omp_tandem/cli.py"))
        package.write_text("def main(): return 'changed but still importable'\n")
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.select_runtime(self.root)
        self.assertEqual(bootstrap.runtime_selection(self.root)["status"], "blocked")
        repaired = bootstrap.prepare_runtime(self.root)
        self.assertNotEqual(repaired, python)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.select_runtime(self.root)
        bootstrap.pin_runtime(self.root)
        self.assertEqual(bootstrap.select_runtime(self.root), repaired)

    def test_unsafe_marker_and_executable_path_are_not_pin_authority(self):
        python = bootstrap.prepare_runtime(self.root)
        key = python.parents[2].name
        marker = python.parents[2] / "ready.json"
        original = json.loads(marker.read_text())
        unsafe = {**original, "generation": "../../outside"}
        marker.write_text(json.dumps(unsafe))
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.pin_runtime(self.root, key)
        marker.write_text(json.dumps(original))
        moved = marker.with_name("saved.json")
        marker.rename(moved)
        marker.symlink_to(moved)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.pin_runtime(self.root, key)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.pin_runtime(self.root, str(python))


class CandidateStateTests(unittest.TestCase):
    def test_candidate_smoke_populates_only_fresh_state_and_preserves_live_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            live = root / "live"
            scope = resolve_scope(live, root)
            database = initialize_database(scope)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE live_sentinel (value TEXT)")
                db.execute(
                    "INSERT INTO live_sentinel VALUES ('active work must remain')"
                )
            before = database.read_bytes()
            auth = live / "auth.json"
            auth.write_text('{"fixture": "must-not-copy"}')
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"OMP_TANDEM_STATE_DIR": str(live)}),
                redirect_stdout(output),
            ):
                main(
                    [
                        "--candidate",
                        "--candidate-smoke",
                        "--project-root",
                        str(root),
                        "--omp",
                        "unavailable-test-peer",
                    ]
                )
            report = json.loads(output.getvalue())
            candidate = Path(report["state_base"])
            self.addCleanup(shutil.rmtree, candidate)
            self.assertFalse(candidate.is_relative_to(live))
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(auth.read_text(), '{"fixture": "must-not-copy"}')
            candidate_db = Path(report["state_directory"]) / "tasks.sqlite3"
            with sqlite3.connect(candidate_db) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
                )
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone()[0],
                    STATE_SCHEMA_VERSION,
                )
                self.assertIsNone(
                    db.execute(
                        "SELECT name FROM sqlite_master WHERE name='live_sentinel'"
                    ).fetchone()
                )
                self.assertEqual(
                    db.execute("SELECT project_root FROM bridge_scope").fetchone()[0],
                    str(root),
                )
            self.assertFalse(list(candidate.rglob("auth.json")))
            self.assertEqual(report["diagnostics"]["runtime"]["short_task"], "not_run")

    def test_candidate_cannot_select_live_state_or_inherit_managed_authority(self):
        for arguments in (
            ["--state-dir", "/unsafe"],
            ["--migrate-only"],
            ["--work-token-file", "/unsafe"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                main(["--candidate", *arguments])

    def test_newer_schema_is_rejected_before_any_database_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            scope = resolve_scope(root / "state", root)
            database = initialize_database(scope)
            with sqlite3.connect(database) as db:
                db.execute(f"PRAGMA user_version={STATE_SCHEMA_VERSION + 1}")
                db.execute("CREATE TABLE future_work (value TEXT)")
                db.execute("INSERT INTO future_work VALUES ('preserve')")
            before = database.read_bytes()
            with self.assertRaises(StateCompatibilityError):
                initialize_database(scope)
            self.assertEqual(database.read_bytes(), before)
            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute("SELECT value FROM future_work").fetchone()[0],
                    "preserve",
                )

    def test_legacy_guard_stamp_rolls_back_with_failed_initialization(self):
        with sqlite3.connect(":memory:", isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            guard_database(db)
            db.rollback()
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            db.execute("BEGIN IMMEDIATE")
            guard_database(db)
            db.commit()
            self.assertEqual(
                db.execute("PRAGMA user_version").fetchone()[0], STATE_SCHEMA_VERSION
            )
