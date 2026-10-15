"""Wire budgets, complete recovery and scoped continuation for team responses."""

import base64
import hashlib
import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from omp_tandem.channel import ChannelDelivery
from omp_tandem.task_store import TaskStore, initialize_database
from omp_tandem.team_models import TeamPlan
from omp_tandem.team_presentation import TeamPresentation
from omp_tandem.team_runs import TeamRuns
from omp_tandem.workspace import resolve_scope


class TeamPresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.scope = resolve_scope(root / "state", root)
        database = initialize_database(self.scope)
        self.channel = ChannelDelivery(database, enabled=False, webhook_enabled=False)
        self.tasks = TaskStore(self.scope, self.channel)
        forbidden = Mock(
            side_effect=AssertionError("Presentation must not launch or replay workers")
        )
        self.catalog = Mock(
            return_value={"status": "ready", "provider": "openai-codex", "models": []}
        )
        self.runs = TeamRuns(
            self.tasks,
            self.scope,
            forbidden,
            forbidden,
            forbidden,
            SimpleNamespace(catalog=self.catalog),
            str(uuid4()),
        )
        # No controller is started by these durable fixture inserts.
        self.addCleanup(self.runs.lease.close)
        self.presentation = TeamPresentation(self.tasks, self.runs)
        self.run_id = str(uuid4())
        self.task_id = str(uuid4())
        self.long_text = '雪\U0001f680\\"\n\t\x00' * 3000
        self.plan = TeamPlan.model_validate(
            {
                "goal": "Recover the full plan",
                "steps": [
                    {
                        "id": "source",
                        "title": "Source",
                        "mode": "think",
                        "contract": {
                            "goal": "Gather evidence",
                            "context": self.long_text,
                        },
                    },
                    {
                        "id": "final",
                        "title": "Final",
                        "mode": "think",
                        "depends_on": ["source"],
                        "contract": {"goal": "Integrate evidence"},
                    },
                ],
            }
        ).model_dump(mode="json")
        self.report = {
            "outcome": "partial",
            "answer": self.long_text,
            "summary": "Partial evidence",
            "blockers": [self.long_text],
            "findings": [{"description": self.long_text}],
        }
        self.saved = {
            "status": "completed",
            "outcome": "partial",
            "answer": self.long_text,
            "report": self.report,
            "question": {"question_id": "old", "question": self.long_text},
        }
        self.selection = {
            "catalog": {
                "models": [
                    {"model": f"openai-codex/model-{index}"} for index in range(300)
                ]
            },
            "assignments": {},
            "pending": ["final"],
            "jev": {"reason": self.long_text},
        }
        with closing(self.tasks.connect()) as db:
            db.execute(
                "INSERT INTO team_runs(run_id,owner,request_key,plan_json,selection_json,status,created,updated,deadline,error,outcome) VALUES(?,?,?,?,?,'completed',1,1,? ,?,'partial')",
                (
                    self.run_id,
                    self.runs.owner,
                    "recovery",
                    json.dumps(self.plan),
                    json.dumps(self.selection),
                    time.time() + 3600,
                    self.long_text,
                ),
            )
            db.executemany(
                "INSERT INTO team_steps(run_id,step_id,task_id,status,result_json,ordinal) VALUES(?,?,?,'completed',?,?)",
                [
                    (self.run_id, "source", self.task_id, json.dumps(self.saved), 0),
                    (self.run_id, "final", None, json.dumps(self.saved), 1),
                ],
            )
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,answer,report_json,team_run_id) VALUES(?,?,1,1,?,'think','openai-codex/model','fixture','completed',?,?,?)",
                (
                    self.task_id,
                    str(uuid4()),
                    str(self.scope.root),
                    self.long_text,
                    json.dumps(self.report),
                    self.run_id,
                ),
            )

    def assert_bounded(self, page):
        wire = json.dumps(page).encode("utf-8")
        self.assertLessEqual(len(wire), 16384)
        self.assertEqual(json.loads(wire), page)

    def recover(self, request):
        parts = []
        offset = 0
        digest = None
        while request:
            arguments = dict(request)
            action = arguments.pop("action")
            if action == "models":
                page = self.presentation.models(self.channel, **arguments)
            else:
                page = self.presentation.read(channel=self.channel, **arguments)
            self.assert_bounded(page)
            self.assertEqual(page["offset_bytes"], offset)
            chunk = page["content"].encode("utf-8")
            self.assertEqual(page["returned_bytes"], len(chunk))
            digest = digest or page["sha256"]
            self.assertEqual(page["sha256"], digest)
            parts.append(chunk)
            offset += len(chunk)
            request = page["next_request"]
            self.assertEqual(page["next_offset_bytes"], offset if request else None)
        data = b"".join(parts)
        self.assertEqual(len(data), page["total_bytes"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        return json.loads(data)

    def test_escaped_unicode_sections_recover_complete_durable_evidence(self):
        for section, expected in (("plan", self.plan), ("selection", self.selection)):
            with self.subTest(section=section):
                self.assertEqual(
                    self.recover(
                        {"action": "read", "run_id": self.run_id, "section": section}
                    ),
                    expected,
                )
        run = self.recover({"action": "read", "run_id": self.run_id, "section": "run"})
        self.assertEqual(run["error"], self.long_text)
        source = next(step for step in run["steps"] if step["step_id"] == "source")
        step = self.recover(source["reader"])
        self.assertEqual(step["saved_result"], self.saved)
        self.assertEqual(step["native_result"]["report"], self.report)
        self.assertEqual(step["native_result"]["answer"], self.long_text)
        self.assertIsNone(step["current_question"])

    def test_digest_change_refuses_mixed_pages_and_restarts(self):
        first = self.presentation.read(self.run_id, "run", self.channel, max_bytes=64)
        with closing(self.tasks.connect()) as db:
            db.execute(
                "UPDATE team_runs SET error=? WHERE run_id=?",
                ("changed " + self.long_text, self.run_id),
            )
        request = dict(first["next_request"])
        request.pop("action")
        changed = self.presentation.read(channel=self.channel, **request)
        self.assert_bounded(changed)
        self.assertEqual(changed["status"], "digest_mismatch")
        self.assertNotIn("content", changed)
        recovered = self.recover(changed["restart_request"])
        self.assertEqual(recovered["error"], "changed " + self.long_text)
        with self.assertRaises(ValueError):
            self.presentation.read(self.run_id, "plan", self.channel, offset_bytes=1)
        first = self.presentation.read(self.run_id, "plan", self.channel)
        data = json.dumps(
            self.plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        interior = data.index("雪".encode()) + 1
        with self.assertRaises(ValueError):
            self.presentation.read(
                self.run_id,
                "plan",
                self.channel,
                offset_bytes=interior,
                expected_sha256=first["sha256"],
            )

    def test_question_recovery_distinguishes_current_and_saved_questions(self):
        question_id = str(uuid4())
        deadline = time.time() + 3600
        with closing(self.tasks.connect()) as db:
            db.execute(
                "UPDATE team_runs SET status='waiting_input' WHERE run_id=?",
                (self.run_id,),
            )
            db.execute(
                "UPDATE tasks SET status='waiting_input' WHERE task_id=?",
                (self.task_id,),
            )
            db.execute(
                "INSERT INTO questions(question_id,task_id,question,context,options_json,created,deadline,state) VALUES(?,?,?,?,?,1,?,'pending')",
                (
                    question_id,
                    self.task_id,
                    self.long_text,
                    self.long_text,
                    json.dumps([self.long_text]),
                    deadline,
                ),
            )
        request = {
            "action": "read",
            "run_id": self.run_id,
            "section": "step",
            "step_id": "source",
        }
        current = self.recover(request)
        self.assertEqual(current["current_question"]["question"], self.long_text)
        self.assertEqual(current["current_question"]["context"], self.long_text)
        self.assertEqual(current["current_question"]["options"], [self.long_text])
        self.assertEqual(current["saved_result"]["question"]["question_id"], "old")
        with closing(self.tasks.connect()) as db:
            db.execute(
                "UPDATE questions SET deadline=0 WHERE question_id=?", (question_id,)
            )
        expired = self.recover(request)
        self.assertIsNone(expired["current_question"])
        self.assertEqual(expired["saved_result"], self.saved)
        with closing(self.tasks.connect()) as db:
            db.execute(
                "UPDATE questions SET deadline=? WHERE question_id=?",
                (deadline, question_id),
            )
            db.execute(
                "UPDATE tasks SET cancel_requested=1 WHERE task_id=?", (self.task_id,)
            )
        self.assertIsNone(self.recover(request)["current_question"])

    def test_catalog_continuation_is_immutable_and_project_scoped(self):
        catalog = {
            "status": "ready",
            "provider": "openai-codex",
            "models": [
                {
                    "model": f"openai-codex/model-{index}",
                    "thinking": {"efforts": ["high"]},
                    "reasoning": True,
                }
                for index in range(600)
            ],
        }
        self.catalog.return_value = catalog
        first = self.presentation.models(self.channel)
        self.assert_bounded(first)
        self.catalog.side_effect = AssertionError("Continuation must not reprobe")
        # A new presentation object demonstrates persisted, not instance-local recovery.
        self.presentation = TeamPresentation(self.tasks, self.runs)
        remainder = []
        page = first
        while True:
            self.assert_bounded(page)
            remainder.append(page["content"].encode("utf-8"))
            if page["next_request"] is None:
                break
            arguments = dict(page["next_request"])
            arguments.pop("action")
            page = self.presentation.models(self.channel, **arguments)
        data = b"".join(remainder)
        self.assertEqual(hashlib.sha256(data).hexdigest(), first["sha256"])
        self.assertEqual(json.loads(data), catalog)
        missing = self.presentation.models(
            self.channel, cursor=f"{uuid4()}:1:{first['sha256']}"
        )
        self.assertEqual(missing["status"], "invalid_cursor")
        self.assertEqual(missing["restart_request"], {"action": "models"})

    def test_metadata_list_recovers_every_id_with_tied_timestamps(self):
        identifiers = {self.run_id}
        with closing(self.tasks.connect()) as db:
            for index in range(230):
                run_id = str(uuid4())
                identifiers.add(run_id)
                db.execute(
                    "INSERT INTO team_runs(run_id,owner,request_key,plan_json,status,created,updated,deadline) VALUES(?,?,?,?,'completed',1,1,1)",
                    (run_id, self.runs.owner, str(index), json.dumps(self.plan)),
                )
        seen = []
        cursor = None
        with patch.object(
            self.runs,
            "view",
            side_effect=AssertionError("List must not materialize worker reports"),
        ):
            while True:
                page = self.presentation.recent(self.channel, limit=100, cursor=cursor)
                self.assert_bounded(page)
                seen.extend(run["run_id"] for run in page["runs"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(set(seen), identifiers)
        self.assertEqual(len(seen), len(identifiers))
        foreign = base64.urlsafe_b64encode(
            json.dumps(["another-project", 1, self.run_id]).encode()
        ).decode()
        self.assertEqual(
            self.presentation.recent(self.channel, cursor=foreign)["status"],
            "invalid_cursor",
        )

    def test_projection_preserves_actions_and_partial_markers_after_delivery(self):
        steps = [
            {
                "id": f"step-{index}",
                "task_id": str(uuid4()),
                "status": "waiting_input",
                "outcome": "partial",
                "selected_model": "openai-codex/selected",
                "selected_thinking": "high",
                "execution": {
                    "actual": {"model": "openai-codex/actual", "thinking": "high"}
                },
                "summary": self.long_text,
                "title": self.long_text,
                "error": self.long_text,
                "result": self.saved,
            }
            for index in range(16)
        ]
        raw = {
            "run_id": self.run_id,
            "status": "running",
            "outcome": "partial",
            "integration_step": "step-15",
            "next_action": "reply",
            "steps": steps,
            "pending_models": ["step-15"],
            "incomplete_steps": [step["id"] for step in steps],
            "actions": [
                {
                    "action": "reply",
                    "task_id": steps[0]["task_id"],
                    "question_id": str(uuid4()),
                    "deadline": 100,
                }
            ],
            "answer": self.long_text,
            "answer_artifact_id": str(uuid4()),
            "outcome_artifact_id": str(uuid4()),
            "final": {"answer_length": len(self.long_text), "report": self.report},
            "goal": self.long_text,
            "error": self.long_text,
            "wait": {"mode": "auto", "reason": "question"},
        }
        page = self.presentation.present(raw, self.channel, details=True)
        self.assert_bounded(page)
        self.assertEqual(page["next_action"], "reply")
        self.assertEqual(
            [action["action"] for action in page["actions"]], ["reply", "assign"]
        )
        self.assertNotIn("pending", page)
        self.assertEqual(page["pending_models"], ["step-15"])
        self.assertEqual(page["incomplete_steps"], raw["incomplete_steps"])
        self.assertEqual(page["outcome"], "partial")
        self.assertTrue(page["answer_truncated"])
        self.assertEqual(page["answer_length"], len(self.long_text))
        self.assertEqual(page["answer_artifact_id"], raw["answer_artifact_id"])
        self.assertEqual(page["outcome_artifact_id"], raw["outcome_artifact_id"])
        self.assertEqual(
            page["steps"][0]["execution"]["actual"]["model"], "openai-codex/actual"
        )
        raw["actions"] = []
        raw["next_action"] = "agree_models"
        self.assertEqual(
            self.presentation.present(raw, self.channel)["next_action"], "agree_models"
        )

    def test_terminal_outcome_matches_the_final_lifecycle_observation(self):
        with closing(self.tasks.connect()) as db:
            db.execute(
                "UPDATE team_runs SET status='running',outcome=NULL WHERE run_id=?",
                (self.run_id,),
            )
        observe = self.runs.observation

        def finish_before_observation(run_id):
            with closing(self.tasks.connect()) as db:
                db.execute(
                    "UPDATE team_runs SET status='completed',outcome='partial' WHERE run_id=?",
                    (run_id,),
                )
            return observe(run_id)

        with patch.object(
            self.runs, "observation", side_effect=finish_before_observation
        ):
            result = self.runs.view(self.run_id)
        self.assertEqual(
            (result["status"], result["outcome"]), ("completed", "partial")
        )

    def test_list_recovers_a_lost_owner_without_loading_worker_results(self):
        foreign_owner = str(uuid4())
        lease = self.runs._lease(foreign_owner)
        try:
            with closing(self.tasks.connect()) as db:
                db.execute(
                    "UPDATE team_runs SET owner=?,status='running',outcome=NULL WHERE run_id=?",
                    (foreign_owner, self.run_id),
                )
            live = self.presentation.recent(self.channel, limit=1)
            self.assertEqual(live["runs"][0]["status"], "running")
        finally:
            lease.close()
        recovered = self.presentation.recent(self.channel, limit=1)
        self.assert_bounded(recovered)
        self.assertEqual(recovered["runs"][0]["status"], "interrupted")
        self.assertEqual(recovered["runs"][0]["run_id"], self.run_id)
        with closing(self.tasks.connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM team_runs WHERE run_id=?", (self.run_id,)
                ).fetchone()["status"],
                "interrupted",
            )
