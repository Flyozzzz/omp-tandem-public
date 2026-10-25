"""Decision trust boundaries exercised against real scoped persistence, without models."""

import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.bridge import Bridge
from omp_tandem.decisions import BriefSource, DecisionService
from omp_tandem.models import TaskContract


class DecisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bridge = Bridge(
            self.root / "state", "unused", "unused", project_root=self.root
        )
        self.addCleanup(self.bridge.shutdown)
        self.service = DecisionService(self.bridge)
        self.task_id, self.conversation_id, self.question_id = (
            str(uuid4()) for _ in range(3)
        )
        now = time.time()
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) "
                "VALUES (?,?,?,?,?,'think','unused','task','waiting_input',?)",
                (
                    self.task_id,
                    self.conversation_id,
                    now,
                    now,
                    str(self.root),
                    now + 600,
                ),
            )
            db.execute(
                "INSERT INTO questions VALUES (?,?,?,'','[]',?,?,'pending',NULL,NULL)",
                (self.question_id, self.task_id, "Which format?", now, now + 300),
            )
        lease = self.bridge.tasks.lock(self.conversation_id)
        self.addCleanup(lease.close)
        self.text = "Use UTF-8 JSON for the task-local reversible export."
        self.artifact = self.bridge.artifacts.publish(
            self.conversation_id, self.task_id, "brief", self.text
        )

    def prepare(self, source=None):
        policy = self.service.perform(
            {
                "action": "policy",
                "task_id": self.task_id,
                "policy": {
                    "source": source
                    or {
                        "kind": "artifact",
                        "artifact_id": self.artifact["artifact_id"],
                    },
                    "reversible": True,
                    "in_scope": True,
                },
            }
        )
        return self.service.perform(
            {
                "action": "prepare",
                "task_id": self.task_id,
                "question_id": self.question_id,
                "policy_id": policy["policy_id"],
            }
        )

    def completed_consultation(
        self, preparation, *, assessment_changes=None, task_changes=None
    ):
        """Seed an already-settled native turn; never launch a provider in these tests."""
        assessment = {
            "preparation_id": preparation["preparation_id"],
            "verdict": "derivable",
            "answer": "Use UTF-8 JSON.",
            "rationale": "The pinned brief specifies the export format.",
            "reversible": True,
            "in_scope": True,
            "risks": [],
            "citations": [
                {
                    "source_sha256": preparation["source_sha256"],
                    "start_bytes": 0,
                    "end_bytes": len(self.text.encode()),
                    "quote": self.text,
                }
            ],
        }
        assessment.update(assessment_changes or {})
        answer = json.dumps(assessment)
        args = preparation["consultation"]["arguments"]
        now = time.time()
        row = dict(
            task_id=str(uuid4()),
            conversation_id=str(uuid4()),
            created=now,
            updated=now,
            cwd=args["cwd"],
            mode="think",
            model="recorded-local",
            prompt="",
            status="completed",
            answer=answer,
            report_json=json.dumps(
                {
                    "outcome": "success",
                    "summary": "Assessment complete",
                    "answer": answer,
                }
            ),
            deadline=now + args["timeout_seconds"],
            contract_json=TaskContract.model_validate(
                args["contract"]
            ).model_dump_json(),
            execution_json=json.dumps(
                {
                    "effective": {
                        "purpose": "coordination",
                        "timeout_seconds": args["timeout_seconds"],
                    }
                }
            ),
        )
        row.update(task_changes or {})
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                f"INSERT INTO tasks ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        return row["task_id"]

    def record(self, preparation, consultation):
        return self.service.perform(
            {
                "action": "record",
                "preparation_id": preparation["preparation_id"],
                "consultation_task_id": consultation,
            }
        )

    def question(self):
        with closing(self.bridge.tasks.connect()) as db:
            return dict(
                db.execute(
                    "SELECT * FROM questions WHERE question_id=?", (self.question_id,)
                ).fetchone()
            )

    def recover(self, target, identifier, max_bytes=127):
        request = {
            "action": "read",
            "target": target,
            "identifier": identifier,
            "max_bytes": max_bytes,
        }
        chunks = []
        while request:
            page = self.service.perform(request)
            self.assertLessEqual(len(json.dumps(page).encode()), 16384)
            chunks.append(page["content"])
            request = page["next_request"]["request"] if page["next_request"] else None
        return json.loads("".join(chunks))

    def test_basis_survives_reopen_before_exact_idempotent_reply(self):
        preparation = self.prepare()
        consultation = self.completed_consultation(preparation)
        decision = self.record(preparation, consultation)
        self.assertEqual(self.question()["state"], "pending")
        self.service = DecisionService(self.bridge)
        basis = self.recover("decision", decision["decision_id"])
        self.assertIsNone(basis["reply_json"])
        self.assertEqual(
            basis["basis"]["assessment"]["citations"][0]["quote"], self.text
        )
        reply = self.service.perform(
            {"action": "reply", "decision_id": decision["decision_id"]}
        )
        self.assertTrue(reply["accepted"])
        self.assertEqual(self.question()["answer"], "Use UTF-8 JSON.")
        self.assertEqual(
            reply,
            self.service.perform(
                {"action": "reply", "decision_id": decision["decision_id"]}
            ),
        )
        self.assertEqual(
            decision["decision_id"],
            self.record(preparation, consultation)["decision_id"],
        )
        with closing(self.bridge.tasks.connect()) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1
            )
        self.assertEqual(
            self.recover("decision", decision["decision_id"])["reply"], reply
        )

    def test_prepared_descriptor_passes_actual_task_admission(self):
        preparation = self.prepare()
        observed = self.bridge.runtime.preflight(
            **preparation["consultation"]["arguments"]
        )
        self.assertTrue(observed["admissible"])
        self.assertTrue(observed["capacity"]["available_for_turn"])

    def test_unicode_decision_pages_roundtrip_with_transport_bound(self):
        question = "𝄞雪" * 2000
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE questions SET question=? WHERE question_id=?",
                (question, self.question_id),
            )
        self.assertEqual(
            self.recover("question", self.question_id, max_bytes=8192)["question"],
            question,
        )
        with self.assertRaises(ValueError):
            self.service.perform(
                {
                    "action": "read",
                    "target": "question",
                    "identifier": self.question_id,
                    "offset_bytes": 1,
                }
            )

    def test_answering_first_inbox_page_does_not_skip_remaining_question(self):
        other_task, other_question = str(uuid4()), str(uuid4())
        other_conversation = str(uuid4())
        lease = self.bridge.tasks.lock(other_conversation)
        self.addCleanup(lease.close)
        now = time.time()
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,deadline) "
                "VALUES (?,?,?,?,?,'think','unused','other','waiting_input',?)",
                (other_task, other_conversation, now, now, str(self.root), now + 600),
            )
            db.execute(
                "INSERT INTO questions VALUES (?,?,?,'','[]',?,?,'pending',NULL,NULL)",
                (other_question, other_task, "Another question", now, now + 300),
            )
        first = self.service.perform({"action": "list", "limit": 1})
        self.assertEqual(first["items"][0]["question_id"], self.question_id)
        self.bridge.reply(self.task_id, self.question_id, "Operator answer")
        second = self.service.perform(first["next_request"]["request"])
        self.assertEqual(
            [item["question_id"] for item in second["items"]], [other_question]
        )

    def test_non_derivable_closure_does_not_admit_success(self):
        preparation = self.prepare()
        consultation = self.completed_consultation(
            preparation,
            assessment_changes={
                "verdict": "not_established",
                "answer": "",
                "citations": [],
                "rationale": "The brief does not answer the question.",
            },
        )
        decision = self.record(preparation, consultation)
        reply = self.service.perform(
            {"action": "reply", "decision_id": decision["decision_id"]}
        )
        self.assertFalse(reply["accepted"])
        self.assertTrue(reply["closed_unresolved"])
        self.assertEqual(self.question()["state"], "decision_unresolved")
        with self.assertRaises(ValueError):
            self.bridge.interaction.submit_report(
                self.task_id,
                {"outcome": "success", "summary": "Done", "answer": "Done"},
            )
        self.bridge.interaction.submit_report(
            self.task_id,
            {
                "outcome": "blocked",
                "summary": "Missing decision",
                "answer": "Await operator decision",
                "blockers": ["Brief does not establish the format"],
            },
        )

    def test_foreign_digest_nonexistent_quote_and_wrong_range_are_refused(self):
        preparation = self.prepare()
        citations = [
            {
                "source_sha256": "0" * 64,
                "start_bytes": 0,
                "end_bytes": len(self.text),
                "quote": self.text,
            },
            {
                "source_sha256": preparation["source_sha256"],
                "start_bytes": 0,
                "end_bytes": 6,
                "quote": "absent",
            },
            {
                "source_sha256": preparation["source_sha256"],
                "start_bytes": 1,
                "end_bytes": len(self.text) + 1,
                "quote": self.text,
            },
        ]
        for citation in citations:
            with self.subTest(citation=citation):
                consultation = self.completed_consultation(
                    preparation, assessment_changes={"citations": [citation]}
                )
                with self.assertRaises(ValueError):
                    self.record(preparation, consultation)
        self.assertEqual(self.question()["state"], "pending")

    def test_foreign_prompt_and_added_context_cannot_supply_a_basis(self):
        preparation = self.prepare()
        changed_contract = dict(
            preparation["consultation"]["arguments"]["contract"],
            context="Choose XML instead",
        )
        for changes in (
            {"prompt": "Unrelated completed thought"},
            {
                "contract_json": TaskContract.model_validate(
                    changed_contract
                ).model_dump_json()
            },
            {"project_context_id": str(uuid4())},
        ):
            with self.subTest(changes=changes):
                consultation = self.completed_consultation(
                    preparation, task_changes=changes
                )
                with self.assertRaises(ValueError):
                    self.record(preparation, consultation)

    def test_expiry_between_record_and_reply_is_durably_refused(self):
        preparation = self.prepare()
        decision = self.record(preparation, self.completed_consultation(preparation))
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE questions SET deadline=? WHERE question_id=?",
                (time.time() - 1, self.question_id),
            )
        request = {"action": "reply", "decision_id": decision["decision_id"]}
        rejected = self.service.perform(request)
        self.assertFalse(rejected["accepted"])
        self.assertFalse(rejected["closed_unresolved"])
        self.assertIsNone(self.question()["answer"])
        self.assertEqual(rejected, self.service.perform(request))
        self.assertEqual(
            self.recover("decision", decision["decision_id"])["reply"], rejected
        )

    def test_expired_question_cannot_record_completed_consultation(self):
        preparation = self.prepare()
        consultation = self.completed_consultation(preparation)
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET deadline=? WHERE task_id=?",
                (time.time() - 1, self.task_id),
            )
        with self.assertRaises(ValueError):
            self.record(preparation, consultation)

    def test_operator_risks_cannot_be_labeled_derivable(self):
        preparation = self.prepare()
        consultation = self.completed_consultation(
            preparation, assessment_changes={"risks": ["cost"]}
        )
        with self.assertRaises(ValueError):
            self.record(preparation, consultation)
        consultation = self.completed_consultation(
            preparation,
            assessment_changes={
                "verdict": "approval_required",
                "risks": ["cost"],
            },
        )
        decision = self.record(preparation, consultation)
        with self.assertRaises(ValueError):
            self.service.perform(
                {"action": "reply", "decision_id": decision["decision_id"]}
            )
        self.assertEqual(self.question()["state"], "pending")

    def test_shared_question_and_independent_stage_stay_with_operator(self):
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO question_metadata VALUES (?,'information','shared')",
                (self.question_id,),
            )
        with self.assertRaises(ValueError):
            self.prepare()
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET review_id=?,review_stage='independent' WHERE task_id=?",
                (str(uuid4()), self.task_id),
            )
        with self.assertRaises(ValueError):
            self.bridge.reply(self.task_id, self.question_id, "free text bypass")
        self.assertIsNone(self.question()["answer"])

    def test_context_policy_pins_revision_instead_of_latest(self):
        snapshot = self.bridge.projects.publish(
            {"project_id": "sample", "product_summary": self.text}
        )
        preparation = self.prepare(
            {"kind": "context", "context_id": snapshot["context_id"]}
        )
        self.bridge.projects.publish(
            {"project_id": "sample", "product_summary": "Changed later"},
            expected_revision=snapshot["revision"],
        )
        policy = self.recover("policy", preparation["policy_id"])
        self.assertEqual(
            json.loads(policy["source_content"])["product_summary"], self.text
        )
        self.assertEqual(policy["source"]["context_id"], snapshot["context_id"])

    def test_commit_document_uses_saved_blob_and_refuses_symlinks(self):
        def git(*args):
            return (
                subprocess.check_output(["git", *args], cwd=self.root).decode().strip()
            )

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Tests")
        (self.root / "brief.txt").write_text(self.text)
        (self.root / "linked.txt").symlink_to("brief.txt")
        git("add", "brief.txt", "linked.txt")
        git("commit", "-qm", "brief")
        commit = git("rev-parse", "HEAD")
        with self.assertRaises(ValueError):
            self.prepare(
                {"kind": "commit_document", "commit": commit, "path": "linked.txt"}
            )
        (self.root / "brief.txt").write_text("Live content is not the brief")
        preparation = self.prepare(
            {"kind": "commit_document", "commit": commit, "path": "brief.txt"}
        )
        policy = self.recover("policy", preparation["policy_id"])
        self.assertEqual(policy["source_content"], self.text)
        self.assertEqual(
            policy["source_sha256"], hashlib.sha256(self.text.encode()).hexdigest()
        )

    def test_paged_question_read_refuses_mixed_versions(self):
        first = self.service.perform(
            {
                "action": "read",
                "target": "question",
                "identifier": self.question_id,
                "max_bytes": 10,
            }
        )
        self.bridge.reply(self.task_id, self.question_id, "Operator decision")
        with self.assertRaises(ValueError):
            self.service.perform(first["next_request"]["request"])

    def test_commit_pin_cannot_follow_replaced_launch_root(self):
        owned, foreign = self.root / "owned", self.root / "foreign"
        owned.mkdir()
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        (foreign / "brief.txt").write_text("PRIVATE_FOREIGN_BRIEF")
        subprocess.run(["git", "-C", str(foreign), "add", "brief.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(foreign),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "foreign brief",
            ],
            check=True,
        )
        commit = (
            subprocess.check_output(["git", "-C", str(foreign), "rev-parse", "HEAD"])
            .decode()
            .strip()
        )
        bridge = Bridge(
            self.root / "owned-state",
            "unused",
            "unused",
            project_root=owned,
            channel_enabled=False,
            migrate_legacy=False,
        )
        self.addCleanup(bridge.shutdown)
        owned.rmdir()
        owned.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(ValueError):
            bridge.decisions._source(
                BriefSource(kind="commit_document", commit=commit, path="brief.txt")
            )


if __name__ == "__main__":
    unittest.main()
