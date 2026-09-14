"""Bounded public reads preserve complete material and fresh capability gates."""

import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.task_results import TaskResults
from omp_tandem.task_store import TaskStore
from omp_tandem.work_access import WorkToolRequest, perform_work
from omp_tandem.work_items import WorkCommand, WorkPresentation, WorkStep, present_work

from . import test_work_items as fixtures


class ProjectionLimitsTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.WorkItemsTests(methodName="runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = self.case.store
        self.work_id = self.case.work_id

    def public(self, action="get", *, actor="claude", token=None, **presentation):
        return perform_work(
            self.store,
            {
                "action": action,
                **({"work_id": self.work_id} if action != "list" else {}),
            },
            actor=actor,
            attempt_token=token,
            presentation=WorkPresentation(**presentation),
        )

    def section(self, name, *, arguments=None):
        request = WorkToolRequest.model_validate(
            arguments
            or {
                "request": {"action": "get", "work_id": self.work_id},
                "section": name,
            }
        )
        content, cursor = [], None
        while True:
            page = perform_work(
                self.store,
                request.request,
                actor="claude",
                presentation=request.model_copy(update={"cursor": cursor}),
            )
            self.assertLessEqual(len(json.dumps(page).encode()), 16384)
            content.append(page["content"])
            cursor = page["next_cursor"]
            if cursor is None:
                return json.loads("".join(content))

    def test_large_required_sections_remain_bounded_and_fully_readable(self):
        for index in range(6):
            self.case.change(
                "block",
                step_id="backend",
                note=f"Blocker {index}: " + "界" * 6000,
                condition="Inspect all required evidence, not only the first page",
            )
        full = self.public(view="full")
        summary = self.public(limit=2)
        self.assertLessEqual(len(json.dumps(summary).encode()), 16384)
        blockers = self.section("blockers", arguments=summary["blockers"]["arguments"])
        expected = full["blockers"] + [
            b for step in full["steps"] for b in step["blockers"]
        ]
        self.assertEqual(blockers, expected)
        self.assertEqual(self.section("operator_commands"), full["operator_commands"])
        self.assertEqual(self.section("plan"), full["plan"])
        # Long operator/activation material must use the same explicit reader,
        # rather than an unbounded remaining-ID list or silently cut strings.
        large = dict(full)
        for key in ("next_actions", "operator_commands", "activation_preview"):
            large[key] = [{"required": "界" * 6000} for _ in range(20)]
        bounded = present_work(large, actor="claude")
        self.assertLessEqual(len(json.dumps(bounded).encode()), 16384)

    def test_list_keyset_does_not_load_later_cards_and_rejects_changed_context(self):
        with sqlite3.connect(self.store.database) as db:
            original = db.execute(
                "SELECT card FROM work_cards WHERE work_id=?", (self.work_id,)
            ).fetchone()[0]
            for identifier in ("zz-second", "zz-third"):
                card = json.loads(original)
                card["work_id"] = identifier
                db.execute(
                    "INSERT INTO work_cards(work_id,card,revision,status,updated_at) VALUES (?,?,?,?,?)",
                    (
                        identifier,
                        json.dumps(card),
                        card["revision"],
                        card["status"],
                        card["updated_at"],
                    ),
                )
            # Reading page one must not even decode this later corrupt record.
            db.execute("UPDATE work_cards SET card='not-json' WHERE work_id='zz-third'")
        first = self.public("list", limit=1)
        self.assertEqual([item["work_id"] for item in first["items"]], [self.work_id])
        cursor = first["next_cursor"]
        self.assertEqual(
            self.public("list", limit=1, cursor=cursor, actor="omp")["error"]["code"],
            "cursor_stale",
        )
        second = self.public("list", limit=1, cursor=cursor)
        self.assertEqual([item["work_id"] for item in second["items"]], ["zz-second"])
        self.case.change("agree")
        self.assertEqual(
            self.public("list", limit=1, cursor=cursor)["error"]["code"], "cursor_stale"
        )
        with sqlite3.connect(self.store.database) as db:
            card = json.loads(original)
            card["work_id"] = "zz-third"
            db.execute(
                "UPDATE work_cards SET card=? WHERE work_id='zz-third'",
                (json.dumps(card),),
            )
        self.assertEqual(
            len(self.store.perform({"action": "list"}, actor="claude")["items"]), 3
        )

    def test_section_cursor_requires_same_actor_revision_and_live_attempt(self):
        self.case.change(
            "block",
            step_id="backend",
            note="Required " * 1000,
            condition="Resolve explicitly",
        )
        page = self.public(section="blockers")
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        self.assertEqual(
            self.public(section="blockers", cursor=cursor, actor="omp")["error"][
                "code"
            ],
            "cursor_stale",
        )
        self.case.change("agree")
        self.assertEqual(
            self.public(section="blockers", cursor=cursor)["error"]["code"],
            "cursor_stale",
        )

    def test_history_pages_do_not_materialize_current_card_or_snapshots(self):
        self.case.agreed()
        expected = self.store.perform(
            {"action": "history", "work_id": self.work_id}, actor="claude"
        )["events"]
        with sqlite3.connect(self.store.database) as db:
            db.execute(
                "UPDATE work_cards SET card='not-json' WHERE work_id=?", (self.work_id,)
            )
        events, cursor = [], None
        while True:
            page = self.public("history", limit=1, cursor=cursor)
            events.extend(page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(
            [event["revision"] for event in events],
            [event["revision"] for event in expected],
        )
        self.assertTrue(all("snapshot" not in event for event in events))

    def test_capability_expiry_is_not_cached_by_card_revision(self):
        self.case.agreed()
        attempt = self.case.reserve(autonomous=False)
        before = self.store.perform(
            {"action": "get", "work_id": self.work_id},
            actor="omp",
            attempt_token=attempt["token"],
        )
        active = self.store.next_actions(
            before, actor="omp", attempt_token=attempt["token"]
        )
        self.assertTrue(
            next(item for item in active if item["action"] == "submit")["allowed"]
        )
        with self.store._transaction() as db:
            saved = self.store._attempt(db, attempt["attempt_id"])
            saved["deadline"] = 0
            self.store._save_attempt(db, saved)
        retired = self.store.next_actions(
            before, actor="omp", attempt_token=attempt["token"]
        )
        self.assertFalse(any(item["allowed"] for item in retired))
        self.assertEqual(retired[0]["blocked_reason"], "capability_retired")
        with self.assertRaisesRegex(ValueError, "expired|fenced"):
            self.public(actor="omp", token=attempt["token"], section="plan")

    def test_shell_ladder_refuses_before_spending_and_copies_selected_role(self):
        plan = fixtures.plan()
        plan["steps"][0]["requirements"] = {"requires_write": True}
        plan["steps"][0]["verification"] = {
            "stage": "candidate",
            "checks": [
                {
                    "id": "target",
                    "criterion": "Observable target",
                    "command": "python check.py",
                    "estimated_seconds": 5,
                }
            ],
        }
        self.case.revise(plan)
        self.case.agreed()
        grant = self.case.authorize()
        with self.assertRaisesRegex(ValueError, "shell requirement"):
            self.case.reserve()
        current = self.case.view()
        self.assertEqual(
            current["authorization"]["launches"], grant["authorization"]["launches"]
        )
        self.assertEqual(self.store.active_attempts(), [])
        self.case.authorize(allow_shell=True)
        attempt = self.case.reserve()
        self.assertTrue(attempt["requirements"]["requires_write"])
        self.assertEqual(attempt["verification"]["stage"], "candidate")
        self.case.submit(attempt)
        self.case.authorize(allow_shell=False)
        review = self.case.reserve(actor="claude", kind="review")
        self.assertFalse(review["requirements"]["requires_write"])
        self.assertIsNone(review["verification"])

    def test_review_write_declaration_is_invalid_not_implicit_authority(self):
        step = fixtures.plan()["steps"][0]
        with self.assertRaises(ValidationError):
            WorkStep.model_validate(
                {**step, "review_requirements": {"requires_write": True}}
            )

    def test_legacy_receipt_replays_with_new_empty_declarations(self):
        request = {
            "action": "create",
            "plan": fixtures.plan(),
            "expected_revision": 0,
            "operation_id": "create",
        }
        legacy = WorkCommand.model_validate(request).model_dump()
        for step in legacy["plan"]["steps"]:
            for key in (
                "review_context_paths",
                "requirements",
                "verification",
                "review_requirements",
                "review_verification",
            ):
                step.pop(key)
        digest = hashlib.sha256(
            json.dumps(
                {"command": legacy, "attempt_id": None},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        with sqlite3.connect(self.store.database) as db:
            db.execute(
                "UPDATE work_operations SET fingerprint=? WHERE actor='claude' AND operation_id='create'",
                (digest,),
            )
            saved = json.loads(
                db.execute(
                    "SELECT response FROM work_operations WHERE actor='claude' AND operation_id='create'"
                ).fetchone()[0]
            )
        replay = self.store.perform(request, actor="claude")
        self.assertEqual(replay, saved)

    def test_recent_never_reads_artifacts_and_full_result_retains_answer(self):
        tasks = TaskStore(self.case.scope, None)
        artifacts = ArtifactStore(self.store.database)
        results = TaskResults(tasks, artifacts, None)
        task_id, conversation = str(uuid4()), str(uuid4())
        answer = "Complete answer " * 3000
        report = {
            "outcome": "partial",
            "summary": "Delivered exact material",
            "answer": answer,
        }
        with closing(tasks.connect()) as db:
            db.execute(
                "INSERT INTO tasks(task_id,conversation_id,created,updated,cwd,mode,model,prompt,status,report_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    conversation,
                    1,
                    1,
                    str(self.case.root),
                    "analyze",
                    "test",
                    "question",
                    "completed",
                    json.dumps(report),
                ),
            )
        with (
            patch.object(
                artifacts,
                "read",
                side_effect=AssertionError("summary read artifact body"),
            ),
            patch.object(
                results,
                "view",
                side_effect=AssertionError("summary loaded full result"),
            ),
        ):
            recent = results.recent(1)
        self.assertEqual(recent[0]["summary"], report["summary"])
        self.assertEqual(recent[0]["outcome"], "partial")
        full = results.view(task_id, details=True)
        self.assertEqual(full["answer"], answer)
        self.assertFalse(full["answer_truncated"])
        self.assertEqual(full["usage"]["task"]["coverage"], "unknown")
