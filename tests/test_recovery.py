"""Overview discovery observes persisted evidence without authority or lifecycle effects."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.recovery import RecoveryOverview
from omp_tandem.reviews import ReviewStore
from omp_tandem.work_items import WorkStore
from omp_tandem.workspace import resolve_scope


class RecoveryOverviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        root = home / "project"
        root.mkdir()
        self.scope = resolve_scope(home / "state", root)
        self.database = self.scope.directory / "tasks.sqlite3"
        self.work = WorkStore(self.database, self.scope)
        ReviewStore(self.database, self.scope, ArtifactStore(self.database))
        self.bridge = SimpleNamespace(
            scope=self.scope,
            work_items=self.work,
            work_participant="claude",
            work_token=None,
            _work_claims={},
        )
        self.overview = RecoveryOverview(self.bridge)

    def task(self, status="interrupted", stage=None):
        identifier = str(uuid4())
        with sqlite3.connect(self.database) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,answer,review_stage) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    str(uuid4()),
                    time.time(),
                    time.time(),
                    str(self.scope.root),
                    "think",
                    "local",
                    "SECRET_PROMPT",
                    status,
                    "SECRET_AUTHOR_ANSWER",
                    stage,
                ),
            )
        return identifier

    def dump(self):
        with sqlite3.connect(self.database) as db:
            return tuple(db.iterdump())

    def test_discovery_keeps_expired_questions_claims_and_saved_bytes_unchanged(self):
        interrupted = self.task()
        waiting = self.task("waiting_input", "independent")
        question, work_id = str(uuid4()), str(uuid4())
        with sqlite3.connect(self.database) as db:
            db.execute(
                "INSERT INTO questions(question_id,task_id,question,context,options_json,created,deadline,state,answer,answered) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    question,
                    waiting,
                    "SECRET_QUESTION",
                    "SECRET_CONTEXT",
                    "[]",
                    time.time(),
                    time.time() + 300,
                    "pending",
                    None,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO work_cards(work_id,card,revision,status,updated_at) VALUES (?,?,?,?,?)",
                (
                    work_id,
                    json.dumps({"token": "SECRET_CAPABILITY"}),
                    7,
                    "paused",
                    time.time(),
                ),
            )
        before = self.dump()
        result = self.overview.get()
        self.assertEqual(self.dump(), before)
        items = {item["id"]: item for item in result["items"]}
        self.assertEqual(items[interrupted]["category"], "operator_required_recovery")
        self.assertEqual(items[waiting]["category"], "pending_question")
        self.assertEqual(items[waiting]["question"]["question_id"], question)
        self.assertEqual(
            items[work_id]["next_observation"],
            {
                "tool": "tandem_work",
                "arguments": {"request": {"action": "get", "work_id": work_id}},
            },
        )
        self.assertFalse(result["replay_authorized"])
        self.assertNotIn("SECRET", json.dumps(result))
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE questions SET deadline=?", (time.time() - 1,))
        before = self.dump()
        result = self.overview.get()
        self.assertEqual(self.dump(), before)
        item = next(item for item in result["items"] if item["id"] == waiting)
        self.assertNotIn("question", item)

    def test_pages_find_every_saved_result_and_cursor_cannot_cross_scope(self):
        expected = {self.task("completed") for _ in range(7)}
        seen, cursor = set(), None
        while True:
            page = self.overview.get(limit=2, cursor=cursor)
            ids = {item["id"] for item in page["items"]}
            self.assertFalse(ids & seen)
            seen.update(ids)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, expected)
        cursor = self.overview.get(limit=2)["next_cursor"]
        with (
            patch.object(self.bridge, "work_participant", "omp"),
            self.assertRaises(ValueError),
        ):
            self.overview.get(cursor=cursor)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE bridge_scope SET scope_id='foreign'")
        with self.assertRaises(ValueError):
            self.overview.get()

    def test_snapshot_scope_and_restricted_seats_cannot_leak_author_material(self):
        own, foreign = str(uuid4()), str(uuid4())
        with sqlite3.connect(self.database) as db:
            for identifier, scope in ((own, self.scope.key), (foreign, "foreign")):
                db.execute(
                    "INSERT INTO reviews VALUES (?,?,?,?)",
                    (identifier, scope, time.time(), '{"author":"SECRET"}'),
                )
        result = self.overview.get()
        self.assertEqual([item["id"] for item in result["items"]], [own])
        self.bridge.work_token = "SECRET_CAPABILITY"
        with self.assertRaises(ValueError):
            self.overview.get()

    def test_waiting_models_and_cancelling_are_not_ready_results(self):
        cancelling = self.task("cancelling")
        waiting = str(uuid4())
        with sqlite3.connect(self.database) as db:
            db.execute(
                "CREATE TABLE team_runs(run_id TEXT PRIMARY KEY,status TEXT,updated REAL)"
            )
            db.execute(
                "INSERT INTO team_runs VALUES (?,?,?)",
                (waiting, "waiting_models", time.time()),
            )
        result = self.overview.get()
        observed = {item["id"]: item["category"] for item in result["items"]}
        self.assertEqual(observed[cancelling], "active_work")
        self.assertEqual(observed[waiting], "active_work")
