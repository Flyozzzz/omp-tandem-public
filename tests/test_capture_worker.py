"""Reserved child publication cannot outlive cancellation, budget, or its owner."""

from __future__ import annotations

import fcntl
import json
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


class CaptureWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "project"
        self.root.mkdir()
        (self.root / "code.txt").write_text("saved code\n")
        (self.root / "context.txt").write_text("saved context\n")
        self.scope = resolve_scope(self.home / "state", self.root)
        self.database = initialize_database(self.scope)
        self.store = ReviewStore(
            self.database, self.scope, ArtifactStore(self.database)
        )
        self.request = ReviewRequest(
            requirements="Keep the saved behavior",
            paths=["code.txt"],
            context_paths=["context.txt"],
            author_proposal="private author material",
        )
        self.reservation = CaptureReservation(str(uuid4()), str(uuid4()), str(uuid4()))
        self.lease = (
            self.scope.directory / f"review-owner-{self.reservation.owner}.lock"
        ).open("a")
        self.addCleanup(self.lease.close)
        fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.deadline = time.time() + 60
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("""CREATE TABLE review_runs (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, capture_id TEXT,
                capture_started INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL,
                status TEXT NOT NULL, deadline REAL NOT NULL, review_id TEXT,
                payload_json TEXT NOT NULL, updated REAL NOT NULL)""")
            db.execute(
                "INSERT INTO review_runs VALUES (?, ?, ?, 1, 'capture', 'starting', ?, NULL, ?, ?)",
                (
                    self.reservation.run_id,
                    self.reservation.owner,
                    self.reservation.review_id,
                    self.deadline,
                    json.dumps({"request": self.request.model_dump()}),
                    time.time(),
                ),
            )

    def update_run(self, **values):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "UPDATE review_runs SET " + ", ".join(key + "=?" for key in values),
                tuple(values.values()),
            )

    def assert_unpublished(self):
        with closing(sqlite3.connect(self.database)) as db:
            for table in ("reviews", "review_contents", "review_authors"):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
            self.assertIsNone(
                db.execute("SELECT review_id FROM review_runs").fetchone()[0]
            )

    def child(self, payload=None):
        return subprocess.run(
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
                self.reservation.run_id,
                "--owner",
                self.reservation.owner,
                "--review-id",
                self.reservation.review_id,
            ],
            input=self.request.model_dump_json().encode()
            if payload is None
            else payload,
            capture_output=True,
            cwd=self.home,
            timeout=15,
            check=False,
        )

    def test_child_publishes_reserved_id_and_atomic_mapping_without_author_leak(self):
        result = self.child()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        summary = json.loads(result.stdout)
        self.assertEqual(summary["review_id"], self.reservation.review_id)
        self.assertEqual((summary["change_count"], summary["context_count"]), (1, 1))
        self.assertNotIn(b"private author material", result.stdout)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT review_id, status, phase FROM review_runs"
                ).fetchone(),
                (self.reservation.review_id, "starting", "capture"),
            )
        (self.root / "code.txt").write_text("later source")
        self.assertEqual(
            self.store.read(self.reservation.review_id, "selected", "code.txt")[
                "content"
            ],
            "saved code\n",
        )
        self.assertEqual(
            self.store.read(self.reservation.review_id, "selected", "context.txt")[
                "content"
            ],
            "saved context\n",
        )
        # A late cancellation preserves the already committed immutable identity.
        self.update_run(status="cancelled")
        self.assertEqual(self.store.info(self.reservation.review_id), summary)
        with (
            patch.object(
                self.store, "_capture", side_effect=AssertionError("recaptured")
            ),
            self.assertRaises(ValueError),
        ):
            self.store.create(self.request, reservation=self.reservation)

    def test_duplicate_publication_never_recaptures_or_rewrites(self):
        original = self.store.create(self.request, reservation=self.reservation)
        (self.root / "code.txt").write_text("replacement")
        with (
            patch.object(
                self.store, "_capture", side_effect=AssertionError("recaptured")
            ),
            self.assertRaises(ValueError),
        ):
            self.store.create(self.request, reservation=self.reservation)
        self.assertEqual(self.store.info(self.reservation.review_id), original)
        self.assertEqual(
            self.store.read(self.reservation.review_id, "selected", "code.txt")[
                "content"
            ],
            "saved code\n",
        )

    def test_invalid_reservation_rejects_before_reading_source(self):
        cases = (
            {"status": "cancelled"},
            {"deadline": time.time() - 1},
            {"owner": str(uuid4())},
            {"capture_id": str(uuid4())},
            {"capture_started": 0},
            {"phase": "independent"},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                self.update_run(
                    status="starting",
                    deadline=self.deadline,
                    owner=self.reservation.owner,
                    capture_id=self.reservation.review_id,
                    capture_started=1,
                    phase="capture",
                )
                self.update_run(**changed)
                with (
                    patch.object(
                        self.store,
                        "_capture",
                        side_effect=AssertionError("read source"),
                    ),
                    self.assertRaises(ValueError),
                ):
                    self.store.create(self.request, reservation=self.reservation)
                self.assert_unpublished()

    def test_reservation_cannot_substitute_request_or_run_identity(self):
        different = self.request.model_copy(
            update={"requirements": "A different request"}
        )
        for request, reservation in (
            (different, self.reservation),
            (
                self.request,
                CaptureReservation(
                    str(uuid4()), self.reservation.owner, self.reservation.review_id
                ),
            ),
        ):
            with self.subTest(run_id=reservation.run_id):
                with self.assertRaises(ValueError):
                    self.store.create(request, reservation=reservation)
                self.assert_unpublished()
        with self.assertRaises(ValueError):
            CaptureReservation(
                self.reservation.run_id.replace("-", ""),
                self.reservation.owner,
                self.reservation.review_id,
            )

    def test_cancellation_deadline_and_owner_replacement_during_capture_roll_back(self):
        original_capture = self.store._capture
        for changed in (
            {"status": "cancelled"},
            {"deadline": time.time() - 1},
            {"owner": str(uuid4())},
            {"capture_id": str(uuid4())},
        ):
            with self.subTest(changed=changed):
                self.update_run(
                    status="starting",
                    deadline=self.deadline,
                    owner=self.reservation.owner,
                    capture_id=self.reservation.review_id,
                )

                def capture(request, changed=changed):
                    material = original_capture(request)
                    self.update_run(**changed)
                    return material

                with (
                    patch.object(self.store, "_capture", side_effect=capture),
                    self.assertRaises(ValueError),
                ):
                    self.store.create(self.request, reservation=self.reservation)
                self.assert_unpublished()

    def test_owner_dying_during_capture_cannot_publish(self):
        original_capture = self.store._capture

        def capture(request):
            material = original_capture(request)
            self.lease.close()
            return material

        with (
            patch.object(self.store, "_capture", side_effect=capture),
            self.assertRaises(ValueError),
        ):
            self.store.create(self.request, reservation=self.reservation)
        self.assert_unpublished()

    def test_deadline_and_owner_loss_during_inserts_roll_back_all_saved_bytes(self):
        connect = self.store._connect
        for reason in ("deadline", "owner"):
            with self.subTest(reason=reason):
                clock = [self.deadline - 1]

                def invalidate(reason=reason, clock=clock):
                    if reason == "deadline":
                        clock[0] = self.deadline + 1
                    else:
                        self.lease.close()
                    return 0

                def publication_connection():
                    db = connect()
                    db.create_function("invalidate_capture", 0, invalidate)
                    db.execute("""CREATE TEMP TRIGGER invalidate_publication
                        AFTER INSERT ON review_authors BEGIN
                        SELECT invalidate_capture(); END""")
                    return db

                with (
                    patch.object(
                        self.store, "_connect", side_effect=publication_connection
                    ),
                    patch(
                        "omp_tandem.reviews.time.time",
                        side_effect=lambda clock=clock: clock[0],
                    ),
                    self.assertRaises(ValueError),
                ):
                    self.store.create(self.request, reservation=self.reservation)
                self.assert_unpublished()

    def test_child_rejects_dead_owner_and_cancelled_row_without_publication(self):
        self.update_run(status="cancelled")
        result = self.child()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assert_unpublished()
        self.update_run(status="starting")
        self.lease.close()
        result = self.child()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"owner lease", result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assert_unpublished()

    def test_child_validates_database_identity_before_publication(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE bridge_scope SET project_root=?", (str(self.home),))
        result = self.child()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"different launch project", result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assert_unpublished()

    def test_child_bounds_input_and_does_not_echo_invalid_request(self):
        for payload in (b"x" * (16 * 1024 * 1024 + 1), b'{"secret":"do-not-echo"}'):
            with self.subTest(size=len(payload)):
                result = self.child(payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertLess(len(result.stderr), 1024)
                self.assertNotIn(b"do-not-echo", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assert_unpublished()
