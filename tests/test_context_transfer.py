"""Recipient-bound sharing regressions using disposable project namespaces."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.context_transfer import ContextTransfer
from omp_tandem.project_context import ContextConflict, ProjectContextStore


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ContextTransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = self.root / "state"
        self.source = self.project("source")
        self.target = self.project("target")
        self.other = self.project("other")
        self.evidence = self.source.artifacts.publish(
            str(uuid4()), str(uuid4()), "design evidence", "café 雪\u0000proof"
        )
        self.decision_evidence = self.source.artifacts.publish(
            str(uuid4()),
            str(uuid4()),
            "decision evidence",
            '{"reason":"reversible"}',
            "application/json",
        )
        self.unshared = self.source.artifacts.publish(
            str(uuid4()), str(uuid4()), "private", "DO NOT SHARE"
        )
        self.context = {
            "project_id": "product",
            "product_summary": "Keep product requirements, not task authority.",
            "rules": [{"id": "rule", "text": "Preserve user data", "source": "owner"}],
            "decisions": [
                {
                    "id": "decision",
                    "text": "Use reversible migration",
                    "status": "accepted",
                    "source": "design discussion",
                    "evidence_artifact_ids": [
                        self.decision_evidence["artifact_id"],
                        self.evidence["artifact_id"],
                    ],
                }
            ],
            "artifact_ids": [self.evidence["artifact_id"]],
        }
        self.snapshot = self.source.projects.publish(self.context)

    def project(self, name):
        root = self.root / name
        root.mkdir()
        root = root.resolve()
        key = hashlib.sha256(str(root).encode()).hexdigest()
        directory = self.base / "projects" / key
        directory.mkdir(parents=True)
        scope = SimpleNamespace(root=root, base=self.base, key=key, directory=directory)
        projects = ProjectContextStore(directory / "tasks.sqlite3")
        artifacts = ArtifactStore(projects.db_path)
        return SimpleNamespace(
            scope=scope,
            projects=projects,
            artifacts=artifacts,
            transfer=ContextTransfer(scope, projects, artifacts),
        )

    def export(self):
        return self.source.transfer.export(
            self.snapshot["context_id"], self.target.scope.root
        )

    def bundle_path(self, token):
        return self.base / "transfers" / (token + ".json")

    def change_bundle(self, token, mutate, *, update_checksum=True):
        path = self.bundle_path(token)
        bundle = json.loads(path.read_bytes())
        mutate(bundle["payload"])
        if update_checksum:
            bundle["sha256"] = hashlib.sha256(canonical(bundle["payload"])).hexdigest()
        path.write_bytes(canonical(bundle))

    def assert_empty_target(self):
        self.assertEqual(self.target.projects.list(), [])
        with closing(self.target.artifacts._connect()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM context_imports").fetchone()[0], 0
            )

    def test_explicit_import_remaps_all_evidence_without_changing_source(self):
        before = self.source.projects.get(self.snapshot["context_id"])
        transfer = self.export()
        for artifact_id in (
            self.evidence["artifact_id"],
            self.decision_evidence["artifact_id"],
            self.unshared["artifact_id"],
        ):
            with self.assertRaises(ValueError):
                self.target.artifacts.read(artifact_id)
        with self.assertRaises(ValueError):
            self.target.projects.get(self.snapshot["context_id"])
        self.assert_empty_target()
        imported = self.target.transfer.import_context(transfer["transfer_id"])
        self.assertNotEqual(imported["context_id"], self.snapshot["context_id"])
        context = imported["context"]
        new_id = context["artifact_ids"][0]
        decision_ids = context["decisions"][0]["evidence_artifact_ids"]
        self.assertEqual(decision_ids[1], new_id)
        self.assertNotIn(self.evidence["artifact_id"], decision_ids)
        self.assertNotIn(self.decision_evidence["artifact_id"], decision_ids)
        evidence = self.target.artifacts.read(new_id)
        self.assertEqual(evidence["content"], "café 雪\u0000proof")
        self.assertEqual(evidence["context_id"], imported["context_id"])
        self.assertIsNone(evidence["conversation_id"])
        self.assertIsNone(evidence["task_id"])
        self.assertEqual(
            self.target.artifacts.read(decision_ids[0])["content"],
            '{"reason":"reversible"}',
        )
        self.assertEqual(context["rules"], before["context"]["rules"])
        self.assertEqual(context["decisions"][0]["status"], "accepted")
        self.assertEqual(context["decisions"][0]["source"], "design discussion")
        self.assertIn("not approval", imported["publisher"])
        self.assertIn(transfer["transfer_id"], imported["publisher"])
        self.assertEqual(self.source.projects.get(self.snapshot["context_id"]), before)
        self.assertEqual(
            self.source.artifacts.info(self.evidence["artifact_id"]), self.evidence
        )
        self.assertEqual(len(self.source.projects.list()), 1)
        with self.assertRaises(ValueError):
            self.source.artifacts.read(new_id)
        for artifact_id in (self.evidence["artifact_id"], self.unshared["artifact_id"]):
            with self.assertRaises(ValueError):
                self.target.artifacts.read(artifact_id)
        self.assertNotIn(
            b"DO NOT SHARE", self.bundle_path(transfer["transfer_id"]).read_bytes()
        )

    def test_invalid_and_foreign_capabilities_do_not_disclose_sender(self):
        token = self.export()["transfer_id"]
        messages = []
        for recipient, value in (
            (self.other, token),
            (self.source, token),
            (self.target, "../" + token),
            (self.target, token.upper()),
            (self.target, "0" * 64),
            (self.target, ""),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                recipient.transfer.import_context(value)
            messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1)
        self.assertNotIn(self.snapshot["context_id"], messages[0])
        self.assert_empty_target()

    def test_export_requires_a_distinct_existing_recipient_and_local_snapshot(self):
        for root in (
            self.source.scope.root,
            self.root / "missing",
            self.source.projects.db_path,
        ):
            with self.subTest(root=root), self.assertRaises((ValueError, OSError)):
                self.source.transfer.export(self.snapshot["context_id"], root)
        with self.assertRaises(ValueError):
            self.target.transfer.export(
                self.snapshot["context_id"], self.other.scope.root
            )

    def test_revision_conflict_is_atomic_and_retry_imports_once(self):
        prior = self.target.projects.publish(
            self.context | {"artifact_ids": [], "decisions": []}
        )
        token = self.export()["transfer_id"]
        with self.assertRaises(ContextConflict):
            self.target.transfer.import_context(token, expected_revision=0)
        self.assertEqual(self.target.projects.list(), [prior])
        with closing(self.target.artifacts._connect()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM context_imports").fetchone()[0], 0
            )
        imported = self.target.transfer.import_context(token, expected_revision=1)
        self.assertEqual(imported["revision"], 2)
        self.target.projects.publish(imported["context"], expected_revision=2)
        # Receipts survive reopening and capability-file removal; stale CAS on a retry
        # must not create a fourth revision or replace the original result.
        self.bundle_path(token).unlink()
        reopened = ContextTransfer(
            self.target.scope, self.target.projects, self.target.artifacts
        )
        self.assertEqual(reopened.import_context(token, expected_revision=0), imported)
        self.assertEqual(len(self.target.projects.list()), 3)
        with closing(self.target.artifacts._connect()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 2
            )

    def test_storage_failure_rolls_back_snapshot_evidence_and_receipt(self):
        token = self.export()["transfer_id"]
        with closing(self.target.artifacts._connect()) as db:
            db.execute("""CREATE TRIGGER reject_second_evidence BEFORE INSERT ON artifacts
                WHEN (SELECT COUNT(*) FROM artifacts) >= 1
                BEGIN SELECT RAISE(ABORT, 'evidence storage rejected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.target.transfer.import_context(token)
        self.assert_empty_target()
        with closing(self.target.artifacts._connect()) as db:
            db.execute("DROP TRIGGER reject_second_evidence")
        imported = self.target.transfer.import_context(token)
        self.assertEqual(imported["revision"], 1)
        for artifact_id in imported["context"]["decisions"][0]["evidence_artifact_ids"]:
            self.assertEqual(
                self.target.artifacts.info(artifact_id)["context_id"],
                imported["context_id"],
            )

    def test_export_does_not_resolve_foreign_evidence(self):
        foreign = self.target.artifacts.publish(
            str(uuid4()), str(uuid4()), "foreign", "private target evidence"
        )
        snapshot = self.source.projects.publish(
            self.context | {"artifact_ids": [foreign["artifact_id"]]},
            expected_revision=1,
        )
        with self.assertRaises(ValueError):
            self.source.transfer.export(snapshot["context_id"], self.target.scope.root)
        self.assertFalse((self.base / "transfers").exists())
        self.assertEqual(
            self.target.artifacts.read(foreign["artifact_id"])["content"],
            "private target evidence",
        )

    def test_corrupt_schema_checksums_and_evidence_are_rejected_atomically(self):
        def unsupported(payload):
            payload["artifacts"][0]["media_type"] = "text/html"

        mutations = [
            (lambda p: p.update(format_version=2), True),
            (lambda p: p.update(format_version=True), True),
            (lambda p: p.update(tasks=[]), True),
            (lambda p: p["context"].update(product_summary="tampered"), True),
            (lambda p: p["artifacts"][0].update(content="tampered"), True),
            (lambda p: p["artifacts"].append(p["artifacts"][0]), True),
            (lambda p: p["artifacts"].pop(), True),
            (lambda p: p["artifacts"][0].update(artifact_id=str(uuid4())), True),
            (unsupported, True),
            (lambda p: p["source"].update(revision=2), False),
        ]
        for mutate, checksum in mutations:
            with self.subTest(mutation=mutate):
                token = self.export()["transfer_id"]
                self.change_bundle(token, mutate, update_checksum=checksum)
                with self.assertRaises(ValueError):
                    self.target.transfer.import_context(token)
                self.assert_empty_target()

    def test_duplicate_json_members_and_symlink_bundles_are_denied(self):
        token = self.export()["transfer_id"]
        path = self.bundle_path(token)
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b',"sha256":"' + b"0" * 64 + b'"}')
        with self.assertRaises(ValueError):
            self.target.transfer.import_context(token)
        external = self.root / "not-a-transfer.json"
        external.write_bytes(original)
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            self.target.transfer.import_context(token)
        self.assert_empty_target()

    def test_bundle_limit_rejects_without_truncation_or_partial_import(self):
        token = self.export()["transfer_id"]
        self.bundle_path(token).write_bytes(b" " * (8 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.target.transfer.import_context(token)
        self.assert_empty_target()
        large = [
            self.source.artifacts.publish(
                str(uuid4()), str(uuid4()), f"large {index}", "x" * (4 * 1024 * 1024)
            )
            for index in range(2)
        ]
        snapshot = self.source.projects.publish(
            self.context | {"artifact_ids": [entry["artifact_id"] for entry in large]},
            expected_revision=1,
        )
        before = set((self.base / "transfers").iterdir())
        with self.assertRaises(ValueError):
            self.source.transfer.export(snapshot["context_id"], self.target.scope.root)
        self.assertEqual(set((self.base / "transfers").iterdir()), before)
        for artifact in large:
            self.assertEqual(
                self.source.artifacts.info(artifact["artifact_id"])["characters"],
                4 * 1024 * 1024,
            )

    def test_exported_capability_is_private_and_immutable(self):
        first = self.export()
        path = self.bundle_path(first["transfer_id"])
        content = path.read_bytes()
        second = self.export()
        self.assertNotEqual(first["transfer_id"], second["transfer_id"])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
