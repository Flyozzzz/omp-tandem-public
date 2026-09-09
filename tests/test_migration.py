"""Copy-only legacy migration regressions using disposable SQLite stores."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from omp_tandem import migration
from omp_tandem.artifacts import ArtifactStore
from omp_tandem.migration import migrate_legacy
from omp_tandem.project_context import ProjectContextStore

_TASK_SCHEMA = """CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
    created REAL NOT NULL, updated REAL NOT NULL, cwd TEXT NOT NULL,
    mode TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
    session_file TEXT, status TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
    error TEXT, activity TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
    contract_json TEXT, report_json TEXT, deadline REAL,
    result_artifacts TEXT NOT NULL DEFAULT '[]', policy_json TEXT,
    project_context_id TEXT, previous_project_context_id TEXT,
    question_timeout_seconds INTEGER, event_history_limit INTEGER
)"""
_QUESTION_SCHEMA = """CREATE TABLE questions (
    question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, question TEXT NOT NULL,
    context TEXT NOT NULL, options_json TEXT NOT NULL, created REAL NOT NULL,
    deadline REAL NOT NULL, state TEXT NOT NULL, answer TEXT, answered REAL
)"""


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a, self.b = self.root / "a", self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.base = self.root / "state"
        self.base.mkdir()
        (self.base / "sessions").mkdir()
        key = hashlib.sha256(str(self.a).encode()).hexdigest()
        directory = self.base / "projects" / key
        directory.mkdir(parents=True)
        self.scope = SimpleNamespace(
            root=self.a, base=self.base, key=key, directory=directory
        )
        self.source = self.base / "tasks.sqlite3"
        self.target = directory / "tasks.sqlite3"
        for database in (self.source, self.target):
            with closing(sqlite3.connect(database)) as db:
                db.execute(_TASK_SCHEMA)
                db.execute(_QUESTION_SCHEMA)
                db.commit()
            ArtifactStore(database)
            ProjectContextStore(database)

    def execute(self, database, sql, values=()):
        with closing(sqlite3.connect(database)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, values).fetchall()
            db.commit()
            return [dict(row) for row in rows]

    def task(
        self, cwd=None, conversation=None, status="completed", session=None, **fields
    ):
        conversation = conversation or str(uuid4())
        task = {
            "task_id": str(uuid4()),
            "conversation_id": conversation,
            "created": 1.0,
            "updated": 2.0,
            "cwd": str(cwd or self.a),
            "mode": "work",
            "model": "test/model",
            "prompt": "A private request",
            "status": status,
            "answer": "Readable final answer",
            "session_file": session,
            **fields,
        }
        self.execute(
            self.source,
            f"INSERT INTO tasks ({', '.join(task)}) VALUES ({', '.join('?' for _ in task)})",
            tuple(task.values()),
        )
        (self.base / f"{conversation}.lock").touch()
        return task

    def session(self, cwd=None, sidecar=False, title=True):
        path = self.base / "sessions" / f"{uuid4()}.jsonl"
        path.write_text(
            (
                json.dumps(
                    {
                        "type": "title",
                        "v": 1,
                        "title": "Native session",
                        "pad": " " * 40,
                    }
                )
                + "\n"
                if title
                else ""
            )
            + json.dumps(
                {"type": "session", "id": str(uuid4()), "cwd": str(cwd or self.a)}
            )
            + '\n{"type":"message","message":{"role":"assistant","content":"Final"}}\n'
        )
        if sidecar:
            folder = path.with_suffix("") / "nested"
            folder.mkdir(parents=True)
            (folder / "report.txt").write_text("Native sidecar content")
        return path

    def artifact(self, task, content="Evidence"):
        return ArtifactStore(self.source).publish(
            task["conversation_id"], task["task_id"], "evidence", content
        )

    def context(self, artifact_ids=(), project_id="product", **fields):
        return ProjectContextStore(self.source).publish(
            {
                "project_id": project_id,
                "product_summary": "Project product",
                "artifact_ids": list(artifact_ids),
                **fields,
            }
        )

    def migrate(self, **options):
        return migrate_legacy(self.scope, self.target, **options)

    def test_exact_roots_exclude_foreign_and_unmapped_nested_projects(self):
        nested = self.a / "nested"
        nested.mkdir()
        own = self.task()
        foreign = self.task(self.b)
        child = self.task(nested)
        result = self.migrate()
        self.assertEqual(
            [
                row["task_id"]
                for row in self.execute(self.target, "SELECT task_id FROM tasks")
            ],
            [own["task_id"]],
        )
        self.assertEqual(result["imported_conversations"], 1)
        self.assertNotIn(foreign["task_id"], json.dumps(result))
        self.assertNotIn(child["conversation_id"], json.dumps(result))
        self.assertNotIn(str(self.b), json.dumps(result))
        self.assertEqual(result["deferred_conversations"], 0)

    def test_explicit_mapping_adds_only_named_working_directories(self):
        nested = self.a / "nested"
        nested.mkdir()
        deeper = nested / "other-project"
        deeper.mkdir()
        mapped = self.task(nested)
        self.task(deeper)
        self.assertEqual(self.migrate()["imported_tasks"], 0)
        self.assertEqual(self.migrate(legacy_cwds=[nested])["imported_tasks"], 1)
        self.assertEqual(
            self.execute(self.target, "SELECT cwd FROM tasks"), [{"cwd": mapped["cwd"]}]
        )

    def test_operator_can_map_removed_external_worktree_without_rewriting_cwd(self):
        removed = self.root / "old-worktree"
        task = self.task(removed)
        self.assertEqual(self.migrate()["imported_tasks"], 0)
        result = self.migrate(legacy_cwds=[removed])
        self.assertEqual(result["imported_tasks"], 1)
        row = self.execute(self.target, "SELECT * FROM tasks")[0]
        self.assertEqual(row["cwd"], task["cwd"])
        self.assertEqual(row["answer"], task["answer"])
        with self.assertRaises(ValueError):
            self.migrate(legacy_cwds=["relative-worktree"])

    def test_active_and_flocked_conversations_defer_then_import_after_release(self):
        for status in ("starting", "running", "cancelling", "waiting_input"):
            self.task(status=status)
        settled = self.task()
        with (self.base / f"{settled['conversation_id']}.lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.migrate()
            self.assertEqual(result["imported_tasks"], 0)
            self.assertEqual(result["deferred_conversations"], 5)
        result = self.migrate()
        self.assertEqual(result["imported_tasks"], 1)
        self.assertEqual(result["deferred_conversations"], 4)

    def test_copy_preserves_database_session_sidecar_answers_and_questions(self):
        session = self.session(sidecar=True)
        task = self.task(
            session=str(session),
            report_json=json.dumps(
                {"outcome": "success", "answer": "Final", "artifact_ids": []}
            ),
        )
        artifact = self.artifact(task)
        self.execute(
            self.source,
            "UPDATE tasks SET result_artifacts=? WHERE task_id=?",
            (json.dumps([artifact["artifact_id"]]), task["task_id"]),
        )
        question = str(uuid4())
        self.execute(
            self.source,
            "INSERT INTO questions VALUES (?, ?, 'Question?', 'Context', '[]', 1, 2, 'answered', 'Approved', 2)",
            (question, task["task_id"]),
        )
        self.execute(self.source, "CREATE TABLE channel_secrets (secret TEXT)")
        self.execute(self.source, "INSERT INTO channel_secrets VALUES ('do-not-copy')")
        before = {
            path: path.read_bytes()
            for path in (
                self.source,
                session,
                session.with_suffix("") / "nested" / "report.txt",
            )
        }
        result = self.migrate()
        self.assertEqual(result["imported_tasks"], 1)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        row = self.execute(self.target, "SELECT * FROM tasks")[0]
        copied = Path(row["session_file"])
        self.assertTrue(copied.is_relative_to(self.scope.directory / "sessions"))
        self.assertNotEqual(copied, session)
        self.assertEqual(copied.read_bytes(), session.read_bytes())
        self.assertEqual(
            (copied.with_suffix("") / "nested" / "report.txt").read_text(),
            "Native sidecar content",
        )
        self.assertEqual(row["answer"], task["answer"])
        self.assertEqual(row["report_json"], task["report_json"])
        self.assertEqual(
            ArtifactStore(self.target).read(artifact["artifact_id"])["content"],
            "Evidence",
        )
        self.assertEqual(
            self.execute(
                self.target,
                "SELECT answer FROM questions WHERE question_id=?",
                (question,),
            ),
            [{"answer": "Approved"}],
        )
        self.assertEqual(
            self.execute(
                self.target,
                "SELECT name FROM sqlite_master WHERE name='channel_secrets'",
            ),
            [],
        )

    def test_repeated_migration_does_not_clobber_destination_continuation(self):
        task = self.task(session=str(self.session()))
        self.assertEqual(self.migrate()["imported_tasks"], 1)
        self.execute(
            self.target,
            "UPDATE tasks SET answer='Target continuation' WHERE task_id=?",
            (task["task_id"],),
        )
        self.execute(
            self.source,
            "UPDATE tasks SET answer='Later legacy continuation' WHERE task_id=?",
            (task["task_id"],),
        )
        result = self.migrate()
        self.assertEqual(result["already_imported"], 1)
        self.assertEqual(result["imported_tasks"], 0)
        self.assertEqual(
            self.execute(self.target, "SELECT answer FROM tasks"),
            [{"answer": "Target continuation"}],
        )
        self.assertEqual(
            len(
                list((self.scope.directory / "sessions" / ".legacy-imports").iterdir())
            ),
            1,
        )

    def test_absent_history_keeps_finished_result_without_resume_path(self):
        task = self.task(session=str(self.base / "sessions" / "missing.jsonl"))
        result = self.migrate()
        self.assertEqual(result["history_unavailable_tasks"], 1)
        row = self.execute(self.target, "SELECT answer, session_file FROM tasks")[0]
        self.assertEqual(row, {"answer": task["answer"], "session_file": None})

    def test_mixed_root_conversation_never_imports_partial_turns(self):
        task = self.task()
        self.task(self.b, conversation=task["conversation_id"])
        result = self.migrate()
        self.assertEqual(result["deferred_conversations"], 1)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])

    def test_foreign_contract_report_result_and_profile_evidence_defer(self):
        foreign = self.artifact(self.task(self.b), "Foreign confidential evidence")
        context = self.context(
            decisions=[
                {
                    "id": "decision",
                    "text": "Use evidence",
                    "status": "accepted",
                    "source": "Review",
                    "evidence_artifact_ids": [foreign["artifact_id"]],
                }
            ]
        )
        for field, value in (
            (
                "contract_json",
                json.dumps(
                    {"goal": "Do work", "artifact_ids": [foreign["artifact_id"]]}
                ),
            ),
            (
                "report_json",
                json.dumps(
                    {"outcome": "success", "artifact_ids": [foreign["artifact_id"]]}
                ),
            ),
            ("result_artifacts", json.dumps([foreign["artifact_id"]])),
            ("project_context_id", context["context_id"]),
        ):
            self.task(**{field: value})
        result = self.migrate()
        self.assertEqual(result["deferred_conversations"], 4)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])
        self.assertEqual(
            self.execute(self.target, "SELECT * FROM project_contexts"), []
        )
        self.assertEqual(self.execute(self.target, "SELECT * FROM artifacts"), [])
        self.assertNotIn("Foreign confidential", json.dumps(result))

    def test_owned_cross_conversation_evidence_imports_atomically(self):
        first = self.task()
        evidence = self.artifact(first)
        context = self.context([evidence["artifact_id"]])
        second = self.task(
            contract_json=json.dumps(
                {"goal": "Continue", "artifact_ids": [evidence["artifact_id"]]}
            ),
            project_context_id=context["context_id"],
        )
        result = self.migrate()
        self.assertEqual(result["imported_conversations"], 2)
        self.assertEqual(
            ProjectContextStore(self.target).get(context["context_id"])["sha256"],
            context["sha256"],
        )
        self.assertEqual(
            {
                row["task_id"]
                for row in self.execute(self.target, "SELECT task_id FROM tasks")
            },
            {first["task_id"], second["task_id"]},
        )

    def test_unreferenced_profiles_need_explicit_operator_selection_not_invented_evidence(
        self,
    ):
        selected = self.context(project_id="selected")
        untouched = self.context(project_id="untouched")
        self.migrate()
        self.assertEqual(
            self.execute(self.target, "SELECT * FROM project_contexts"), []
        )
        result = self.migrate(context_ids=[selected["context_id"]])
        self.assertEqual(result["imported_contexts"], 1)
        self.assertEqual(
            ProjectContextStore(self.target).get(selected["context_id"])["sha256"],
            selected["sha256"],
        )
        with self.assertRaises(ValueError):
            ProjectContextStore(self.target).get(untouched["context_id"])

    def test_titleless_native_history_remains_resumable(self):
        original = self.session(title=False)
        task = self.task(session=str(original))
        self.assertEqual(self.migrate()["imported_tasks"], 1)
        copied = self.execute(
            self.target,
            "SELECT session_file FROM tasks WHERE task_id=?",
            (task["task_id"],),
        )[0]["session_file"]
        self.assertEqual(Path(copied).read_bytes(), original.read_bytes())

    def test_profile_revision_collision_rolls_back_whole_conversation(self):
        context = self.context()
        self.task(project_context_id=context["context_id"])
        existing = ProjectContextStore(self.target).publish(
            {"project_id": "product", "product_summary": "Target product"}
        )
        result = self.migrate()
        self.assertEqual(result["deferred_conversations"], 1)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])
        self.assertEqual(
            self.execute(self.target, "SELECT context_id FROM project_contexts"),
            [{"context_id": existing["context_id"]}],
        )

    def test_copy_budget_defers_whole_group_then_operator_can_copy_unbounded(self):
        task = self.task(session=str(self.session(sidecar=True)))
        result = self.migrate(max_bytes=1)
        self.assertEqual(result["deferred_conversations"], 1)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])
        self.assertIn(
            "history_budget_exceeded_use_explicit_unlimited_migration",
            result["reasons"],
        )
        self.assertEqual(self.migrate(max_bytes=None)["imported_tasks"], 1)
        self.assertEqual(
            self.execute(self.target, "SELECT task_id FROM tasks"),
            [{"task_id": task["task_id"]}],
        )

    def test_foreign_header_and_symlinked_sidecar_are_rejected(self):
        self.task(session=str(self.session(self.b)))
        own = self.session(sidecar=True)
        (own.with_suffix("") / "foreign").symlink_to(self.b, target_is_directory=True)
        self.task(session=str(own))
        result = self.migrate()
        self.assertEqual(result["deferred_conversations"], 2)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])

    def test_existing_destination_conversation_is_not_merged(self):
        task = self.task()
        self.execute(
            self.target,
            "INSERT INTO tasks (task_id, conversation_id, created, updated, cwd, mode, model, prompt, status, answer) VALUES (?, ?, 1, 2, ?, 'work', 'model', 'target', 'completed', 'Keep target')",
            (str(uuid4()), task["conversation_id"], str(self.a)),
        )
        self.assertEqual(self.migrate()["deferred_conversations"], 1)
        self.assertEqual(
            self.execute(self.target, "SELECT answer FROM tasks"),
            [{"answer": "Keep target"}],
        )

    def test_explicit_profile_can_be_added_after_its_evidence_was_imported(self):
        evidence = self.artifact(self.task())
        context = self.context([evidence["artifact_id"]])
        self.assertEqual(self.migrate()["imported_contexts"], 0)
        result = self.migrate(context_ids=[context["context_id"]])
        self.assertEqual(result["imported_contexts"], 1)
        self.assertEqual(result["imported_tasks"], 0)
        self.assertEqual(
            ProjectContextStore(self.target).get(context["context_id"])["sha256"],
            context["sha256"],
        )

    def test_session_shared_with_foreign_task_defers_even_with_own_header(self):
        session = self.session()
        self.task(session=str(session))
        self.task(self.b, session=str(session))
        self.assertEqual(self.migrate()["deferred_conversations"], 1)
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])

    def test_missing_lock_defers_without_creating_source_file(self):
        task = self.task()
        path = self.base / f"{task['conversation_id']}.lock"
        path.unlink()
        self.assertEqual(self.migrate()["deferred_conversations"], 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])

    def test_retry_removes_only_uncommitted_staging_directories(self):
        self.task(session=str(self.session()))
        self.migrate()
        copied = Path(
            self.execute(self.target, "SELECT session_file FROM tasks")[0][
                "session_file"
            ]
        )
        staging_root = self.scope.directory / "sessions" / ".legacy-imports"
        orphan = staging_root / "snapshot-uncommitted"
        orphan.mkdir()
        (orphan / "partial.jsonl").write_text("Interrupted copy")
        self.migrate()
        self.assertFalse(orphan.exists())
        self.assertTrue(copied.is_file())

    def test_history_copy_does_not_block_source_or_target_writers(self):
        selected = self.task(session=str(self.session(sidecar=True)))
        foreign = self.task(cwd=self.b, status="running")
        current = {
            **foreign,
            "task_id": str(uuid4()),
            "conversation_id": str(uuid4()),
            "cwd": str(self.a),
        }
        self.execute(
            self.target,
            f"INSERT INTO tasks ({', '.join(current)}) VALUES ({', '.join('?' for _ in current)})",
            tuple(current.values()),
        )
        copy_history = migration._copy_history

        def copy_while_workers_commit(*args):
            for database, task in ((self.source, foreign), (self.target, current)):
                with closing(sqlite3.connect(database, timeout=0.1)) as writer:
                    writer.execute(
                        "UPDATE tasks SET activity='Still progressing' WHERE task_id=?",
                        (task["task_id"],),
                    )
                    writer.commit()
            return copy_history(*args)

        with patch.object(
            migration, "_copy_history", side_effect=copy_while_workers_commit
        ):
            result = self.migrate()
        self.assertEqual(result["imported_conversations"], 1, result)
        self.assertEqual(
            self.execute(
                self.target,
                "SELECT answer FROM tasks WHERE task_id=?",
                (selected["task_id"],),
            ),
            [{"answer": selected["answer"]}],
        )
        for database, task in ((self.source, foreign), (self.target, current)):
            self.assertEqual(
                self.execute(
                    database,
                    "SELECT activity FROM tasks WHERE task_id=?",
                    (task["task_id"],),
                ),
                [{"activity": "Still progressing"}],
            )

    def test_no_source_is_a_clear_noop(self):
        self.source.unlink()
        self.assertEqual(self.migrate()["status"], "no_legacy_store")
        self.assertEqual(self.execute(self.target, "SELECT * FROM tasks"), [])


if __name__ == "__main__":
    unittest.main()
