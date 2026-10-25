"""Context hints must retain source identity without widening review authority."""

import hashlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.reviews import ReviewRequest, ReviewStore
from omp_tandem.workspace import resolve_scope


class ReviewContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.root = home / "project"
        self.root.mkdir()
        self.scope = resolve_scope(home / "state", self.root)
        self.database = self.scope.directory / "reviews.sqlite3"
        self.store = ReviewStore(
            self.database, self.scope, ArtifactStore(self.database)
        )
        self.git("init", "-q")
        self.git("config", "user.email", "context@example.invalid")
        self.git("config", "user.name", "Context Regression")

    def git(self, *args):
        return (
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=self.root,
                check=True,
                capture_output=True,
                timeout=15,
            )
            .stdout.decode()
            .strip()
        )

    def put(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def prepare(self, **kwargs):
        return self.store.prepare(
            ReviewRequest(requirements="Preserve behavior", **kwargs)
        )

    def test_staged_importers_and_test_hints_ignore_live_bytes(self):
        self.put("main.py", "value = 1\n")
        self.put("dep.py", "value = 1\n")
        self.put("caller.py", "import main\n")
        self.put("test_main.py", "import main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.put("main.py", "import dep\nvalue = 2\n")
        self.git("add", "main.py")
        self.put("main.py", "import foreign_live\n")
        self.put("dep.py", "SECRET_LIVE_BYTES\n")
        request = ReviewRequest(
            requirements="Preserve behavior", source="staged", paths=["main.py"]
        )
        before = request.model_dump()
        result = self.store.prepare(request)
        candidates = {item["path"]: item for item in result["context_candidates"]}
        self.assertEqual(set(candidates), {"dep.py", "caller.py", "test_main.py"})
        self.assertEqual(
            candidates["dep.py"]["source_identity"]["sha256"],
            hashlib.sha256(b"value = 1\n").hexdigest(),
        )
        self.assertIn(
            {"kind": "reverse_importer", "related_path": "main.py"},
            candidates["caller.py"]["reasons"],
        )
        self.assertEqual(request.model_dump(), before)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM reviews").fetchone()[0], 0
            )

    def test_commit_typescript_resolution_uses_exact_tree(self):
        self.put("src/main.ts", "export const value = 1;\n")
        self.put("src/dep.ts", "export const value = 2;\n")
        self.put("tests/main.spec.ts", "import { value } from '../src/main.js';\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        self.put(
            "src/main.ts", "import { value } from './dep.js';\nexport { value };\n"
        )
        self.git("add", ".")
        self.git("commit", "-qm", "target")
        commit = self.git("rev-parse", "HEAD")
        self.put("src/dep.ts", "export const live = 'not reviewed';\n")
        result = self.prepare(
            source="commit", base=base, commit=commit, paths=["src/main.ts"]
        )
        candidates = {item["path"]: item for item in result["context_candidates"]}
        self.assertEqual(set(candidates), {"src/dep.ts", "tests/main.spec.ts"})
        for item in candidates.values():
            self.assertEqual(item["source_identity"]["commit"], commit)
        self.assertEqual(
            candidates["src/dep.ts"]["source_identity"]["sha256"],
            hashlib.sha256(b"export const value = 2;\n").hexdigest(),
        )

    def test_symlink_and_missing_import_are_not_followed_or_silently_complete(self):
        self.put("main.py", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        outside = self.root.parent / "secret.py"
        outside.write_text("TOP_SECRET = True\n")
        (self.root / "unsafe.py").symlink_to(outside)
        self.put("main.py", "import unsafe\nimport missing\n")
        result = self.prepare(paths=["main.py"])
        self.assertNotIn(
            "unsafe.py", {item["path"] for item in result["context_candidates"]}
        )
        self.assertFalse(result["context_scan"]["complete"])
        unresolved = result["context_scan"]["unresolved"]
        self.assertTrue(any(item["path"] == "unsafe.py" for item in unresolved))
        self.assertTrue(any(item["relationship"] == "missing" for item in unresolved))
        self.assertNotIn("TOP_SECRET", str(result))

    def test_candidate_budget_and_unknown_language_are_explicit(self):
        self.put("main.py", "value = 1\n")
        self.put("other.rs", "fn main() {}\n")
        for index in range(25):
            self.put(f"caller{index:02}.py", "import main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.put("main.py", "value = 2\n")
        self.put("other.rs", 'fn main() { println!("changed"); }\n')
        result = self.prepare(paths=["main.py", "other.rs"])
        self.assertEqual(len(result["context_candidates"]), 20)
        self.assertEqual(result["context_scan"]["candidates_omitted"], 5)
        self.assertIn(
            {
                "path": "other.rs",
                "relationship": "language",
                "reason": "unsupported_language",
            },
            result["context_scan"]["unresolved"],
        )

    def test_review_directory_excludes_outer_callers_and_source_bytes(self):
        self.put("nested/main.py", "value = 1\n")
        self.put("nested/dep.py", "value = 2\n")
        self.put("outside.py", "import nested.main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.put("nested/main.py", "import dep\n")
        self.git("add", "nested/main.py")
        result = self.prepare(
            source="staged", review_directory="nested", paths=["nested/main.py"]
        )
        self.assertEqual(
            [item["path"] for item in result["context_candidates"]], ["nested/dep.py"]
        )
        self.assertEqual(
            result["context_candidates"][0]["source_identity"]["review_directory"],
            "nested",
        )

    def test_scan_limit_reports_unexamined_relationships(self):
        self.put("main.py", "value = 1\n")
        for index in range(130):
            self.put(f"module{index:03}.py", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.put("main.py", "import module129\n")
        result = self.prepare(paths=["main.py"])
        scan = result["context_scan"]
        self.assertEqual(scan["scanned_files"], 128)
        self.assertFalse(scan["complete"])
        self.assertIn(
            {
                "path": "main.py",
                "relationship": "module129.py",
                "reason": "resolved_path_not_scanned",
            },
            scan["unresolved"],
        )

    def test_non_utf8_untracked_inventory_does_not_prevent_preparation(self):
        self.put("main.py", "value = 1\n")
        self.git("add", "main.py")
        self.git("commit", "-qm", "base")
        self.put("main.py", "value = 2\n")
        original = self.store._git

        def inventory(*arguments, **kwargs):
            if arguments[:1] == ("ls-files",) and "--others" in arguments:
                return b"\xff.py\0"
            return original(*arguments, **kwargs)

        # Some filesystems reject non-UTF-8 names; exercise the Git protocol
        # boundary portably while all selected-source reads still use real Git.
        with patch.object(self.store, "_git", side_effect=inventory):
            result = self.prepare(paths=["main.py"])
        self.assertEqual(result["code"], "selection_ready")
        self.assertEqual(result["context_scan"]["undecodable_inventory_paths"], 1)
        self.assertFalse(result["context_scan"]["complete"])
