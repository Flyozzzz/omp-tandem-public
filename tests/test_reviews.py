"""Immutable review boundaries, version observations, and adversarial capture races."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.reviews import ReviewCheck, ReviewRequest, ReviewStore
from omp_tandem.workspace import resolve_scope


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "project"
        self.root.mkdir()
        self.scope = resolve_scope(self.home / "state", self.root)
        self.db_path = self.scope.directory / "state.sqlite3"
        self.store = ReviewStore(self.db_path, self.scope, ArtifactStore(self.db_path))

    def git(self, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout

    def initialize(self):
        self.git("init", "-q")
        self.git("config", "user.email", "review@example.invalid")
        self.git("config", "user.name", "Review Regression")
        (self.root / "tracked.txt").write_bytes(b"base\n")
        (self.root / "deleted.txt").write_bytes(b"deleted base\n")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")

    def manifest(self, review_id):
        return json.loads(self.store.read(review_id, limit=50000)["content"])

    def create(self, **kwargs):
        return self.store.create(
            ReviewRequest(requirements="Preserve the original behavior", **kwargs)
        )

    def test_capture_staged_unstaged_deleted_untracked_and_ignored(self):
        self.initialize()
        (self.root / "tracked.txt").write_bytes(b"staged\n")
        self.git("add", "tracked.txt")
        (self.root / "tracked.txt").write_bytes(b"selected\n")
        (self.root / "deleted.txt").unlink()
        (self.root / "new.bin").write_bytes(b"\x00\xff\x80")
        (self.root / "ignored.txt").write_text("not selected")
        review = self.create()
        rid = review["review_id"]
        files = {row["path"]: row for row in self.manifest(rid)["files"]}
        self.assertEqual(set(files), {"tracked.txt", "deleted.txt", "new.bin"})
        for section, expected in (
            ("base", "base\n"),
            ("staged", "staged\n"),
            ("selected", "selected\n"),
        ):
            self.assertEqual(
                self.store.read(rid, section, "tracked.txt")["content"], expected
            )
        self.assertFalse(self.store.read(rid, "selected", "deleted.txt")["exists"])
        self.assertEqual(
            self.store.read(rid, "base", "deleted.txt")["content"], "deleted base\n"
        )
        binary = self.store.read(rid, "selected", "new.bin")
        self.assertEqual(binary["encoding"], "base64")
        self.assertEqual(base64.b64decode(binary["content"]), b"\x00\xff\x80")
        self.assertEqual(binary["sha256"], hashlib.sha256(b"\x00\xff\x80").hexdigest())
        self.assertIn("+selected", self.store.read(rid, "diff")["content"])
        (self.root / "tracked.txt").write_text("later")
        self.assertEqual(
            self.store.read(rid, "selected", "tracked.txt")["content"], "selected\n"
        )
        self.assertEqual(self.store.assess(rid)["changed_paths"], ["tracked.txt"])

    def test_rename_retains_original_and_destination_bytes(self):
        self.initialize()
        self.git("mv", "tracked.txt", "renamed.txt")
        review = self.create()
        rid = review["review_id"]
        self.assertEqual(
            {row["path"]: row["change"] for row in self.manifest(rid)["files"]},
            {"tracked.txt": "deleted", "renamed.txt": "added"},
        )
        self.assertEqual(
            self.store.read(rid, "base", "tracked.txt")["content"], "base\n"
        )
        self.assertEqual(
            self.store.read(rid, "selected", "renamed.txt")["content"], "base\n"
        )

    def test_author_separation_paging_and_saved_only_reader(self):
        (self.root / "file.txt").write_text("abcdefgh")
        review = self.create(
            paths=["file.txt"],
            author_proposal="SECRET PROPOSAL",
            author_rationale="SECRET RATIONALE",
            criteria=["original criterion"],
        )
        rid = review["review_id"]
        self.assertNotIn("SECRET", json.dumps(review))
        manifest = self.manifest(rid)
        self.assertNotIn("author", json.dumps(manifest))
        with self.assertRaises(ValueError):
            self.store.read(rid, "author")
        self.assertIn(
            "SECRET RATIONALE",
            self.store.read(rid, "author", reveal_author=True)["content"],
        )
        with (
            patch.object(
                self.store, "_working", side_effect=AssertionError("live read")
            ),
            patch.object(self.store, "_git", side_effect=AssertionError("live git")),
        ):
            self.assertEqual(
                self.store.read(rid, "selected", "file.txt", offset=2, limit=3)[
                    "content"
                ],
                "cde",
            )
            self.assertEqual(
                self.store.read(rid, "criteria")["content"], '["original criterion"]'
            )
            self.assertEqual(self.store.info(rid), review)

    def test_mutation_is_retried_and_never_persists_a_mixed_bundle(self):
        (self.root / "file.txt").write_text("first")
        original = self.store._working
        calls = 0

        def mutate_once(path):
            nonlocal calls
            result = original(path)
            calls += 1
            if calls == 1:
                (self.root / "file.txt").write_text("second")
            return result

        with patch.object(self.store, "_working", side_effect=mutate_once):
            review = self.create(paths=["file.txt"])
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "file.txt")["content"],
            "second",
        )

        def mutate_always(path):
            result = original(path)
            (self.root / "file.txt").write_text(
                (self.root / "file.txt").read_text() + "!"
            )
            return result

        with (
            patch.object(self.store, "_working", side_effect=mutate_always),
            self.assertRaisesRegex(ValueError, "three capture attempts"),
        ):
            self.create(paths=["file.txt"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1
            )

    def test_boundaries_reports_and_unrelated_files_do_not_claim_immutability(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("selected")
        review = self.create(
            paths=["tracked.txt"],
            external_boundaries=["remote schema"],
            checks=[ReviewCheck(name="tests", output="passed", command="pytest")],
        )
        rid = review["review_id"]
        manifest = self.manifest(rid)
        self.assertIn("remote schema", manifest["boundaries"]["external"])
        self.assertFalse(manifest["boundaries"]["whole_environment_immutable"])
        check = json.loads(self.store.read(rid, "checks")["content"])[0]
        self.assertFalse(check["verified"])
        self.assertEqual(check["version_association"], "unknown")
        (self.root / "unrelated.txt").write_text("unrelated change")
        self.assertEqual(self.store.assess(rid)["status"], "current_selected_state")
        self.git("add", "unrelated.txt")
        self.git("commit", "-qm", "unrelated commit")
        assessment = self.store.assess(rid)
        self.assertEqual(assessment["status"], "previous_version")
        self.assertEqual(assessment["changed_paths"], [])
        self.assertGreaterEqual(assessment["assessed_at"], review["created"])
        self.assertEqual(self.store.info(rid), review)

    def test_supplied_matching_fingerprint_still_does_not_verify_test_execution(self):
        (self.root / "file.txt").write_text("same")
        first = self.create(paths=["file.txt"])
        second = self.create(
            paths=["file.txt"],
            checks=[
                ReviewCheck(
                    name="tests",
                    output="pass",
                    code_fingerprint=first["code_fingerprint"],
                )
            ],
        )
        check = json.loads(self.store.read(second["review_id"], "checks")["content"])[0]
        self.assertEqual(check["version_association"], "matches_supplied_fingerprint")
        self.assertFalse(check["verified"])

    def test_foreign_unknown_ids_and_unsafe_paths_are_rejected(self):
        (self.root / "file.txt").write_text("same")
        review = self.create(paths=["file.txt"])
        other_root = self.home / "other"
        other_root.mkdir()
        other_scope = resolve_scope(self.home / "state", other_root)
        other_db = other_scope.directory / "state.sqlite3"
        other = ReviewStore(other_db, other_scope, ArtifactStore(other_db))
        for rid in (review["review_id"], str(uuid4())):
            with self.assertRaises(ValueError):
                other.read(rid)
        for path in (
            "../file.txt",
            "/etc/passwd",
            ".git/config",
            "file.txt/../file.txt",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.create(paths=[path])
        (self.root / "escape").symlink_to(self.home, target_is_directory=True)
        (self.home / "secret").write_text("outside")
        with self.assertRaises(ValueError):
            self.create(paths=["escape/secret"])
        with self.assertRaises(ValueError):
            self.store.read(review["review_id"], "selected", "other.txt")

    def test_non_git_requires_explicit_paths_and_does_not_invent_commit(self):
        (self.root / "file.txt").write_text("same")
        with self.assertRaisesRegex(ValueError, "explicit file paths"):
            self.create()
        review = self.create(paths=["file.txt"])
        self.assertEqual(review["git"]["kind"], "files")
        self.assertIsNone(review["git"]["head"])
        self.assertIsNone(review["git"]["base_commit"])

    def test_subdirectory_scope_excludes_parent_files(self):
        self.initialize()
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("nested base")
        self.git("add", "nested")
        self.git("commit", "-qm", "nested base")
        (nested / "file.txt").write_text("nested changed")
        (self.root / "tracked.txt").write_text("outside change")
        scope = resolve_scope(self.home / "state", nested)
        db = scope.directory / "state.sqlite3"
        store = ReviewStore(db, scope, ArtifactStore(db))
        review = store.create(ReviewRequest(requirements="nested review"))
        manifest = json.loads(store.read(review["review_id"])["content"])
        self.assertEqual([row["path"] for row in manifest["files"]], ["file.txt"])
        self.assertEqual(
            store.read(review["review_id"], "base", "file.txt")["content"],
            "nested base",
        )

    def test_clean_filter_is_not_executed_by_capture(self):
        self.initialize()
        marker = self.home / "executed"
        self.git("config", "filter.surprise.clean", f"touch {marker}; cat")
        (self.root / ".gitattributes").write_text("tracked.txt filter=surprise\n")
        (self.root / "tracked.txt").write_text("changed")
        review = self.create()
        self.assertFalse(marker.exists())
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "tracked.txt")["content"],
            "changed",
        )

    def test_unsafe_live_state_is_unknown_without_rewriting_saved_bytes(self):
        (self.root / "file.txt").write_text("saved")
        review = self.create(paths=["file.txt"])
        (self.root / "file.txt").unlink()
        (self.home / "outside.txt").write_text("outside")
        (self.root / "file.txt").symlink_to(self.home / "outside.txt")
        assessment = self.store.assess(review["review_id"])
        self.assertEqual(assessment["status"], "unknown")
        self.assertIn("file.txt", assessment["unknown"][0])
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "file.txt")["content"],
            "saved",
        )

    def test_oversized_file_is_rejected_before_persistence(self):
        with (self.root / "large.bin").open("wb") as stream:
            stream.truncate(4 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            self.create(paths=["large.bin"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0
            )

    def test_no_final_newline_diff_does_not_merge_added_and_removed_lines(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("without newline")
        review = self.create(paths=["tracked.txt"])
        diff = self.store.read(review["review_id"], "diff")["content"]
        self.assertIn("-base\n+without newline\n\\ No newline at end of file\n", diff)
