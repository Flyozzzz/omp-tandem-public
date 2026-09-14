"""Pinned product policy remains reconstructible without native conversation history."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.models import parse_outcome
from omp_tandem.project_context import (
    ContextReadRequest,
    ProjectContextStore,
    read_task_context,
    task_context_packet,
)
from omp_tandem.task_contracts import TaskMessages
from omp_tandem.workspace import ProjectScope


class ContextPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = ProjectContextStore(self.root / "contexts.db")
        self.artifact = str(uuid4())
        self.data = {
            "project_id": "cafe",
            "product_summary": "面向 café 店主的产品。" * 30,
            "components": ["上传", "审批"],
            "rules": [
                {
                    "id": "required",
                    "text": "所有订单必须保留原始审批记录。",
                    "source": "负责人批准的规范 §2",
                    "applies_to": ["所有订单", "导入路径"],
                    "positive_examples": ["批准后保留历史记录。"],
                    "negative_examples": ["覆盖旧的批准记录。"],
                },
                {
                    "id": "advisory",
                    "requirement": "advisory",
                    "text": "建议按时间排序。",
                    "source": "用户调研",
                },
            ],
            "decisions": [
                {
                    "id": status,
                    "text": f"产品决策: {status}",
                    "status": status,
                    "source": "2026 年评审",
                    "evidence_artifact_ids": [self.artifact],
                    **({"supersedes": "superseded"} if status == "accepted" else {}),
                }
                for status in ("accepted", "rejected", "deferred", "superseded")
            ],
            "artifact_ids": [self.artifact],
        }
        metadata = self.projects.publish(self.data)
        self.snapshot = self.projects.get(metadata["context_id"])
        self.task = {
            "project_context_id": metadata["context_id"],
            "previous_project_context_id": None,
            "contract_json": json.dumps({"goal": "保留审批记录"}),
            "policy_json": json.dumps({"constraints": ["不得修改依赖"]}),
            "prompt": "",
            "workspace_roots": None,
            "mode": "analyze",
            "cwd": str(self.root),
            "question_timeout_seconds": 300,
        }
        scope = ProjectScope(self.root, self.root, "test", self.root, "test")
        self.messages = TaskMessages(scope, self.projects, None)

    def message(self, **changes):
        return json.loads(self.messages.build({**self.task, **changes}))

    def test_selectors_never_hide_required_rules_or_current_decisions(self):
        packet = task_context_packet(self.snapshot, {"decision_ids": ["accepted"]})
        context = packet["context"]
        required = context["rules"][0]
        for key in ("text", "source", "applies_to"):
            self.assertEqual(required[key], self.data["rules"][0][key])
        self.assertNotIn("text", context["rules"][1])
        current = {item["id"]: item for item in context["decisions"]}
        for original in self.snapshot["context"]["decisions"][:3]:
            self.assertEqual(current[original["id"]], original)
        self.assertEqual(current["superseded"]["status"], "superseded")
        self.assertNotIn("text", current["superseded"])
        self.assertEqual(current["accepted"]["supersedes"], "superseded")
        selected = task_context_packet(
            self.snapshot,
            {"advisory_rule_ids": ["advisory"], "decision_ids": ["superseded"]},
        )["context"]
        self.assertEqual(selected["rules"][1]["text"], self.data["rules"][1]["text"])
        self.assertEqual(
            selected["decisions"][-1], self.snapshot["context"]["decisions"][-1]
        )

    def test_invalid_selectors_fail_against_pinned_snapshot_even_in_full_mode(self):
        for options in (
            {"advisory_rule_ids": ["missing"]},
            {"advisory_rule_ids": ["required"]},
            {"decision_ids": ["missing"]},
            {"delivery": "full", "decision_ids": ["missing"]},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.message(
                    contract_json=json.dumps(
                        {"goal": "检查", "context_options": options}
                    )
                )
        with self.assertRaises(ValueError):
            self.message(
                project_context_id=None,
                contract_json=json.dumps(
                    {"goal": "检查", "context_options": {"decision_ids": ["accepted"]}}
                ),
            )

    def test_continuation_retains_policy_without_repeating_overview(self):
        first = self.message()["project_context"]
        continued = self.message(context_unchanged=1)["project_context"]
        self.assertEqual(
            first["context"]["product_summary"], self.data["product_summary"]
        )
        self.assertNotIn("product_summary", continued["context"])
        self.assertNotIn("components", continued["context"])
        self.assertEqual(continued["context"]["rules"], first["context"]["rules"])
        self.assertEqual(
            continued["context"]["decisions"], first["context"]["decisions"]
        )
        self.assertEqual(continued["context_id"], first["context_id"])
        self.assertEqual(continued["sha256"], first["sha256"])
        self.assertLess(
            continued["delivered_context_bytes"], first["delivered_context_bytes"]
        )
        pointer = continued["retrieval"]["pointers"]["product_summary"]
        overview = read_task_context(self.task, self.projects, {"pointer": pointer})
        self.assertEqual(json.loads(overview["content"]), self.data["product_summary"])
        updated = self.projects.publish(
            {**self.data, "product_summary": "新产品概览"}, expected_revision=1
        )
        changed = self.message(
            project_context_id=updated["context_id"],
            previous_project_context_id=first["context_id"],
            context_unchanged=0,
        )
        self.assertEqual(
            changed["project_context"]["context"]["product_summary"], "新产品概览"
        )
        self.assertEqual(changed["replaces_project_context_id"], first["context_id"])
        full = self.message(
            context_unchanged=1,
            contract_json=json.dumps(
                {"goal": "检查", "context_options": {"delivery": "full"}}
            ),
        )["project_context"]
        self.assertEqual(full["context"], self.snapshot["context"])

    def test_reader_reconstructs_exact_pinned_bytes_after_new_publication(self):
        self.projects.publish(
            {**self.data, "product_summary": "不应读取的新版本"}, expected_revision=1
        )
        pages = []
        offset = 0
        while True:
            page = read_task_context(
                self.task, self.projects, {"offset_bytes": offset, "max_bytes": 7}
            )
            content = page["content"].encode("utf-8")
            self.assertLessEqual(len(content), 7)
            self.assertEqual(page["returned_bytes"], len(content))
            self.assertEqual(page["offset_bytes"], offset)
            self.assertEqual(page["context_id"], self.snapshot["context_id"])
            pages.append(content)
            if page["next_offset_bytes"] is None:
                break
            self.assertEqual(page["next_offset_bytes"], offset + len(content))
            offset = page["next_offset_bytes"]
        reconstructed = b"".join(pages)
        self.assertEqual(json.loads(reconstructed), self.snapshot["context"])
        self.assertEqual(
            hashlib.sha256(reconstructed).hexdigest(), self.snapshot["sha256"]
        )
        self.assertEqual(len(reconstructed), page["total_bytes"])
        self.assertTrue(page["complete"])

    def test_capsule_omissions_remain_accessible_by_exact_pointers(self):
        pointers = self.message(context_unchanged=1)["project_context"]["retrieval"][
            "pointers"
        ]
        for pointer, expected in (
            (pointers["rules"]["required"], self.snapshot["context"]["rules"][0]),
            (pointers["rules"]["advisory"], self.snapshot["context"]["rules"][1]),
            (
                pointers["decisions"]["superseded"],
                self.snapshot["context"]["decisions"][-1],
            ),
            (pointers["artifact_ids"], [self.artifact]),
        ):
            result = read_task_context(self.task, self.projects, {"pointer": pointer})
            self.assertEqual(json.loads(result["content"]), expected)
            self.assertEqual(result["pointer"], pointer)
            self.assertEqual(result["publisher"], self.snapshot["publisher"])
            self.assertEqual(result["sha256"], self.snapshot["sha256"])

    def test_reader_rejects_unpinned_invalid_offsets_and_review_exposure(self):
        for request in (
            {"pointer": "/rules/00"},
            {"pointer": "/rules/-1"},
            {"pointer": "/product_summary/missing"},
            {"pointer": "/bad~2escape"},
            {"pointer": "/product_summary", "offset_bytes": 2},
            {"offset_bytes": 100000},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                read_task_context(self.task, self.projects, request)
        for request in (
            {"max_bytes": 3},
            {"max_bytes": 16385},
            {"offset_bytes": True},
            {"context_id": str(uuid4())},
        ):
            with self.subTest(request=request), self.assertRaises(ValidationError):
                ContextReadRequest.model_validate(request)
        for changes in (
            {"project_context_id": None},
            {"review_id": str(uuid4()), "review_stage": "independent"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                read_task_context({**self.task, **changes}, self.projects, {})
        with self.assertRaises(ValueError):
            self.messages.build(
                {**self.task, "project_context_id": str(uuid4())}, self.snapshot
            )

    def test_fresh_handoff_reintroduces_overview_without_native_history_or_grants(self):
        handoff = {
            "reason": "明确重新开始",
            "summary": "旧方案不再适用",
            "invalidated_assumptions": ["原路径已经删除"],
        }
        result = self.message(
            context_unchanged=1,
            handoff_json=json.dumps(handoff),
            previous_task_id="previous-task",
            previous_conversation_id="previous-conversation",
            session_file="/private/native-history.jsonl",
            execution_json='{"provider_key":"do-not-copy"}',
        )
        self.assertEqual(result["handoff"]["data"]["summary"], handoff["summary"])
        self.assertEqual(result["handoff"]["previous_task_id"], "previous-task")
        self.assertEqual(
            result["project_context"]["context"]["product_summary"],
            self.data["product_summary"],
        )
        self.assertEqual(result["work_policy"]["mode"], "analyze")
        encoded = json.dumps(result)
        self.assertNotIn("do-not-copy", encoded)
        self.assertNotIn("/private/native-history.jsonl", encoded)
        with self.assertRaises(ValidationError):
            self.message(
                handoff_json=json.dumps({**handoff, "provider_key": "not-allowed"})
            )

    def test_current_contract_selects_verification_without_changing_work_policy(self):
        result = self.message(
            contract_json=json.dumps(
                {
                    "goal": "检查",
                    "requirements": {
                        "requires_shell": True,
                        "entry_paths": ["orders.py"],
                    },
                    "verification": {
                        "stage": "candidate",
                        "preparation_seconds": 3,
                        "checks": [
                            {
                                "id": "target",
                                "criterion": "审批记录保留",
                                "estimated_seconds": 4,
                            },
                            {
                                "id": "candidate",
                                "criterion": "候选包完整",
                                "phase": "candidate",
                                "estimated_seconds": 5,
                            },
                            {
                                "id": "integration",
                                "criterion": "集成发布",
                                "phase": "integration",
                                "requires_shell": True,
                            },
                        ],
                    },
                }
            )
        )
        self.assertTrue(result["task"]["requirements"]["requires_shell"])
        selected = result["task"]["verification"]
        self.assertEqual(
            [check["id"] for check in selected["checks"]], ["target", "candidate"]
        )
        self.assertEqual(selected["estimated_seconds"], 12)
        self.assertEqual(result["work_policy"]["mode"], "analyze")
        self.assertEqual(result["work_policy"]["constraints"], ["不得修改依赖"])

    def test_legacy_contract_and_report_retain_product_assessments(self):
        message = self.message()
        self.assertEqual(message["task"]["goal"], "保留审批记录")
        self.assertEqual(
            message["project_context"]["context"]["rules"][0]["text"],
            self.data["rules"][0]["text"],
        )
        report = parse_outcome(
            {
                "outcome": "success",
                "answer": "审批记录已保留",
                "summary": "已核对",
                "rule_references": [
                    {
                        "rule_id": "required",
                        "assessment": "preserved",
                        "explanation": "历史记录未删除",
                    }
                ],
                "decision_references": ["rejected"],
            }
        )
        self.assertEqual(report.rule_references[0].rule_id, "required")
        self.assertEqual(report.decision_references, ["rejected"])


if __name__ == "__main__":
    unittest.main()
