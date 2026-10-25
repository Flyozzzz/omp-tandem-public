"""Bounded public results recover exact durable evidence without worker execution."""

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from omp_tandem.bridge import Bridge
from omp_tandem.errors import ProtocolError
from omp_tandem.presentation import (
    MAX_READ_BYTES,
    MAX_RESPONSE_BYTES,
    ResultPresentation,
    wire_size,
)


class ResultPresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bridge = Bridge(
            self.root / "state", "unused", "unused", project_root=self.root
        )
        self.addCleanup(self.bridge.shutdown)
        self.presentation = ResultPresentation(self.bridge)
        self.task_id, self.conversation_id = str(uuid4()), str(uuid4())
        self.answer = '雪界𝄞"\n\x00' * 3000
        self.report = {
            "outcome": "partial",
            "answer": self.answer,
            "summary": "Saved assessment",
            "blockers": [self.answer],
        }
        self.contract = {"goal": "Recover exact evidence", "context": self.answer}
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,answer,report_json,contract_json) VALUES(?,?,1,1,?,'think','unused','fixture','completed',?,?,?)",
                (
                    self.task_id,
                    self.conversation_id,
                    str(self.root),
                    self.answer,
                    json.dumps(self.report),
                    json.dumps(self.contract),
                ),
            )

    def recover(self, reader):
        request, parts, offset, digest = reader["arguments"], [], 0, None
        while request is not None:
            page = self.presentation.read(**request)
            self.assertLessEqual(wire_size(page), MAX_RESPONSE_BYTES)
            self.assertEqual(page["offset_bytes"], offset)
            chunk = page["content"].encode("utf-8")
            self.assertEqual(page["returned_bytes"], len(chunk))
            self.assertLessEqual(len(chunk), MAX_READ_BYTES)
            digest = digest or page["sha256"]
            self.assertEqual(page["sha256"], digest)
            parts.append(chunk)
            offset += len(chunk)
            request = page["next_request"]
            self.assertEqual(page["next_offset_bytes"], offset if request else None)
        recovered = b"".join(parts)
        self.assertEqual(hashlib.sha256(recovered).hexdigest(), digest)
        return recovered.decode("utf-8")

    def test_unicode_results_bound_both_views_without_mutating_domain(self):
        raw = self.bridge.view(self.task_id, details=True)
        original = copy.deepcopy(raw)
        for details in (False, True):
            result = self.presentation.task(raw, details=details)
            self.assertLessEqual(wire_size(result), MAX_RESPONSE_BYTES)
            self.assertEqual(result["task_id"], self.task_id)
            self.assertEqual(result["outcome"], "partial")
            self.assertTrue(result["answer_truncated"])
            self.assertTrue(self.answer.startswith(result["answer"]))
            self.assertEqual(self.recover(result["readers"]["answer"]), self.answer)
            self.assertEqual(
                json.loads(self.recover(result["readers"]["report"])), self.report
            )
            self.assertEqual(
                json.loads(self.recover(result["readers"]["contract"])), self.contract
            )
            self.assertEqual(
                json.loads(self.recover(result["readers"]["result"])), original
            )
        self.assertEqual(raw, original)
        self.assertEqual(
            self.bridge.view(self.task_id, details=True)["report"]["answer"],
            self.answer,
        )

    def test_changed_source_refuses_continuation_and_restart_recovers_new_value(self):
        first = self.presentation.read("task", self.task_id, "answer", max_bytes=7)
        self.report["answer"] += " changed"
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET report_json=? WHERE task_id=?",
                (json.dumps(self.report), self.task_id),
            )
        stale = self.presentation.read(**first["next_request"])
        self.assertEqual(stale["status"], "digest_mismatch")
        self.assertIsNone(stale["content"])
        self.assertEqual(
            self.recover({"arguments": stale["restart_request"]}), self.report["answer"]
        )
        with self.assertRaises(ProtocolError) as refusal:
            self.presentation.read("task", self.task_id, "answer", offset_bytes=3)
        self.assertEqual(refusal.exception.envelope["code"], "missing_digest")
        with self.assertRaises(ProtocolError) as refusal:
            self.presentation.read(
                "task",
                self.task_id,
                "answer",
                offset_bytes=1,
                expected_sha256=stale["sha256"],
            )
        self.assertEqual(refusal.exception.envelope["code"], "invalid_offset")

    def test_small_utf8_pages_reassemble_without_splitting_codepoints(self):
        reader = {
            "arguments": {
                "target": "task",
                "identifier": self.task_id,
                "section": "answer",
                "max_bytes": 7,
            }
        }
        # A short value is enough to cross every boundary; avoid thousands of reads.
        self.report["answer"] = '雪界𝄞"\n\x00' * 3
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "UPDATE tasks SET report_json=? WHERE task_id=?",
                (json.dumps(self.report), self.task_id),
            )
        self.assertEqual(self.recover(reader), self.report["answer"])

    def test_review_stages_recover_independently_without_duplicate_report_answers(self):
        run_id = str(uuid4())
        independent = self.bridge.view(self.task_id, details=True)
        comparison = {
            **independent,
            "task_id": str(uuid4()),
            "answer": self.answer + " comparison",
        }
        with closing(self.bridge.tasks.connect()) as db:
            db.execute(
                "INSERT INTO review_runs(run_id,owner,request_key,payload_json,created,updated,deadline,status,phase,outcome,independent_json,comparison_json) VALUES(?,?,?,'{}',1,2,3,'completed','comparison','partial',?,?)",
                (
                    run_id,
                    self.bridge.review_runs.owner,
                    run_id,
                    json.dumps(independent),
                    json.dumps(comparison),
                ),
            )
        raw = self.bridge.review_runs.view(run_id)
        result = self.presentation.review(raw)
        self.assertLessEqual(wire_size(result), MAX_RESPONSE_BYTES)
        self.assertEqual(result["run_id"], run_id)
        for stage, expected in (
            ("independent", independent),
            ("comparison", comparison),
        ):
            self.assertEqual(
                json.loads(self.recover(result["readers"][stage])), expected
            )
            self.assertEqual(raw[stage], expected)

    def test_large_question_retains_identity_and_requires_full_read_before_reply(self):
        question_id = str(uuid4())
        raw = {
            "task_id": self.task_id,
            "status": "waiting_input",
            "next_action": "reply",
            "question": {
                "question_id": question_id,
                "question": self.answer,
                "options": [self.answer],
                "context": self.answer,
                "deadline": 1234,
                "kind": "approval",
                "blocking_scope": "shared",
            },
        }
        original = copy.deepcopy(raw)
        result = self.presentation.task(raw)
        self.assertLessEqual(wire_size(result), MAX_RESPONSE_BYTES)
        self.assertEqual(result["next_action"], "read_question")
        self.assertEqual(result["question"]["question_id"], question_id)
        self.assertEqual(result["question"]["deadline"], 1234)
        self.assertEqual(result["question"]["kind"], "approval")
        self.assertEqual(result["question"]["blocking_scope"], "shared")
        self.assertTrue(result["question"]["truncated"])
        self.assertEqual(raw, original)

    def test_artifact_character_offsets_survive_byte_budget_and_embedded_nuls(self):
        artifact = self.bridge.artifacts.publish(
            self.conversation_id, self.task_id, "unicode", self.answer
        )
        parts, offset = [], 0
        while True:
            page = self.bridge.artifacts.read(artifact["artifact_id"], offset, 50000)
            self.assertLessEqual(wire_size(page), MAX_RESPONSE_BYTES)
            self.assertEqual(
                page["content"], self.answer[offset : offset + len(page["content"])]
            )
            parts.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertEqual(page["next_offset"], offset + len(page["content"]))
            offset = page["next_offset"]
        self.assertEqual("".join(parts), self.answer)

    def test_unknown_project_cannot_read_task_or_review_result(self):
        other_root = self.root / "other"
        other_root.mkdir()
        other = Bridge(self.root / "state", "unused", "unused", project_root=other_root)
        self.addCleanup(other.shutdown)
        with self.assertRaises(ValueError):
            ResultPresentation(other).read("task", self.task_id, "answer")
        with self.assertRaises(ProtocolError) as refusal:
            self.presentation.read("review_run", str(uuid4()), "answer")
        self.assertEqual(refusal.exception.envelope["code"], "invalid_result_section")
