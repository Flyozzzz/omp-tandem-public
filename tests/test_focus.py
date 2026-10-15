"""Observable consent, exact-source, reservation and cancellation boundaries."""

import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.artifacts import ArtifactStore
from omp_tandem.focus import (
    FocusCandidate,
    FocusEngine,
    FocusInput,
    FocusInterrupted,
    FocusPolicy,
    load_focus_policy,
)
from omp_tandem.jev_client import JevClient, JevConfig, canonical_json


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def policy(**changes):
    return FocusPolicy.model_validate(
        {
            "allow_external": True,
            "data_classes": [
                "optional_project_context",
                "declared_or_own_artifacts",
                "tool_catalog",
            ],
            **changes,
        }
    )


def intent(**changes):
    return {
        "goal": "Investigate parser failure",
        "allow_external": True,
        "surfaces": ["context", "outputs", "tools"],
        **changes,
    }


def response(payload, choice="relevant"):
    questions = json.loads(payload)["questions"]
    return {
        "model": "typesafe/jev-1.13",
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "answers": {
            key: {
                "type": "choice",
                "choice": choice,
                "confidence": 1,
                "probabilities": {
                    label: int(label == choice) for label in question["criteria"]
                },
            }
            for key, question in questions.items()
        },
    }


class FocusSchemaTests(unittest.TestCase):
    def test_consent_cannot_be_coerced_or_smuggled_in_extra_fields(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                FocusInput.model_validate(intent(allow_external=value))
        for change in (
            {"surfaces": ["context", "context"]},
            {"goal": " \n"},
            {"prompt": "implicit consent"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                FocusInput.model_validate(intent(**change))
        with self.assertRaises(ValidationError):
            policy(request_timeout_seconds=float("inf"))
        with self.assertRaises(ValidationError):
            policy(max_requests=True)
        altered = FocusInput.model_validate(intent())
        altered.allow_external = "yes"
        with self.assertRaises(ValidationError):
            FocusInput.model_validate(altered)

    def test_loader_rejects_ambiguous_unbounded_and_nonregular_inputs_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "focus.json"
            for raw in (
                b'{"allow_external":false,"allow_external":true}',
                b'{"request_timeout_seconds":NaN}',
                b'{"private_secret":"never echo"}',
                b" " * 32001,
                b"\xff",
            ):
                path.write_bytes(raw)
                with (
                    self.subTest(raw=raw[:40]),
                    self.assertRaises(ValueError) as caught,
                ):
                    load_focus_policy(path)
                self.assertNotIn("never echo", str(caught.exception))
                self.assertNotIn(str(path), str(caught.exception))
            path.write_text(canonical_json(policy().model_dump(mode="json")))
            self.assertTrue(load_focus_policy(path).allow_external)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                load_focus_policy(link)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                load_focus_policy(fifo)


class FocusEngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.task = {
            "task_id": str(uuid4()),
            "conversation_id": str(uuid4()),
            "status": "running",
            "deadline": time.time() + 60,
            "mode": "work",
            "contract_json": "{}",
            "project_context_id": str(uuid4()),
            "cancel_requested": False,
        }
        self.tasks = SimpleNamespace(connect=self.connect, get=self.get_task)
        self.artifacts = ArtifactStore(self.path)
        context = {
            "project_id": "fixture",
            "product_summary": "Required product facts",
            "components": [],
            "artifact_ids": [],
            "decisions": [],
            "rules": [
                {
                    "id": "private-rule-id",
                    "text": "Investigate parser failure carefully",
                    "requirement": "advisory",
                    "source": "private/source/path",
                    "positive_examples": [],
                    "negative_examples": [],
                }
            ],
        }
        self.snapshot = {
            "context_id": self.task["project_context_id"],
            "context": context,
            "sha256": digest(context),
        }
        self.projects = SimpleNamespace(
            get=lambda context_id: copy.deepcopy(self.snapshot)
        )
        self.engine = FocusEngine(
            self.tasks,
            self.projects,
            self.artifacts,
            policy=policy(),
            config=JevConfig(api_key="synthetic-key"),
        )
        with closing(self.connect()) as db, db:
            db.execute(
                "CREATE TABLE task_focus(task_id TEXT PRIMARY KEY,record_json TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE task_focus_attempts(task_id TEXT NOT NULL,purpose TEXT NOT NULL,input_sha256 TEXT NOT NULL,attempt_id TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,reserved_at REAL NOT NULL,timeout_seconds REAL NOT NULL,result_json TEXT,finished_at REAL,PRIMARY KEY(task_id,purpose,input_sha256),CHECK((result_json IS NULL AND finished_at IS NULL) OR (result_json IS NOT NULL AND finished_at IS NOT NULL)))"
            )
        self.freeze()
        self.sent = []

        async def request(client, payload):
            self.sent.append(json.loads(payload))
            return response(payload), None

        self.transport = patch.object(JevClient, "_request", request)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def get_task(self, task_id, *, refresh):
        if refresh or task_id != self.task["task_id"]:
            raise ValueError("Unscoped or refreshing read")
        return copy.deepcopy(self.task)

    def freeze(self, request=None, **kwargs):
        record = self.engine.prepare(
            self.task,
            {"requested": {"focus": intent() if request is None else request}},
            **kwargs,
        )
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO task_focus VALUES(?,?)",
                (self.task["task_id"], canonical_json(record)),
            )
        return record

    def context_candidate(self, *, pointer="/rules/0/text", text=None, **changes):
        text = self.snapshot["context"]["rules"][0]["text"] if text is None else text
        data = {
            "id": "candidate",
            "kind": "context",
            "text": text,
            "source": {
                "kind": "context",
                "id": self.task["project_context_id"],
                "sha256": self.snapshot["sha256"],
                "pointer": pointer,
                "unit": "value",
                "start": 0,
                "end": len(text),
            },
            **changes,
        }
        return FocusCandidate.model_validate(data)

    def artifact_candidate(self, text, *, owner=None, name="artifact"):
        info = self.artifacts.publish(
            self.task["conversation_id"], owner or self.task["task_id"], name, text
        )
        return FocusCandidate(
            id=name,
            kind="output",
            text=text,
            source={
                "kind": "artifact",
                "id": info["artifact_id"],
                "sha256": info["sha256"],
                "unit": "unicode_code_points",
                "start": 0,
                "end": len(text),
            },
        )

    def select(self, candidates=None, purpose="startup", **kwargs):
        return self.engine.select(
            self.task["task_id"],
            purpose,
            candidates if candidates is not None else [self.context_candidate()],
            **kwargs,
        )

    def test_explicit_consent_only_and_prepare_has_no_material_or_provider_effects(
        self,
    ):
        with patch.object(
            self.projects, "get", side_effect=AssertionError("material read")
        ):
            record = self.engine.prepare(self.task, {"effective": {"focus": intent()}})
            self.assertEqual(record["reason"], "missing_input")
            self.freeze(intent(allow_external=False))
            result = self.select()
            self.assertEqual(result["reason"], "request_export_not_allowed")
            self.assertEqual(self.engine.summary(self.task["task_id"])["reserved"], 0)
        self.assertEqual(self.sent, [])
        self.assertIsNone(
            FocusEngine(self.tasks, self.projects, self.artifacts).prepare(
                self.task, {}
            )
        )

    def test_cached_decisions_recheck_stage_source_consent_and_cancellation(self):
        first = self.select()
        self.assertEqual(first["labels"], {"candidate": "relevant"})
        cached = self.select()
        self.assertTrue(cached["cached"])
        self.assertEqual(len(self.sent), 1)
        self.task["review_stage"] = "comparison"
        with patch.object(
            self.projects, "get", side_effect=AssertionError("stage leaked body")
        ):
            self.assertEqual(self.select()["reason"], "snapshot_review")
        self.task.pop("review_stage")
        self.engine.work_items = SimpleNamespace(
            native_attempt=lambda _: {"kind": "work"}, stage_attempt=lambda _: None
        )
        self.assertEqual(self.select()["reason"], "managed_attempt")
        self.engine.work_items = None
        original = self.context_candidate()
        self.snapshot["context"]["rules"][0]["text"] = "Changed after cache"
        self.snapshot["sha256"] = digest(self.snapshot["context"])
        with self.assertRaises(ValueError):
            self.select([original])
        self.engine.policy = policy(allow_external=False)
        self.assertEqual(self.select()["reason"], "disabled")
        self.task["cancel_requested"] = True
        with self.assertRaises(FocusInterrupted):
            self.select()
        self.assertEqual(len(self.sent), 1)

    def test_exact_whole_values_required_data_and_foreign_artifacts_never_export(self):
        with self.assertRaises(ValueError):
            self.select([self.context_candidate(text="Investigate")])
        candidate = self.context_candidate()
        candidate.source["id"] = str(uuid4())
        with self.assertRaises(ValueError):
            self.select([candidate])
        self.snapshot["context"]["rules"][0]["requirement"] = "required"
        self.snapshot["sha256"] = digest(self.snapshot["context"])
        self.assertEqual(self.select()["reason"], "no_eligible_candidates")
        foreign = self.artifact_candidate(
            "Private unrelated output", owner=str(uuid4())
        )
        with self.assertRaises(ValueError):
            self.select([foreign], "artifact_reader")
        self.assertEqual(self.sent, [])
        self.task["contract_json"] = canonical_json(
            {"artifact_ids": [foreign.source["id"]]}
        )
        self.assertEqual(
            self.select([foreign], "artifact_reader")["status"], "completed"
        )
        self.task["contract_json"] = "{}"
        with self.assertRaises(ValueError):
            self.select([foreign], "artifact_reader")
        self.assertEqual(len(self.sent), 1)

    def test_context_utf8_output_requires_outputs_surface_and_exact_bytes(self):
        value = self.snapshot["context"]["rules"][0]["text"]
        encoded = canonical_json(value).encode()
        candidate = FocusCandidate(
            id="window",
            kind="output",
            text=encoded.decode(),
            source={
                "kind": "context",
                "id": self.snapshot["context_id"],
                "sha256": self.snapshot["sha256"],
                "pointer": "/rules/0/text",
                "unit": "utf8_bytes",
                "start": 0,
                "end": len(encoded),
            },
        )
        self.freeze(intent(surfaces=["context"]))
        self.assertEqual(
            self.select([candidate], "context_reader")["reason"],
            "no_eligible_candidates",
        )
        self.freeze(intent(surfaces=["outputs"]))
        self.assertEqual(
            self.select([candidate], "context_reader")["labels"], {"window": "relevant"}
        )

    def test_payload_is_bounded_whole_candidates_and_private_provenance_stays_local(
        self,
    ):
        self.engine.policy = policy(max_candidates=2)
        self.freeze()
        huge = self.artifact_candidate("parser failure " * 2400, name="oversized")
        useful = self.artifact_candidate("Investigate parser failure", name="useful")
        other = self.artifact_candidate("unrelated background", name="other")
        protected = self.artifact_candidate(
            "fatal diagnostic must remain local", name="protected"
        )
        protected.protected = True
        result = self.select([huge, other, protected, useful], "artifact_reader")
        self.assertEqual(result["assessed_ids"], ["useful", "other"])
        self.assertEqual(result["unassessed_ids"], ["oversized", "protected"])
        sent = canonical_json(self.sent[0])
        self.assertLessEqual(len(sent.encode()), 32000)
        self.assertIn(useful.text, sent)
        for candidate in (huge, useful, other, protected):
            self.assertNotIn(candidate.source["id"], sent)
            self.assertNotIn(candidate.source["sha256"], sent)
        self.assertNotIn(protected.text, sent)
        self.assertNotIn(self.task["task_id"], sent)

    def test_quota_is_atomic_across_distinct_concurrent_requests_and_cache_is_free(
        self,
    ):
        self.engine.policy = policy(max_requests=1)
        self.freeze()
        candidates = [
            self.artifact_candidate(f"parser failure {index}", name=f"item{index}")
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda item: self.select([item], "artifact_reader"), candidates
                )
            )
        self.assertEqual(sum(item["status"] == "completed" for item in results), 1)
        self.assertEqual(
            sum(item["reason"] == "quota_exhausted" for item in results), 7
        )
        winner = next(
            candidate
            for candidate, result in zip(candidates, results, strict=True)
            if result["status"] == "completed"
        )
        self.assertTrue(self.select([winner], "artifact_reader")["cached"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.engine.summary(self.task["task_id"])["reserved"], 1)

    def test_concurrent_duplicate_and_restart_never_replay_unresolved_post(self):
        entered, release = threading.Event(), threading.Event()

        async def blocked(client, payload):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return response(payload), None

        with (
            patch.object(JevClient, "_request", blocked),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending = pool.submit(self.select)
            try:
                self.assertTrue(entered.wait(2))
                other = FocusEngine(
                    self.tasks,
                    self.projects,
                    self.artifacts,
                    policy=policy(),
                    config=JevConfig(api_key="synthetic-key"),
                )
                duplicate = other.select(
                    self.task["task_id"], "startup", [self.context_candidate()]
                )
                self.assertEqual(duplicate["reason"], "unresolved")
                self.assertEqual(other.summary(self.task["task_id"])["reserved"], 1)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=2)["status"], "completed")
        self.assertTrue(self.select()["cached"])

    def test_failed_transport_is_terminal_and_unchanged_retry_does_not_send(self):
        async def failed(client, payload):
            self.sent.append(json.loads(payload))
            return None, "timeout"

        with patch.object(JevClient, "_request", failed):
            first = self.select()
            second = self.select()
        self.assertEqual(first["reason"], "timeout")
        self.assertEqual(first["labels"], {})
        self.assertEqual(first["unassessed_ids"], ["candidate"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.sent), 1)

    def test_missing_key_and_protected_only_are_no_export_fallbacks(self):
        self.engine.config = JevConfig()
        with patch.object(
            self.projects, "get", side_effect=AssertionError("keyless material read")
        ):
            self.assertEqual(
                self.engine.configuration(self.task["task_id"])["reason"],
                "missing_api_key",
            )
            self.assertEqual(self.select()["reason"], "missing_api_key")
        self.engine.config = JevConfig(api_key="synthetic-key")
        result = self.select([self.context_candidate(protected=True)])
        self.assertEqual(result["unassessed_ids"], ["candidate"])
        self.assertEqual(result["labels"], {})
        self.assertEqual(self.engine.summary(self.task["task_id"])["reserved"], 0)
        self.assertEqual(self.sent, [])

    def test_unknown_provider_choice_is_not_an_empty_success(self):
        async def malformed(client, payload):
            value = response(payload)
            value["answers"]["item_0"]["choice"] = "invented"
            return value, None

        with patch.object(JevClient, "_request", malformed):
            result = self.select()
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["reason"], "malformed_response")
        self.assertEqual(result["unassessed_ids"], ["candidate"])
        self.assertEqual(result["labels"], {})

    def test_source_stage_changes_after_provider_do_not_apply_the_decision(self):
        async def stage_changed(client, payload):
            self.task["review_id"] = str(uuid4())
            return response(payload), None

        with patch.object(JevClient, "_request", stage_changed):
            result = self.select()
        self.assertEqual(result["reason"], "snapshot_review")
        self.assertEqual(result["labels"], {})
        self.assertEqual(result["assessed_ids"], [])

    def test_cancel_inflight_closes_http_and_leaves_nonreplayable_reservation(self):
        cancel, entered, stopped = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        async def blocked(client, payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with (
            patch.object(JevClient, "_request", blocked),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(self.select, cancelled=cancel.is_set)
            self.assertTrue(entered.wait(2))
            cancel.set()
            with self.assertRaises(FocusInterrupted):
                future.result(timeout=2)
        self.assertTrue(stopped.is_set())
        summary = self.engine.summary(self.task["task_id"])
        self.assertEqual((summary["reserved"], summary["unresolved"]), (1, 1))
        self.assertEqual(self.select()["reason"], "unresolved")
        self.assertEqual(self.sent, [])

    def test_deadline_and_post_response_cancellation_never_continue_raw(self):
        async def cancelled_after_post(client, payload):
            self.task["cancel_requested"] = True
            return response(payload), None

        with (
            patch.object(JevClient, "_request", cancelled_after_post),
            self.assertRaises(FocusInterrupted),
        ):
            self.select()
        self.assertEqual(self.engine.summary(self.task["task_id"])["unresolved"], 1)
        self.task["cancel_requested"] = False
        self.task["deadline"] = time.time() - 1
        with self.assertRaises(FocusInterrupted):
            self.select()

    def test_surface_revocation_blocks_cached_decision_and_full_manifest_changes_identity(
        self,
    ):
        first = self.select()
        self.freeze(intent(surfaces=["tools"]))
        self.assertEqual(self.select()["reason"], "no_eligible_candidates")
        self.freeze()
        candidate = self.context_candidate()
        protected = self.context_candidate(id="protected", protected=True)
        changed = self.select([candidate, protected])
        self.assertNotEqual(first["input_sha256"], changed["input_sha256"])
        self.assertEqual(changed["unassessed_ids"], ["protected"])
        self.assertEqual(len(self.sent), 2)

    def test_safe_startup_reporting_is_immutable_without_changing_frozen_intent(self):
        before = self.engine.configuration(self.task["task_id"])
        report = {
            "status": "fallback",
            "reason": "message_budget",
            "original_message_bytes": 190000,
            "rendered_message_bytes": 190000,
            "added_optional_bytes": 0,
            "tool_presentation": {"mode": "essential", "eager": 5, "deferred": 0},
        }
        self.engine.record_startup(self.task["task_id"], report)
        self.engine.record_startup(self.task["task_id"], report)
        with self.assertRaises(ValueError):
            self.engine.record_startup(
                self.task["task_id"], {**report, "status": "completed"}
            )
        with self.assertRaises(ValueError):
            self.engine.record_startup(
                self.task["task_id"], {**report, "body": "private text"}
            )
        self.assertEqual(self.engine.configuration(self.task["task_id"]), before)
        summary = self.engine.summary(self.task["task_id"])
        self.assertEqual(summary["startup"]["reason"], "message_budget")
        self.assertEqual(summary["reserved"], 0)
        self.assertNotIn("policy", summary)
        self.assertNotIn("input", summary)
