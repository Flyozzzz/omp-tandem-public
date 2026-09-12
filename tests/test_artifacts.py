"""Artifact consumer regressions using disposable SQLite databases."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = self.root / "tasks.sqlite3"
        self.store = ArtifactStore(self.db_path)
        self.conversation_id = str(uuid4())
        self.task_id = str(uuid4())

    def publish(self, name, content, **kwargs):
        return self.store.publish(
            self.conversation_id, self.task_id, name, content, **kwargs
        )

    def test_versions_preserve_content_and_utf8_digest(self):
        first = self.publish("draft", "café 雪")
        second = self.store.publish(
            self.conversation_id, str(uuid4()), "draft", "revised"
        )
        independent = self.store.publish(str(uuid4()), self.task_id, "draft", "other")
        self.assertEqual(
            (first["version"], second["version"], independent["version"]), (1, 2, 1)
        )
        self.assertNotEqual(first["artifact_id"], second["artifact_id"])
        reopened = ArtifactStore(self.db_path)
        self.assertEqual(reopened.read(first["artifact_id"])["content"], "café 雪")
        self.assertEqual(reopened.read(second["artifact_id"])["content"], "revised")
        self.assertEqual(
            first["sha256"], hashlib.sha256("café 雪".encode()).hexdigest()
        )

    def test_unicode_pages_reconstruct_including_embedded_nul(self):
        content = "é雪𝄞e\u0301\x00後終"
        artifact = self.publish("unicode", content)
        pages = []
        offset = 0
        while True:
            page = self.store.read(artifact["artifact_id"], offset, 3)
            self.assertEqual(page["characters"], len(content))
            self.assertEqual(page["content"], content[offset : offset + 3])
            pages.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertEqual(page["next_offset"], offset + len(page["content"]))
            offset = page["next_offset"]
        self.assertEqual("".join(pages), content)
        exhausted = self.store.read(artifact["artifact_id"], len(content) + 10)
        self.assertEqual((exhausted["content"], exhausted["next_offset"]), ("", None))

    def test_distinct_stores_publish_concurrently_without_lost_versions(self):
        workers = 8
        stores = [ArtifactStore(self.db_path) for _ in range(workers)]
        barrier = threading.Barrier(workers)

        def publish(index):
            barrier.wait(timeout=10)
            return stores[index].publish(
                self.conversation_id, str(uuid4()), "shared", f"author {index}"
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(publish, range(workers)))
        self.assertEqual(
            sorted(result["version"] for result in results), list(range(1, workers + 1))
        )
        for index, result in enumerate(results):
            self.assertEqual(
                self.store.read(result["artifact_id"])["content"], f"author {index}"
            )
        self.assertEqual(self.publish("shared", "last")["version"], workers + 1)

    def test_invalid_json_and_unknown_artifacts(self):
        for content in ('{"broken":', '{"value": NaN}', "Infinity"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.publish("json", content, media_type="application/json")
        valid = self.publish("json", '{"value": "雪"}', media_type="application/json")
        self.assertEqual(valid["version"], 1)
        self.assertEqual(
            self.store.read(valid["artifact_id"])["content"], '{"value": "雪"}'
        )
        missing = str(uuid4())
        for lookup in (self.store.info, self.store.read):
            with self.subTest(lookup=lookup.__name__), self.assertRaises(ValueError):
                lookup(missing)
            with self.assertRaises(ValueError):
                lookup("../not-an-artifact")

    def test_validation_boundaries_and_logical_path_names(self):
        logical_path = str(self.root / "must-not-be-written")
        artifact = self.publish(logical_path, "text")
        self.assertFalse(Path(logical_path).exists())
        for offset, limit in ((-1, 1), (0, 0), (0, 50001), (1.5, 1), (0, True)):
            with (
                self.subTest(offset=offset, limit=limit),
                self.assertRaises(ValueError),
            ):
                self.store.read(artifact["artifact_id"], offset, limit)
        for name in ("", " \t", "x" * 121):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.publish(name, "text")
        with self.assertRaises(ValueError):
            self.publish("media", "text", media_type="text/html")
        with self.assertRaises(ValueError):
            self.store.publish("invalid", self.task_id, "name", "text")
        with self.assertRaises(ValueError):
            self.store.publish(self.conversation_id, "invalid", "name", "text")
        boundary = "雪" * ((4 * 1024 * 1024) // 3) + "x"
        accepted = self.publish("x" * 120, boundary)
        self.assertEqual(
            self.store.read(accepted["artifact_id"], offset=len(boundary) - 1)[
                "content"
            ],
            "x",
        )
        with self.assertRaises(ValueError):
            self.publish("too-large", boundary + "x")

    def test_context_evidence_uses_real_owner_and_caller_transaction(self):
        context_id = str(uuid4())
        artifact_id = str(uuid4())
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            evidence = self.store.publish_in_transaction(
                db,
                "context evidence",
                "bounded evidence",
                context_id=context_id,
                artifact_id=artifact_id,
            )
            with self.assertRaises(ValueError):
                self.store.read(artifact_id)
        self.assertEqual(self.store.read(artifact_id)["content"], "bounded evidence")
        self.assertEqual(evidence["context_id"], context_id)
        self.assertIsNone(evidence["task_id"])
        self.assertIsNone(evidence["conversation_id"])
        self.assertEqual(self.store.for_task(self.task_id), [])
        rolled_back = str(uuid4())
        with self.assertRaises(RuntimeError), closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.store.publish_in_transaction(
                db,
                "context evidence",
                "discard",
                context_id=context_id,
                artifact_id=rolled_back,
            )
            raise RuntimeError("Abort the enclosing import")
        with self.assertRaises(ValueError):
            self.store.read(rolled_back)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            next_version = self.store.publish_in_transaction(
                db,
                "context evidence",
                "retained",
                context_id=context_id,
            )
        self.assertEqual(next_version["version"], 2)
        with closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ValueError):
                self.store.publish_in_transaction(
                    db,
                    "mixed owners",
                    "invalid",
                    context_id=context_id,
                    task_id=self.task_id,
                    conversation_id=self.conversation_id,
                )
            with self.assertRaises(ValueError):
                self.store.publish_in_transaction(
                    db,
                    "unsupported",
                    "invalid",
                    context_id=context_id,
                    media_type="text/html",
                )

    def test_legacy_upgrade_preserves_content_versions_and_custom_constraints(self):
        path = self.root / "legacy.sqlite3"
        old_id = str(uuid4())
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE artifacts (
                artifact_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                task_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
                sha256 TEXT NOT NULL, media_type TEXT NOT NULL, characters INTEGER NOT NULL,
                created REAL NOT NULL, content TEXT NOT NULL,
                UNIQUE (conversation_id, name, version)
            )""")
            db.execute(
                "CREATE UNIQUE INDEX unique_evidence_digest ON artifacts(sha256)"
            )
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    old_id,
                    self.conversation_id,
                    self.task_id,
                    "old",
                    1,
                    hashlib.sha256(b"preserved").hexdigest(),
                    "text/plain",
                    9,
                    1.0,
                    "preserved",
                ),
            )
        upgraded = ArtifactStore(path)
        self.assertEqual(upgraded.read(old_id)["content"], "preserved")
        self.assertIsNone(upgraded.info(old_id)["context_id"])
        self.assertEqual(
            upgraded.publish(self.conversation_id, self.task_id, "old", "next")[
                "version"
            ],
            2,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            upgraded.publish(str(uuid4()), str(uuid4()), "duplicate", "preserved")
        context_id = str(uuid4())
        with closing(upgraded._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            context_evidence = upgraded.publish_in_transaction(
                db, "context-owned", "new owner", context_id=context_id
            )
        reopened = ArtifactStore(path)
        self.assertEqual(
            reopened.read(context_evidence["artifact_id"])["context_id"], context_id
        )
        self.assertEqual(reopened.read(old_id)["content"], "preserved")


if __name__ == "__main__":
    unittest.main()


class ArtifactPagingTests(unittest.TestCase):
    def test_read_artifact_text_reassembles_records_longer_than_one_page(self):
        from omp_tandem.task_results import read_artifact_text

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ArtifactStore(Path(directory.name) / "tasks.sqlite3")
        content = "{" + '"note": "' + "雪" * 60000 + '"}'
        published = store.publish(str(uuid4()), str(uuid4()), "big", content)
        self.assertGreater(published["characters"], 50000)
        self.assertEqual(read_artifact_text(store, published["artifact_id"]), content)
