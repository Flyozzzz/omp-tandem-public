"""Native team admission must validate facts, not a global ready flag or cached claim."""

import json
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.bridge import Bridge
from omp_tandem.jev_client import JevConfig
from omp_tandem.team_models import TeamPlan


class TeamAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bridge = Bridge(
            self.root / "state",
            "unused",
            None,
            project_root=self.root,
            channel_enabled=False,
            webhook_enabled=False,
            jev_config=JevConfig(),
        )
        self.addCleanup(self.bridge.shutdown)

    def reservation(self, *, candidate="evidence", policy="success", outcome="success"):
        plan = TeamPlan.model_validate(
            {
                "goal": "Collect evidence and summarize it",
                "cwd": str(self.root),
                "steps": [
                    {
                        "id": "evidence",
                        "title": "Evidence",
                        "mode": "analyze",
                        "contract": {"goal": "Collect evidence"},
                    },
                    {
                        "id": "final",
                        "title": "Summary",
                        "mode": "think",
                        "depends_on": ["evidence"],
                        "dependency_policy": policy,
                        "contract": {"goal": "Summarize available evidence"},
                    },
                ],
            }
        ).model_dump(mode="json")
        run_id, task_id, predecessor_id = str(uuid4()), str(uuid4()), str(uuid4())
        pair = {"model": "openai-codex/baseline", "thinking": "high"}
        selection = {
            "status": "needs_assignment" if candidate == "evidence" else "ready",
            "assignments": {candidate: pair},
            "eligible_assignments": {
                identifier: [pair] for identifier in ("evidence", "final")
            },
            "pending": ["final"] if candidate == "evidence" else [],
        }
        report = {
            "outcome": outcome,
            "summary": "Evidence recorded",
            "answer": "Saved evidence",
            "blockers": ["Authenticated evidence unavailable"]
            if outcome != "success"
            else [],
        }
        saved = {
            "task_id": predecessor_id,
            "status": "completed",
            "outcome": outcome,
            "report": report,
        }
        now = time.time()
        deadline = now + 60
        with closing(self.bridge.tasks.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO team_runs(run_id,owner,request_key,plan_json,selection_json,status,created,updated,deadline) VALUES(?,?,?,?,?,'running',?,?,?)",
                (
                    run_id,
                    self.bridge.channel.owner,
                    run_id,
                    json.dumps(plan),
                    json.dumps(selection),
                    now,
                    now,
                    deadline,
                ),
            )
            db.execute(
                "INSERT INTO team_steps(run_id,step_id,task_id,status,result_json,ordinal) VALUES(?,?,?,?,?,0)",
                (
                    run_id,
                    "evidence",
                    task_id if candidate == "evidence" else predecessor_id,
                    "reserved"
                    if candidate == "evidence"
                    else "completed"
                    if outcome == "success"
                    else "blocked",
                    None if candidate == "evidence" else json.dumps(saved),
                ),
            )
            db.execute(
                "INSERT INTO team_steps(run_id,step_id,task_id,status,ordinal) VALUES(?,?,?,?,1)",
                (
                    run_id,
                    "final",
                    task_id if candidate == "final" else None,
                    "reserved" if candidate == "final" else "pending",
                ),
            )
            if candidate == "final":
                db.execute(
                    "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,report_json,team_run_id) VALUES(?,?,?,?,?,'analyze',?,'','completed',?,?)",
                    (
                        predecessor_id,
                        str(uuid4()),
                        now,
                        now,
                        str(self.root),
                        pair["model"],
                        json.dumps(report),
                        run_id,
                    ),
                )
            db.commit()
        declared = next(step for step in plan["steps"] if step["id"] == candidate)
        record = {
            "task_id": task_id,
            "team_run_id": run_id,
            "model": pair["model"],
            "mode": declared["mode"],
            "cwd": str(self.root),
            "project_context_id": None,
            "execution_json": json.dumps({"effective": pair}),
            "contract_json": json.dumps(declared["contract"]),
            "deadline": now + 120,
        }
        return record, selection, deadline, predecessor_id

    def test_assigned_worker_admission_requires_its_exact_frozen_pair(self):
        record, selection, deadline, _ = self.reservation()
        self.bridge.tasks.validate_team_reservation(record)
        self.assertEqual(record["deadline"], deadline)
        selection["eligible_assignments"]["evidence"] = [
            {"model": "openai-codex/baseline", "thinking": "low"}
        ]
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE team_runs SET selection_json=? WHERE run_id=?",
                (json.dumps(selection), record["team_run_id"]),
            )
        with self.assertRaises(ValueError):
            self.bridge.tasks.validate_team_reservation(record)

    def test_cached_success_cannot_override_failed_native_execution(self):
        record, _, _, predecessor_id = self.reservation(candidate="final")
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET status='failed' WHERE task_id=?", (predecessor_id,)
            )
        with self.assertRaises(ValueError):
            self.bridge.tasks.validate_team_reservation(record)

    def test_partial_requires_explicit_read_only_synthesis_policy(self):
        strict, _, _, _ = self.reservation(candidate="final", outcome="partial")
        with self.assertRaises(ValueError):
            self.bridge.tasks.validate_team_reservation(strict)
        permitted, _, deadline, predecessor_id = self.reservation(
            candidate="final", policy="reported_evidence", outcome="partial"
        )
        self.bridge.tasks.validate_team_reservation(permitted)
        self.assertEqual(permitted["deadline"], deadline)
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET team_run_id=? WHERE task_id=?",
                (str(uuid4()), predecessor_id),
            )
        with self.assertRaises(ValueError):
            self.bridge.tasks.validate_team_reservation(permitted)
