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

    def test_staged_partial_content_diff_and_fingerprint_ignore_unstaged_changes(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt")
        (self.root / "tracked.txt").write_text("unstaged\n")
        (self.root / "deleted.txt").unlink()
        (self.root / "untracked.txt").write_text("untracked")
        with patch.object(
            self.store, "_working", side_effect=AssertionError("live read")
        ):
            review = self.create(source="staged")
            rid = review["review_id"]
            manifest = self.manifest(rid)
            self.assertEqual(
                [row["path"] for row in manifest["files"]], ["tracked.txt"]
            )
            self.assertEqual(manifest["source"], "staged")
            self.assertEqual(manifest["files"][0]["change"], "modified")
            saved = self.store.read(rid, "selected", "tracked.txt")
            self.assertEqual(saved["content"], "staged\n")
            self.assertEqual(saved["source"], "staged")
            self.assertIn("-base\n+staged\n", self.store.read(rid, "diff")["content"])
            self.assertNotIn("unstaged", self.store.read(rid, "diff")["content"])
            (self.root / "tracked.txt").unlink()
            (self.root / "tracked.txt").symlink_to(self.home / "missing-outside-file")
            (self.root / "another-untracked.txt").write_text("later")
            second = self.create(source="staged")
            self.assertEqual(second["code_fingerprint"], review["code_fingerprint"])
            self.assertEqual(self.store.assess(rid)["status"], "current_selected_state")
            explicit = self.create(source="staged", paths=["tracked.txt"])
            self.assertEqual(explicit["code_fingerprint"], review["code_fingerprint"])

    def test_staged_deletion_and_additions_do_not_consult_conflicting_live_paths(self):
        self.initialize()
        self.git("rm", "deleted.txt")
        (self.root / "deleted.txt").write_text("recreated, not staged")
        (self.root / "missing.txt").write_text("saved addition")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "file.txt").write_text("saved nested addition")
        self.git("add", "missing.txt", "nested/file.txt")
        (self.root / "missing.txt").unlink()
        (nested / "file.txt").unlink()
        nested.rmdir()
        nested.symlink_to(self.home, target_is_directory=True)
        with patch.object(
            self.store, "_working", side_effect=AssertionError("live read")
        ):
            review = self.create(source="staged")
            rid = review["review_id"]
            self.assertEqual(
                {row["path"]: row["change"] for row in self.manifest(rid)["files"]},
                {
                    "deleted.txt": "deleted",
                    "missing.txt": "added",
                    "nested/file.txt": "added",
                },
            )
            deleted = self.store.read(rid, "selected", "deleted.txt")
            self.assertFalse(deleted["exists"])
            self.assertEqual(deleted["source"], "staged")
            self.assertEqual(
                self.store.read(rid, "selected", "missing.txt")["content"],
                "saved addition",
            )
            self.assertEqual(
                self.store.read(rid, "selected", "nested/file.txt")["content"],
                "saved nested addition",
            )
            self.assertEqual(self.store.assess(rid)["status"], "current_selected_state")

    def test_staged_assessment_tracks_selected_index_not_new_unselected_paths(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("staged")
        self.git("add", "tracked.txt")
        review = self.create(source="staged")
        rid = review["review_id"]
        (self.root / "later.txt").write_text("not reviewed")
        self.git("add", "later.txt")
        assessment = self.store.assess(rid)
        self.assertEqual(assessment["status"], "current_selected_state")
        self.assertEqual(assessment["changed_paths"], [])
        with self.assertRaisesRegex(ValueError, "not part of this review"):
            self.store.read(rid, "selected", "later.txt")
        (self.root / "tracked.txt").write_text("new staged")
        self.git("add", "tracked.txt")
        with patch.object(
            self.store, "_working", side_effect=AssertionError("live read")
        ):
            assessment = self.store.assess(rid)
        self.assertEqual(assessment["source"], "staged")
        self.assertEqual(assessment["status"], "stale")
        self.assertEqual(assessment["changed_paths"], ["tracked.txt"])
        self.assertEqual(assessment["changed_index_paths"], ["tracked.txt"])
        self.assertEqual(
            self.store.read(rid, "selected", "tracked.txt")["content"], "staged"
        )

    def test_staged_index_race_retries_coherently_and_rejects_continuous_mutation(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("first")
        self.git("add", "tracked.txt")
        original = self.store._index
        calls = 0

        def mutate_once():
            nonlocal calls
            result = original()
            calls += 1
            if calls == 1:
                (self.root / "tracked.txt").write_text("second")
                (self.root / "arrived.txt").write_text("arrived during capture")
                self.git("add", "tracked.txt", "arrived.txt")
            return result

        with (
            patch.object(self.store, "_index", side_effect=mutate_once),
            patch.object(
                self.store, "_working", side_effect=AssertionError("live read")
            ),
        ):
            review = self.create(source="staged")
        rid = review["review_id"]
        self.assertEqual(
            self.store.read(rid, "selected", "tracked.txt")["content"], "second"
        )
        self.assertEqual(
            self.store.read(rid, "selected", "arrived.txt")["content"],
            "arrived during capture",
        )
        self.assertIn("+second", self.store.read(rid, "diff")["content"])
        self.assertNotIn("+first", self.store.read(rid, "diff")["content"])

        def mutate_always():
            nonlocal calls
            result = original()
            calls += 1
            (self.root / "tracked.txt").write_text(f"changing {calls}")
            self.git("add", "tracked.txt")
            return result

        with (
            patch.object(self.store, "_index", side_effect=mutate_always),
            patch.object(
                self.store, "_working", side_effect=AssertionError("live read")
            ),
            self.assertRaisesRegex(ValueError, "three capture attempts"),
        ):
            self.create(source="staged", paths=["tracked.txt"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 1
            )

    def test_staged_modes_binary_and_rename_are_saved_from_index(self):
        self.initialize()
        self.git("mv", "tracked.txt", "renamed.txt")
        self.git("update-index", "--chmod=+x", "renamed.txt")
        self.git("update-index", "--chmod=+x", "deleted.txt")
        binary = b"\x00\xff\x80"
        (self.root / "binary.dat").write_bytes(binary)
        self.git("add", "binary.dat")
        (self.root / "binary.dat").write_text("live text")
        (self.root / "renamed.txt").unlink()
        review = self.create(source="staged")
        rid = review["review_id"]
        files = {row["path"]: row for row in self.manifest(rid)["files"]}
        self.assertEqual(
            {path: row["change"] for path, row in files.items()},
            {
                "tracked.txt": "deleted",
                "renamed.txt": "added",
                "binary.dat": "added",
                "deleted.txt": "modified",
            },
        )
        self.assertEqual(files["renamed.txt"]["selected"]["mode"], "100755")
        self.assertEqual(files["deleted.txt"]["selected"]["mode"], "100755")
        self.assertEqual(
            self.store.read(rid, "selected", "renamed.txt")["content"], "base\n"
        )
        self.assertEqual(
            base64.b64decode(self.store.read(rid, "selected", "binary.dat")["content"]),
            binary,
        )
        diff = self.store.read(rid, "diff")["content"]
        self.assertIn("Modes: absent -> 100755", diff)
        self.assertIn("Modes: 100644 -> 100755", diff)
        self.assertIn(hashlib.sha256(binary).hexdigest(), diff)
        self.assertNotIn("live text", diff)

    def test_staged_selection_uses_requested_base_and_distinguishes_source_hash(self):
        self.initialize()
        base = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "tracked.txt").write_text("committed later\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "later")
        self.assertEqual(self.create(source="staged")["file_count"], 0)
        review = self.create(source="staged", base=base)
        self.assertEqual(
            self.store.read(review["review_id"], "base", "tracked.txt")["content"],
            "base\n",
        )
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "tracked.txt")["content"],
            "committed later\n",
        )
        worktree = self.create(paths=["tracked.txt"], base=base)
        self.assertNotEqual(review["code_fingerprint"], worktree["code_fingerprint"])

    def test_staged_source_validation_empty_selection_and_unborn_repository(self):
        (self.root / "only-live.txt").write_text("not staged")
        with self.assertRaisesRegex(
            ValueError, "Staged reviews require a Git repository"
        ):
            self.create(source="staged", paths=["only-live.txt"])
        with self.assertRaisesRegex(
            ValueError, "Staged reviews require a Git repository"
        ):
            self.create(source="staged")
        with self.assertRaises(ValueError):
            self.create(source="index", paths=["only-live.txt"])
        self.git("init", "-q")
        empty = self.create(source="staged")
        self.assertEqual(empty["file_count"], 0)
        self.assertIsNone(empty["git"]["head"])
        self.assertIsNone(empty["git"]["base_commit"])
        self.assertEqual(self.store.read(empty["review_id"], "diff")["content"], "")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.create(source="staged", paths=["only-live.txt"])
        self.git("add", "only-live.txt")
        added = self.create(source="staged")
        self.assertEqual(
            self.manifest(added["review_id"])["files"][0]["change"], "added"
        )
        self.assertEqual(
            self.store.read(added["review_id"], "selected", "only-live.txt")["content"],
            "not staged",
        )
        self.assertEqual(
            self.store.assess(empty["review_id"])["status"], "current_selected_state"
        )
        with self.assertRaises(ValueError):
            self.store.read(empty["review_id"], "selected", "only-live.txt")

    def test_staged_unmerged_entries_and_submodules_are_explicitly_rejected(self):
        self.initialize()
        head = self.git("rev-parse", "HEAD").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},module")
        with self.assertRaisesRegex(ValueError, "submodules"):
            self.create(source="staged")
        # Unselected unsupported material does not broaden an explicit review.
        review = self.create(source="staged", paths=["tracked.txt"])
        self.assertEqual(review["file_count"], 1)
        self.git("update-index", "--force-remove", "module")
        oid = self.git("rev-parse", "HEAD:tracked.txt").decode().strip()
        subprocess.run(
            ["git", "update-index", "--index-info"],
            cwd=self.root,
            input=f"0 {'0' * 40}\ttracked.txt\n100644 {oid} 1\ttracked.txt\n".encode(),
            capture_output=True,
            check=True,
            timeout=15,
        )
        with self.assertRaisesRegex(ValueError, "Unmerged index entry"):
            self.create(source="staged")
        self.assertEqual(self.store.assess(review["review_id"])["status"], "stale")

    def test_legacy_manifest_without_source_preserves_stored_bytes_and_hash(self):
        (self.root / "file.txt").write_text("saved")
        review = self.create(paths=["file.txt"])
        rid = review["review_id"]
        manifest = self.manifest(rid)
        del manifest["source"]
        manifest["code_fingerprint"] = hashlib.sha256(
            json.dumps(
                {"files": manifest["files"], "git": manifest["git"]},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        legacy = json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE reviews SET manifest=? WHERE review_id=?", (legacy, rid))
        saved = self.store.read(rid)
        expected_hash = hashlib.sha256(legacy.encode()).hexdigest()
        self.assertEqual(saved["sha256"], expected_hash)
        self.assertEqual(saved["content"], legacy)
        self.assertEqual(saved["source"], "worktree")
        self.assertEqual(self.store.info(rid)["source"], "worktree")
        self.assertEqual(
            self.store.info(rid)["code_fingerprint"], manifest["code_fingerprint"]
        )
        (self.root / "file.txt").write_text("changed")
        assessment = self.store.assess(rid)
        self.assertEqual(assessment["source"], "worktree")
        self.assertEqual(assessment["status"], "stale")
        self.assertEqual(self.store.read(rid)["sha256"], expected_hash)
        self.assertEqual(self.store.read(rid)["content"], legacy)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT manifest FROM reviews WHERE review_id=?", (rid,)
                ).fetchone()[0],
                legacy,
            )

    def test_explicit_unchanged_context_is_saved_and_affects_applicability(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("changed")
        review = self.create(paths=["tracked.txt"], context_paths=["deleted.txt"])
        manifest = self.manifest(review["review_id"])
        self.assertEqual(
            {row["path"]: row["role"] for row in manifest["files"]},
            {"tracked.txt": "change", "deleted.txt": "context"},
        )
        self.assertEqual((review["change_count"], review["context_count"]), (1, 1))
        self.assertNotIn(
            "deleted.txt", self.store.read(review["review_id"], "diff")["content"]
        )
        (self.root / "deleted.txt").write_text("context changed later")
        self.assertEqual(
            self.store.assess(review["review_id"])["changed_paths"], ["deleted.txt"]
        )
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "deleted.txt")["content"],
            "deleted base\n",
        )

    def test_staged_context_uses_index_even_when_live_context_is_unsafe(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("staged")
        self.git("add", "tracked.txt")
        (self.root / "deleted.txt").unlink()
        (self.root / "deleted.txt").symlink_to(self.home / "outside")
        with patch.object(
            self.store, "_working", side_effect=AssertionError("live context read")
        ):
            review = self.create(source="staged", context_paths=["deleted.txt"])
            self.assertEqual(
                self.store.read(review["review_id"], "selected", "deleted.txt")[
                    "content"
                ],
                "deleted base\n",
            )
            self.assertEqual(
                self.store.assess(review["review_id"])["status"],
                "current_selected_state",
            )

    def test_context_only_clean_selection_is_not_a_change(self):
        self.initialize()
        review = self.create(source="staged", context_paths=["tracked.txt"])
        self.assertEqual((review["change_count"], review["context_count"]), (0, 1))
        self.assertEqual(self.store.read(review["review_id"], "diff")["content"], "")

    def test_missing_changed_and_unsafe_context_are_not_hidden_live_reads(self):
        self.initialize()
        (self.root / "tracked.txt").write_text("selected")
        (self.root / "deleted.txt").write_text("also changed")
        with self.assertRaisesRegex(ValueError, "Context path has changes"):
            self.create(paths=["tracked.txt"], context_paths=["deleted.txt"])
        with self.assertRaisesRegex(ValueError, "Required context is missing"):
            self.create(paths=["tracked.txt"], context_paths=["missing.txt"])
        with self.assertRaises(ValueError):
            self.create(paths=["tracked.txt"], context_paths=["../outside"])
        included = self.create(
            paths=["tracked.txt", "deleted.txt"], context_paths=["deleted.txt"]
        )
        self.assertEqual(included["change_count"], 2)

    def test_non_git_context_is_explicit_saved_material_not_a_second_change(self):
        (self.root / "file.txt").write_text("selected code")
        (self.root / "context.txt").write_text("explicit context")
        with self.assertRaisesRegex(ValueError, "explicit file paths"):
            self.create(context_paths=["context.txt"])
        review = self.create(paths=["file.txt"], context_paths=["context.txt"])
        self.assertEqual((review["change_count"], review["context_count"]), (1, 1))
        self.assertNotIn(
            "explicit context", self.store.read(review["review_id"], "diff")["content"]
        )
        self.assertEqual(
            self.store.read(review["review_id"], "selected", "context.txt")["content"],
            "explicit context",
        )


class CommitSourceReviewTests(ReviewTests):
    def test_commit_source_captures_committed_bytes_only(self):
        self.initialize()
        (self.root / "module.py").write_text("before\n")
        self.git("add", "module.py")
        self.git("commit", "-q", "-m", "base")
        base = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "module.py").write_text("after\n")
        (self.root / "extra.py").write_text("new\n")
        self.git("add", "module.py", "extra.py")
        self.git("commit", "-q", "-m", "change")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        # Live edits and index changes must not leak into a commit bundle.
        (self.root / "module.py").write_text("dirty working tree\n")
        (self.root / "extra.py").write_text("dirty\n")
        self.git("add", "extra.py")
        summary = self.store.create(
            ReviewRequest(
                requirements="Review the committed change",
                source="commit",
                commit=commit,
                base=base,
                author_proposal="author-sentinel-7731",
            )
        )
        self.assertEqual(summary["source"], "commit")
        self.assertEqual(summary["change_count"], 2)
        selected = self.store.read(
            summary["review_id"], section="selected", path="module.py"
        )
        self.assertEqual(selected["content"], "after\n")
        self.assertEqual(
            self.store.read(summary["review_id"], section="base", path="module.py")[
                "content"
            ],
            "before\n",
        )
        self.assertFalse(
            self.store.read(summary["review_id"], section="staged", path="extra.py")[
                "exists"
            ]
        )
        with self.assertRaises(ValueError):
            self.store.read(summary["review_id"], section="author")
        author = self.store.read(
            summary["review_id"], section="author", reveal_author=True
        )
        self.assertIn("author-sentinel-7731", json.dumps(author))
        manifest = self.store.read(summary["review_id"], section="manifest")
        self.assertEqual(json.loads(manifest["content"])["git"]["commit"], commit)
        assessment = self.store.assess(summary["review_id"])
        self.assertEqual(assessment["changed_paths"], [])
        self.assertIn("committed bytes", assessment["scope"])
        with self.assertRaises(ValueError):
            ReviewRequest(requirements="x", source="commit")
        with self.assertRaises(ValueError):
            self.store.create(
                ReviewRequest(
                    requirements="x", source="commit", commit="0" * 40, base=base
                )
            )
