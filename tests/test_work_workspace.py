"""Real Git regression boundaries for isolated shared-task snapshots."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from omp_tandem.work_workspace import WorkWorkspace
from omp_tandem.workspace import resolve_scope


class WorkWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir()
        self.scope = resolve_scope(self.home / "state", self.root)
        self.workspaces = WorkWorkspace(self.scope)
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "user.name", "Workspace Regression")
        self.git(self.root, "config", "user.email", "workspace@example.invalid")
        for name, content in {
            "alpha.txt": "alpha base\n",
            "beta.txt": "beta base\n",
            "shared.txt": "shared base\n",
            "deleted.txt": "delete me\n",
            ".gitignore": "ignored.txt\ncache/\n",
        }.items():
            (self.root / name).write_text(content)
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "source")
        self.source = self.workspaces.source_commit()
        self.counter = 0

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
            timeout=15,
        ).stdout

    def attempt(self, step="edit", *, kind="implement", submission=None):
        self.counter += 1
        result = {
            "attempt_id": f"attempt-{self.counter}",
            "step_id": step,
            "source_commit": self.source,
            "kind": kind,
            "plan_revision": 1,
        }
        if submission is not None:
            result["submission"] = submission
        return result

    @staticmethod
    def plan(*steps):
        return {
            "steps": [
                {"id": identifier, "owned_files": paths, "depends_on": depends}
                for identifier, paths, depends in steps
            ]
        }

    def publish(self, step, files, *, dependencies=(), plan=None):
        plan = plan or self.plan((step, list(files), []))
        attempt = self.attempt(step)
        workspace = self.workspaces.prepare(attempt, plan, list(dependencies))
        for name, content in files.items():
            path = Path(workspace["path"]) / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        return self.workspaces.finish(attempt, plan, workspace)

    def assert_root_unchanged(self, head, status, contents):
        self.assertEqual(
            self.git(self.root, "rev-parse", "HEAD").decode().strip(), head
        )
        self.assertEqual(
            self.git(self.root, "status", "--porcelain=v1", "--untracked-files=all"),
            status,
        )
        for name, content in contents.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_checkout_attributes_do_not_change_unowned_snapshot_bytes(self):
        (self.root / ".gitattributes").write_text(
            "alpha.txt text eol=crlf\nbeta.txt ident\n"
        )
        (self.root / "beta.txt").write_text("$Id$\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-qm", "attributes")
        self.source = self.workspaces.source_commit()
        attempt = self.attempt()
        plan = self.plan(("edit", ["shared.txt"], []))
        workspace = self.workspaces.prepare(attempt, plan, [])
        (Path(workspace["path"]) / "shared.txt").write_text("changed\n")
        output = self.workspaces.finish(attempt, plan, workspace)
        self.assertEqual(
            self.git(self.root, "show", output["commit"] + ":alpha.txt"),
            b"alpha base\n",
        )
        self.assertEqual(
            self.git(self.root, "show", output["commit"] + ":beta.txt"), b"$Id$\n"
        )
        review = self.attempt(kind="review", submission=output)
        review_workspace = self.workspaces.prepare(review, plan, [])
        self.workspaces.verify_review(review, plan, review_workspace)

    def test_parallel_modules_and_sequential_dependency_compose_without_root_edits(
        self,
    ):
        plan = self.plan(
            ("alpha", ["alpha.txt"], []),
            ("beta", ["beta.txt"], []),
            ("refine", ["alpha.txt"], ["alpha"]),
            ("integrate", ["report.txt"], ["refine", "beta"]),
        )
        (self.root / "shared.txt").write_text("user's active unsaved edit\n")
        status = self.git(
            self.root, "status", "--porcelain=v1", "--untracked-files=all"
        )
        contents = {
            name: (self.root / name).read_bytes()
            for name in ("alpha.txt", "beta.txt", "shared.txt")
        }
        alpha = self.publish(
            "alpha", {"alpha.txt": "alpha implementation\n"}, plan=plan
        )
        beta = self.publish("beta", {"beta.txt": "beta implementation\n"}, plan=plan)
        refined = self.publish(
            "refine", {"alpha.txt": "alpha refined\n"}, dependencies=[alpha], plan=plan
        )
        attempt = self.attempt("integrate")
        workspace = self.workspaces.prepare(attempt, plan, [alpha, beta, refined])
        path = Path(workspace["path"])
        self.assertEqual((path / "alpha.txt").read_text(), "alpha refined\n")
        self.assertEqual((path / "beta.txt").read_text(), "beta implementation\n")
        self.assertEqual((path / "shared.txt").read_text(), "shared base\n")
        (path / "report.txt").write_text("whole graph reviewed\n")
        final = self.workspaces.finish(attempt, plan, workspace)
        self.assertEqual(final["changed_files"], ["report.txt"])
        self.assertEqual(
            self.git(self.root, "show", f"{final['commit']}:alpha.txt"),
            b"alpha refined\n",
        )
        self.assertEqual(
            self.git(self.root, "show", f"{final['commit']}:beta.txt"),
            b"beta implementation\n",
        )
        self.assert_root_unchanged(self.source, status, contents)

    def test_checkpoint_continuation_preserves_cumulative_edits_for_dependency_consumers(
        self,
    ):
        plan = self.plan(
            ("alpha", ["alpha.txt"], []),
            ("edit", ["beta.txt", "new.txt", "deleted.txt"], ["alpha"]),
            ("integrate", ["report.txt"], ["edit"]),
        )
        alpha = self.publish("alpha", {"alpha.txt": "accepted dependency\n"}, plan=plan)
        blocked = self.publish(
            "edit",
            {"beta.txt": "saved before blocker\n", "deleted.txt": None},
            dependencies=[alpha],
            plan=plan,
        )
        attempt = self.attempt()
        attempt["checkpoint"] = dict(blocked, step_id="edit", plan_revision=1)
        workspace = self.workspaces.prepare(attempt, plan, [alpha])
        path = Path(workspace["path"])
        self.assertEqual((path / "alpha.txt").read_text(), "accepted dependency\n")
        self.assertEqual((path / "beta.txt").read_text(), "saved before blocker\n")
        self.assertFalse((path / "deleted.txt").exists())
        self.assertEqual(
            self.git(path, "write-tree").decode().strip(), blocked["tree_hash"]
        )
        (path / "new.txt").write_text("continued after blocker\n")
        submitted = self.workspaces.finish(attempt, plan, workspace)
        self.assertEqual(
            submitted["changed_files"], ["beta.txt", "deleted.txt", "new.txt"]
        )
        # A rejected submission uses the same checkpoint contract. Correcting it
        # must retain both pre-blocker work and the previous continuation.
        correction = self.attempt()
        correction["checkpoint"] = dict(submitted, step_id="edit", plan_revision=1)
        revised = self.workspaces.prepare(correction, plan, [alpha])
        revised_path = Path(revised["path"])
        self.assertEqual(
            (revised_path / "new.txt").read_text(), "continued after blocker\n"
        )
        (revised_path / "beta.txt").write_text("corrected after review\n")
        accepted = self.workspaces.finish(correction, plan, revised)
        integration = self.workspaces.prepare(
            self.attempt("integrate"), plan, [alpha, accepted]
        )
        integrated = Path(integration["path"])
        self.assertEqual(
            (integrated / "alpha.txt").read_text(), "accepted dependency\n"
        )
        self.assertEqual(
            (integrated / "beta.txt").read_text(), "corrected after review\n"
        )
        self.assertEqual(
            (integrated / "new.txt").read_text(), "continued after blocker\n"
        )
        self.assertFalse((integrated / "deleted.txt").exists())
        self.assert_root_unchanged(
            self.source,
            b"",
            {
                "alpha.txt": b"alpha base\n",
                "beta.txt": b"beta base\n",
                "deleted.txt": b"delete me\n",
            },
        )

    def test_checkpoint_rejects_stale_tampered_and_unowned_output_before_checkout(self):
        plan = self.plan(("edit", ["beta.txt"], []))
        saved = self.publish("edit", {"beta.txt": "checkpoint\n"}, plan=plan)
        checkpoint = dict(saved, step_id="edit", plan_revision=1)
        for changed, error in (
            ({"plan_revision": 2}, "current plan"),
            ({"step_id": "another"}, "this step"),
            ({"tree_hash": "0" * len(saved["tree_hash"])}, "tree hash"),
            ({"changed_files": []}, "changed-file manifest"),
        ):
            with self.subTest(changed=changed):
                attempt = self.attempt()
                attempt["checkpoint"] = dict(checkpoint, **changed)
                with self.assertRaisesRegex(ValueError, error):
                    self.workspaces.prepare(attempt, plan, [])
                self.assertFalse(
                    (
                        self.scope.directory / "worktrees" / attempt["attempt_id"]
                    ).exists()
                )
        foreign = self.publish("other", {"shared.txt": "not owned by edit\n"})
        attempt = self.attempt()
        attempt["checkpoint"] = dict(foreign, step_id="edit", plan_revision=1)
        with self.assertRaisesRegex(ValueError, "outside declared step ownership"):
            self.workspaces.prepare(attempt, plan, [])
        tree = self.git(self.root, "rev-parse", "HEAD^{tree}").decode().strip()
        unrelated = (
            self.git(
                self.root, "commit-tree", tree, "-m", "unrelated checkpoint source"
            )
            .decode()
            .strip()
        )
        attempt = self.attempt()
        attempt["source_commit"] = unrelated
        attempt["checkpoint"] = checkpoint
        with self.assertRaisesRegex(ValueError, "pinned source"):
            self.workspaces.prepare(attempt, plan, [])
        self.assert_root_unchanged(self.source, b"", {"beta.txt": b"beta base\n"})

    def test_owned_addition_deletion_binary_and_repeatable_commit(self):
        plan = self.plan(("edit", ["new.bin", "deleted.txt"], []))
        outputs = []
        for _ in range(2):
            attempt = self.attempt()
            workspace = self.workspaces.prepare(attempt, plan, [])
            path = Path(workspace["path"])
            (path / "new.bin").write_bytes(b"\x00\xff\x80exact\r\n")
            (path / "deleted.txt").unlink()
            outputs.append(self.workspaces.finish(attempt, plan, workspace))
            self.assertEqual(
                self.git(path, "rev-parse", "HEAD").decode().strip(), self.source
            )
        output = outputs[0]
        self.assertEqual(output["commit"], outputs[1]["commit"])
        self.assertEqual(output["changed_files"], ["deleted.txt", "new.bin"])
        self.assertEqual(
            self.git(self.root, "show", f"{output['commit']}:new.bin"),
            b"\x00\xff\x80exact\r\n",
        )
        self.assertNotIn(
            b"deleted.txt\0",
            self.git(self.root, "ls-tree", "--name-only", "-z", output["commit"]),
        )
        self.assertEqual(
            self.git(self.root, "rev-parse", f"{output['commit']}^{{tree}}")
            .decode()
            .strip(),
            output["tree_hash"],
        )
        (Path(workspace["path"]) / "new.bin").write_bytes(b"later replacement")
        with self.assertRaisesRegex(ValueError, "already published"):
            self.workspaces.finish(attempt, plan, workspace)

    def test_review_materializes_exact_submission_even_after_source_head_advances(self):
        output = self.publish("edit", {"alpha.txt": "submitted\n"})
        (self.root / "alpha.txt").write_text("new unrelated branch commit\n")
        self.git(self.root, "add", "alpha.txt")
        self.git(self.root, "commit", "-qm", "later")
        plan = self.plan(("edit", ["alpha.txt"], []))
        attempt = self.attempt(kind="review", submission=output)
        workspace = self.workspaces.prepare(attempt, plan, [])
        self.assertEqual(workspace["base_commit"], output["commit"])
        self.assertEqual(
            (Path(workspace["path"]) / "alpha.txt").read_text(), "submitted\n"
        )
        with self.assertRaisesRegex(ValueError, "Review attempts"):
            self.workspaces.finish(attempt, plan, workspace)
        implement = self.workspaces.prepare(self.attempt(), plan, [])
        self.assertEqual(
            (Path(implement["path"]) / "alpha.txt").read_text(), "alpha base\n"
        )
        self.workspaces.verify_review(attempt, plan, workspace)
        attempt["allow_tests"] = True
        (Path(workspace["path"]) / "ignored.txt").write_text("test artifact")
        self.workspaces.verify_review(attempt, plan, workspace)
        (Path(workspace["path"]) / "alpha.txt").write_text("reviewer changed source")
        with self.assertRaisesRegex(ValueError, "outside ownership"):
            self.workspaces.verify_review(attempt, plan, workspace)

    def test_unexpected_tracked_untracked_ignored_and_deleted_files_are_rejected(self):
        plan = self.plan(("edit", ["alpha.txt"], []))
        for name, content in [
            ("beta.txt", "modified"),
            ("new.txt", "untracked"),
            ("ignored.txt", "ignored"),
            ("deleted.txt", None),
        ]:
            with self.subTest(name=name):
                attempt = self.attempt()
                workspace = self.workspaces.prepare(attempt, plan, [])
                target = Path(workspace["path"]) / name
                target.unlink() if content is None else target.write_text(content)
                with self.assertRaisesRegex(ValueError, "outside ownership"):
                    self.workspaces.finish(attempt, plan, workspace)
                self.assertTrue(Path(workspace["path"]).is_dir())
        self.assertEqual(self.git(self.root, "status", "--porcelain=v1"), b"")

    def test_staged_unowned_bytes_cannot_hide_behind_restored_worktree(self):
        plan = self.plan(("edit", ["alpha.txt"], []))
        attempt = self.attempt()
        workspace = self.workspaces.prepare(attempt, plan, [])
        path = Path(workspace["path"])
        (path / "beta.txt").write_text("staged unauthorized change\n")
        self.git(path, "add", "beta.txt")
        (path / "beta.txt").write_text("beta base\n")
        with self.assertRaisesRegex(
            ValueError, "staged modification outside ownership"
        ):
            self.workspaces.finish(attempt, plan, workspace)

    def test_symlink_file_directory_and_nonregular_owned_file_are_refused(self):
        outside = self.home / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("untouched\n")
        for mode in ("file", "directory", "fifo"):
            with self.subTest(mode=mode):
                plan = self.plan(("edit", ["nested/sentinel.txt"], []))
                attempt = self.attempt()
                workspace = self.workspaces.prepare(attempt, plan, [])
                path = Path(workspace["path"])
                if mode == "directory":
                    (path / "nested").symlink_to(outside, target_is_directory=True)
                else:
                    (path / "nested").mkdir()
                    target = path / "nested/sentinel.txt"
                    target.symlink_to(sentinel) if mode == "file" else os.mkfifo(target)
                with self.assertRaisesRegex(ValueError, "Unsafe|nonregular"):
                    self.workspaces.finish(attempt, plan, workspace)
                self.assertEqual(sentinel.read_text(), "untouched\n")

    def test_escaping_ownership_and_scope_identity_fail_before_worktree_creation(self):
        for name in (
            "../escape",
            "/absolute",
            "a/../escape",
            ".git/config",
            "a\\escape",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.workspaces.prepare(
                    self.attempt(), self.plan(("edit", [name], [])), []
                )
        descriptor = self.scope.directory / "scope.json"
        identity = json.loads(descriptor.read_text())
        identity["project_root"] = str(self.home)
        descriptor.write_text(json.dumps(identity))
        with self.assertRaisesRegex(ValueError, "identity"):
            self.workspaces.prepare(
                self.attempt(), self.plan(("edit", ["alpha.txt"], [])), []
            )
        self.assertEqual(
            self.git(self.root, "worktree", "list", "--porcelain").count(b"worktree "),
            1,
        )

    def test_dependency_conflict_preserves_evidence_and_never_mutates_root(self):
        first = self.publish(
            "first", {"shared.txt": "first incompatible replacement\n"}
        )
        second = self.publish(
            "second", {"shared.txt": "second incompatible replacement\n"}
        )
        plan = self.plan(("integrate", ["report.txt"], []))
        attempt = self.attempt("integrate")
        with self.assertRaises(ValueError):
            self.workspaces.prepare(attempt, plan, [first, second])
        path = self.scope.directory / "worktrees" / attempt["attempt_id"]
        self.assertEqual(
            self.git(path, "show", ":2:shared.txt"), b"first incompatible replacement\n"
        )
        self.assertEqual(
            self.git(path, "show", ":3:shared.txt"),
            b"second incompatible replacement\n",
        )
        self.assert_root_unchanged(self.source, b"", {"shared.txt": b"shared base\n"})
        with self.assertRaises(ValueError):
            self.workspaces.prepare(attempt, plan, [first])

    def test_hooks_are_not_executed_and_configured_filters_are_rejected(self):
        sentinel = self.home / "hook-ran"
        for name in ("post-checkout", "pre-commit", "post-commit", "post-merge"):
            hook = self.root / ".git/hooks" / name
            hook.write_text(f"#!/bin/sh\nprintf hook > '{sentinel}'\n")
            hook.chmod(0o700)
        output = self.publish("edit", {"alpha.txt": "safe snapshot\n"})
        self.workspaces.apply(output, self.source)
        self.assertFalse(sentinel.exists())
        self.git(self.root, "config", "filter.unsafe.smudge", f"touch {sentinel}")
        with self.assertRaisesRegex(ValueError, "filters"):
            self.workspaces.prepare(
                self.attempt(), self.plan(("edit", ["alpha.txt"], [])), []
            )
        self.assertFalse(sentinel.exists())

    def test_explicit_apply_rejects_dirty_and_diverged_root_then_fast_forwards(self):
        output = self.publish("edit", {"alpha.txt": "accepted output\n"})
        (self.root / "beta.txt").write_text("user unsaved\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.workspaces.apply(output, self.source)
        self.assertEqual((self.root / "beta.txt").read_text(), "user unsaved\n")
        (self.root / "beta.txt").write_text("beta base\n")
        (self.root / "ignored.txt").write_text("user ignored artifact\n")
        applied = self.workspaces.apply(output, self.source)
        self.assertTrue(applied["applied"])
        self.assertEqual((self.root / "alpha.txt").read_text(), "accepted output\n")
        self.assertEqual(
            (self.root / "ignored.txt").read_text(), "user ignored artifact\n"
        )
        with self.assertRaisesRegex(ValueError, "HEAD changed"):
            self.workspaces.apply(output, self.source)
        (self.root / "beta.txt").write_text("new root commit\n")
        self.git(self.root, "add", "beta.txt")
        self.git(self.root, "commit", "-qm", "diverged")
        later = self.git(self.root, "rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(ValueError, "cannot fast-forward"):
            self.workspaces.apply(output, later)
        self.assert_root_unchanged(
            later,
            b"",
            {"alpha.txt": b"accepted output\n", "beta.txt": b"new root commit\n"},
        )

    def test_assess_reports_directional_relation_without_touching_checkout(self):
        output = self.publish("edit", {"alpha.txt": "accepted output\n"})
        target = output["commit"]
        behind = self.workspaces.assess(target, self.source)
        self.assertEqual(
            (behind["relation"], behind["observed_head"], behind["target_commit"]),
            ("descendant", self.source, target),
        )
        self.assertTrue(behind["expected_matches_observed"])
        self.assertEqual(
            self.git(self.root, "rev-parse", "HEAD").decode().strip(), self.source
        )
        self.workspaces.apply(output, self.source)
        equal = self.workspaces.assess(target, target)
        self.assertEqual(equal["relation"], "equal")
        (self.root / "beta.txt").write_text("later root commit\n")
        self.git(self.root, "add", "beta.txt")
        self.git(self.root, "commit", "-qm", "later")
        later = self.git(self.root, "rev-parse", "HEAD").decode().strip()
        ahead = self.workspaces.assess(target, target)
        self.assertEqual(
            (ahead["relation"], ahead["observed_head"]), ("ancestor", later)
        )
        self.assertFalse(ahead["expected_matches_observed"])
        self.git(self.root, "checkout", "-q", "-b", "side", self.source)
        (self.root / "shared.txt").write_text("side change\n")
        self.git(self.root, "add", "shared.txt")
        self.git(self.root, "commit", "-qm", "side")
        self.assertEqual(
            self.workspaces.assess(target, self.source)["relation"], "diverged"
        )
        self.assertEqual(
            self.workspaces.assess(None, self.source)["relation"], "unknown"
        )
        for key in ("actor", "method", "published", "applied"):
            self.assertNotIn(key, ahead)

    def test_apply_preserves_ignored_file_that_incoming_commit_would_replace(self):
        output = self.publish("edit", {"ignored.txt": "new tracked source\n"})
        (self.root / "ignored.txt").write_text("user ignored original\n")
        with self.assertRaisesRegex(ValueError, "overwrite an ignored"):
            self.workspaces.apply(output, self.source)
        self.assert_root_unchanged(
            self.source, b"", {"ignored.txt": b"user ignored original\n"}
        )

    def test_test_grant_excludes_ignored_artifacts_but_captures_owned_ignored_source(
        self,
    ):
        plan = self.plan(("edit", ["ignored.txt"], []))
        attempt = self.attempt()
        attempt["allow_tests"] = True
        workspace = self.workspaces.prepare(attempt, plan, [])
        path = Path(workspace["path"])
        (path / "cache").mkdir()
        (path / "cache/results.txt").write_text("unreviewed test output")
        (path / "ignored.txt").write_text("explicitly owned source\n")
        output = self.workspaces.finish(attempt, plan, workspace)
        self.assertEqual(output["changed_files"], ["ignored.txt"])
        self.assertEqual(
            self.git(self.root, "show", f"{output['commit']}:ignored.txt"),
            b"explicitly owned source\n",
        )
        self.assertNotIn(
            b"cache/",
            self.git(self.root, "ls-tree", "-r", "--name-only", output["commit"]),
        )
        self.assertEqual(
            (path / "cache/results.txt").read_text(), "unreviewed test output"
        )

    def test_manual_adoption_checks_full_commit_ownership_without_checkout_changes(
        self,
    ):
        plan = self.plan(("edit", ["alpha.txt"], []))
        attempt = self.attempt()
        attempt["autonomous"] = False
        (self.root / "alpha.txt").write_text("manual committed implementation\n")
        self.git(self.root, "add", "alpha.txt")
        self.git(self.root, "commit", "-qm", "manual")
        commit = self.git(self.root, "rev-parse", "HEAD").decode().strip()
        (self.root / "beta.txt").write_text("user still editing\n")
        status = self.git(
            self.root, "status", "--porcelain=v1", "--untracked-files=all"
        )
        output = self.workspaces.adopt_submission(attempt, plan, commit)
        self.assertEqual(output["commit"], commit)
        self.assertEqual(output["base_commit"], self.source)
        self.assertEqual(output["changed_files"], ["alpha.txt"])
        self.assert_root_unchanged(
            commit, status, {"beta.txt": b"user still editing\n"}
        )
        self.assertEqual(
            self.git(self.root, "worktree", "list", "--porcelain").count(b"worktree "),
            1,
        )
        managed = self.attempt()
        managed["autonomous"] = True
        with self.assertRaisesRegex(ValueError, "manual implementation"):
            self.workspaces.adopt_submission(managed, plan, commit)
        self.git(self.root, "add", "beta.txt")
        self.git(self.root, "commit", "-qm", "outside ownership")
        unowned = self.git(self.root, "rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(ValueError, "outside ownership"):
            self.workspaces.adopt_submission(attempt, plan, unowned)
        tree = self.git(self.root, "rev-parse", "HEAD^{tree}").decode().strip()
        unrelated = (
            self.git(self.root, "commit-tree", tree, "-m", "unrelated root")
            .decode()
            .strip()
        )
        with self.assertRaisesRegex(ValueError, "must descend"):
            self.workspaces.adopt_submission(attempt, plan, unrelated)


if __name__ == "__main__":
    unittest.main()
