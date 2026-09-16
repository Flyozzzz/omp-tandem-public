"""Capture of work that lives in a nested Git repository inside the granted root."""

from __future__ import annotations

import fcntl
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.reviews import CaptureReservation, ReviewRequest, ReviewStore
from omp_tandem.task_store import initialize_database
from omp_tandem.workspace import resolve_scope


class _Swapped:
    """Keep the real descriptor context alive while the test swaps the pathname."""

    def __init__(self, handle, fd):
        self.handle, self.fd = handle, fd

    def __enter__(self):
        return self.fd

    def __exit__(self, *details):
        return self.handle.__exit__(*details)


class NestedRepositoryTests(unittest.TestCase):
    """A subtree that is its own repository is still inside the granted root."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "project"
        self.root.mkdir()
        self.nested = self.root / "work"
        self.scope = resolve_scope(self.home / "state", self.root)
        self.db_path = self.scope.directory / "state.sqlite3"
        self.store = ReviewStore(self.db_path, self.scope, ArtifactStore(self.db_path))

    def git(self, cwd, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    def repository(self, path: Path) -> None:
        path.mkdir(exist_ok=True)
        self.git(path, "init", "-q")
        self.git(path, "config", "user.email", "review@example.invalid")
        self.git(path, "config", "user.name", "Nested Regression")

    def initialize(self) -> str:
        """Parent repository with its own history, plus a nested repository inside it."""
        self.repository(self.root)
        (self.root / "outer.txt").write_bytes(b"outer\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "outer")
        self.repository(self.nested)
        (self.nested / "app.py").write_bytes(b"print('v1')\n")
        (self.nested / "kept.txt").write_bytes(b"kept\n")
        self.git(self.nested, "add", ".")
        self.git(self.nested, "commit", "-qm", "spec")
        return self.git(self.nested, "rev-parse", "HEAD")

    def manifest(self, review_id):
        return json.loads(self.store.read(review_id, limit=50000)["content"])

    def test_worktree_capture_uses_the_nested_repository_history(self):
        """The nested base must resolve and modified files must read as modified."""
        base = self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the nested change",
                source="worktree",
                review_directory="work",
                paths=["work/app.py"],
                base=base,
            )
        )
        manifest = self.manifest(created["review_id"])
        row = next(row for row in manifest["files"] if row["path"] == "work/app.py")
        self.assertEqual(row["change"], "modified")
        self.assertTrue(row["base"]["exists"])
        self.assertEqual(manifest["git"]["base_commit"], base)

    def test_automatic_selection_inside_the_nested_repository(self):
        """Without explicit paths the selection comes from the nested repository."""
        base = self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        (self.nested / "added.txt").write_bytes(b"new\n")
        created = self.store.create(
            ReviewRequest(
                requirements="Review whatever changed",
                source="worktree",
                review_directory="work",
            )
        )
        manifest = self.manifest(created["review_id"])
        selected = {row["path"] for row in manifest["files"] if row["role"] == "change"}
        self.assertEqual(selected, {"work/app.py", "work/added.txt"})
        self.assertEqual(manifest["git"]["base_commit"], base)

    def test_staged_capture_inside_the_nested_repository(self):
        """Staged reviews resolve against the nested index, not the parent one."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('staged')\n")
        self.git(self.nested, "add", "app.py")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the staged change",
                source="staged",
                review_directory="work",
            )
        )
        manifest = self.manifest(created["review_id"])
        row = next(row for row in manifest["files"] if row["path"] == "work/app.py")
        self.assertEqual(row["change"], "modified")
        self.assertEqual(row["selected"]["sha256"], row["staged"]["sha256"])

    def test_parent_selection_skips_the_nested_repository_instead_of_failing(self):
        """A nested repository must not make a parent-root capture unusable."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        (self.root / "outer.txt").write_bytes(b"outer changed\n")
        created = self.store.create(
            ReviewRequest(requirements="Review the parent change", source="worktree")
        )
        manifest = self.manifest(created["review_id"])
        selected = {row["path"] for row in manifest["files"] if row["role"] == "change"}
        self.assertEqual(selected, {"outer.txt"})

    def test_paths_reaching_into_a_nested_repository_are_refused_with_guidance(self):
        """Silent wrong answers are worse than a refusal that names the fix."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        with self.assertRaises(ValueError) as error:
            self.store.create(
                ReviewRequest(
                    requirements="Review the nested change",
                    source="worktree",
                    paths=["work/app.py"],
                )
            )
        self.assertIn("work", str(error.exception))

    def test_root_must_stay_inside_the_granted_project(self):
        """The root selector never becomes an escape hatch."""
        self.initialize()
        for value in ("../outside", "/etc", "work/../..", ".git"):
            with self.subTest(root=value), self.assertRaises(ValueError):
                self.store.create(
                    ReviewRequest(
                        requirements="Escape attempt",
                        source="worktree",
                        review_directory=value,
                    )
                )

    def test_root_pointing_at_a_plain_directory_still_works(self):
        """A subdirectory that is not its own repository keeps parent semantics."""
        self.repository(self.root)
        module = self.root / "module"
        module.mkdir()
        (module / "unit.py").write_bytes(b"print('v1')\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "base")
        (module / "unit.py").write_bytes(b"print('v2')\n")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the module change",
                source="worktree",
                review_directory="module",
                paths=["module/unit.py"],
            )
        )
        manifest = self.manifest(created["review_id"])
        row = next(row for row in manifest["files"] if row["path"] == "module/unit.py")
        self.assertEqual(row["change"], "modified")

    def test_review_run_capture_child_accepts_the_directory(self):
        """The scenario path publishes through a separate process; it must agree."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        database = initialize_database(self.scope)
        reservation = CaptureReservation(str(uuid4()), str(uuid4()), str(uuid4()))
        lease = (self.scope.directory / f"review-owner-{reservation.owner}.lock").open(
            "a"
        )
        self.addCleanup(lease.close)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = ReviewRequest(
            requirements="Review the nested change",
            source="worktree",
            review_directory="work",
        )
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS review_runs (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, capture_id TEXT,
                capture_started INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL,
                status TEXT NOT NULL, deadline REAL NOT NULL, review_id TEXT,
                payload_json TEXT NOT NULL, updated REAL NOT NULL)""")
            db.execute(
                "INSERT INTO review_runs VALUES (?, ?, ?, 1, 'capture', 'starting', ?, NULL, ?, ?)",
                (
                    reservation.run_id,
                    reservation.owner,
                    reservation.review_id,
                    time.time() + 60,
                    json.dumps({"request": request.model_dump()}),
                    time.time(),
                ),
            )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "omp_tandem.capture_worker",
                "--state-dir",
                str(self.scope.base),
                "--project-root",
                str(self.root),
                "--run-id",
                reservation.run_id,
                "--owner",
                reservation.owner,
                "--review-id",
                reservation.review_id,
            ],
            input=request.model_dump_json().encode(),
            capture_output=True,
            cwd=self.home,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        summary = json.loads(result.stdout)
        self.assertEqual(summary["change_count"], 1)

    def test_assessment_reads_the_captured_repository_not_the_launch_one(self):
        """A recorded directory is reused, so an unchanged snapshot stays current."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('staged')\n")
        self.git(self.nested, "add", "app.py")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the staged change",
                source="staged",
                review_directory="work",
            )
        )
        assessment = self.store.assess(created["review_id"])
        self.assertEqual(assessment["status"], "current_selected_state")
        self.assertEqual(assessment["changed_paths"], [])
        self.assertEqual(assessment["unknown"], [])
        self.git(self.nested, "rm", "-q", "--cached", "app.py")
        stale = self.store.assess(created["review_id"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["changed_paths"], ["work/app.py"])

    def test_assessment_of_a_nested_commit_snapshot_stays_current(self):
        """A commit that only the nested repository knows must remain reachable."""
        base = self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        self.git(self.nested, "add", "app.py")
        self.git(self.nested, "commit", "-qm", "second")
        head = self.git(self.nested, "rev-parse", "HEAD")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the nested commit",
                source="commit",
                review_directory="work",
                base=base,
                commit=head,
            )
        )
        assessment = self.store.assess(created["review_id"])
        self.assertEqual(assessment["unknown"], [])
        self.assertEqual(assessment["status"], "current_selected_state")
        manifest = self.manifest(created["review_id"])
        row = next(row for row in manifest["files"] if row["path"] == "work/app.py")
        self.assertEqual(row["change"], "modified")

    def test_tracked_subtree_that_became_a_repository_is_refused(self):
        """The enclosing repository still lists those files; the rule must still hold."""
        self.repository(self.root)
        tracked = self.root / "work"
        tracked.mkdir()
        (tracked / "app.py").write_bytes(b"print('v1')\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "parent owns work/")
        self.repository(tracked)
        (tracked / "app.py").write_bytes(b"print('v2')\n")
        with self.assertRaises(ValueError) as error:
            self.store.create(
                ReviewRequest(requirements="Review everything", source="worktree")
            )
        self.assertIn("review_directory='work'", str(error.exception))

    def test_non_git_project_root_still_refuses_nested_selections(self):
        """Without the check a nested tracked file would read as newly added."""
        self.repository(self.nested)
        (self.nested / "app.py").write_bytes(b"print('v1')\n")
        self.git(self.nested, "add", ".")
        self.git(self.nested, "commit", "-qm", "spec")
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        with self.assertRaises(ValueError) as error:
            self.store.create(
                ReviewRequest(
                    requirements="Review the nested change",
                    source="worktree",
                    paths=["work/app.py"],
                )
            )
        self.assertIn("review_directory='work'", str(error.exception))

    def test_a_directory_swapped_after_validation_cannot_redirect_the_capture(self):
        """The child follows the opened descriptor, not the name it was opened by.

        The decoy repository holds a file of the same name, so a capture that followed
        the swapped pathname would save its bytes instead of the opened directory's.
        """
        self.initialize()
        decoy = self.home / "decoy"
        self.repository(decoy)
        (decoy / "app.py").write_bytes(b"outside bytes\n")
        self.git(decoy, "add", ".")
        self.git(decoy, "commit", "-qm", "decoy")
        (self.nested / "app.py").write_bytes(b"print('staged')\n")
        self.git(self.nested, "add", "app.py")
        expected = self.git(self.nested, "rev-parse", "HEAD")
        real = self.store._descriptor

        def swap(directory):
            handle = real(directory)
            fd = handle.__enter__()
            if directory == "work" and not (self.root / "moved").exists():
                # A plain directory, not a symlink: every path check still passes,
                # so only a descriptor-bound child keeps reading the opened one.
                (self.root / "work").rename(self.root / "moved")
                shutil.copytree(decoy, self.root / "work", symlinks=True)
            return _Swapped(handle, fd)

        with patch.object(self.store, "_descriptor", side_effect=swap):
            try:
                created = self.store.create(
                    ReviewRequest(
                        requirements="Review the staged change",
                        source="staged",
                        review_directory="work",
                    )
                )
            except ValueError:
                return  # refusing the swapped capture is also an acceptable outcome
        manifest = self.manifest(created["review_id"])
        # The captured history must be the opened directory's, not the decoy's.
        self.assertEqual(manifest["git"]["base_commit"], expected)
        for row in manifest["files"]:
            for section in ("selected", "base", "staged"):
                if row[section]["exists"]:
                    content = self.store.read(
                        created["review_id"], section, row["path"]
                    )["content"]
                    self.assertNotIn("outside bytes", content)

    def test_worktree_capture_cannot_mix_history_with_replacement_files(self):
        """Git history and live bytes must come from the same opened directory."""
        self.initialize()
        decoy = self.home / "decoy"
        self.repository(decoy)
        (decoy / "app.py").write_bytes(b"outside bytes\n")
        self.git(decoy, "add", ".")
        self.git(decoy, "commit", "-qm", "decoy")
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        expected = self.git(self.nested, "rev-parse", "HEAD")
        real = self.store._descriptor

        def swap(directory):
            handle = real(directory)
            fd = handle.__enter__()
            if directory == "work" and not (self.root / "moved").exists():
                (self.root / "work").rename(self.root / "moved")
                shutil.copytree(decoy, self.root / "work", symlinks=True)
            return _Swapped(handle, fd)

        with patch.object(self.store, "_descriptor", side_effect=swap):
            try:
                created = self.store.create(
                    ReviewRequest(
                        requirements="Review the nested change",
                        source="worktree",
                        review_directory="work",
                        paths=["work/app.py"],
                    )
                )
            except ValueError:
                return  # refusing the replaced directory is also acceptable
        manifest = self.manifest(created["review_id"])
        self.assertEqual(manifest["git"]["base_commit"], expected)
        content = self.store.read(created["review_id"], "selected", "work/app.py")[
            "content"
        ]
        self.assertNotIn("outside bytes", content)

    def test_a_retry_cannot_publish_a_replacement_repository(self):
        """The binding spans every attempt, so a mutation retry keeps its directory."""
        self.initialize()
        decoy = self.home / "decoy"
        self.repository(decoy)
        (decoy / "app.py").write_bytes(b"outside bytes\n")
        self.git(decoy, "add", ".")
        self.git(decoy, "commit", "-qm", "decoy")
        (self.nested / "app.py").write_bytes(b"print('staged')\n")
        self.git(self.nested, "add", "app.py")
        expected = self.git(self.nested, "rev-parse", "HEAD")
        original = self.store._index
        calls = 0

        def mutate_then_swap(directory=""):
            nonlocal calls
            result = original(directory)
            calls += 1
            if calls == 1:
                # Force a retry, and put a different repository where this one was.
                (self.nested / "app.py").write_bytes(b"print('changed')\n")
                self.git(self.nested, "add", "app.py")
                (self.root / "work").rename(self.root / "moved")
                shutil.copytree(decoy, self.root / "work", symlinks=True)
            return result

        with patch.object(self.store, "_index", side_effect=mutate_then_swap):
            try:
                created = self.store.create(
                    ReviewRequest(
                        requirements="Review the staged change",
                        source="staged",
                        review_directory="work",
                    )
                )
            except ValueError:
                return  # refusing the replaced directory is also acceptable
        manifest = self.manifest(created["review_id"])
        self.assertEqual(manifest["git"]["base_commit"], expected)
        for row in manifest["files"]:
            for section in ("selected", "base", "staged"):
                if row[section]["exists"]:
                    self.assertNotIn(
                        "outside bytes",
                        self.store.read(created["review_id"], section, row["path"])[
                            "content"
                        ],
                    )

    def test_a_removed_capture_directory_is_unknown_not_an_exception(self):
        """Applicability must answer, even when the captured subtree is gone."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('staged')\n")
        self.git(self.nested, "add", "app.py")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the staged change",
                source="staged",
                review_directory="work",
            )
        )
        shutil.rmtree(self.nested)
        assessment = self.store.assess(created["review_id"])
        self.assertEqual(assessment["status"], "unknown")
        self.assertTrue(assessment["unknown"])
        (self.root / "work").write_bytes(b"now a file\n")
        replaced = self.store.assess(created["review_id"])
        self.assertEqual(replaced["status"], "unknown")

    def test_an_error_after_observing_a_change_keeps_the_stale_answer(self):
        """Unknown applicability must not swallow staleness that was already seen."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        created = self.store.create(
            ReviewRequest(
                requirements="Review the nested change",
                source="worktree",
                review_directory="work",
                paths=["work/app.py"],
            )
        )
        (self.nested / "app.py").write_bytes(b"print('v3')\n")

        def failing(entry, directory=""):
            raise OSError(24, "Too many open files")

        with patch.object(self.store, "_blob", side_effect=failing):
            assessment = self.store.assess(created["review_id"])
        self.assertEqual(assessment["status"], "stale")
        self.assertEqual(assessment["changed_paths"], ["work/app.py"])
        self.assertTrue(assessment["unknown"])

    def test_manifest_records_the_capture_root(self):
        """A reader must be able to tell which subtree the bytes came from."""
        self.initialize()
        (self.nested / "app.py").write_bytes(b"print('v2')\n")
        created = self.store.create(
            ReviewRequest(
                requirements="Review", source="worktree", review_directory="work"
            )
        )
        manifest = self.manifest(created["review_id"])
        self.assertEqual(manifest["git"]["review_directory"], "work")


if __name__ == "__main__":
    unittest.main()
