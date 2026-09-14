"""Read-only admission and explicit, capability-free conversation rotation."""

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omp_tandem.bridge import Bridge
from tests.helpers import make_peer
from tests.test_execution import PEER


class ConversationPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        peer = make_peer(self.base)
        (self.base / "peer.py").write_text(PEER)
        self.bridge = Bridge(
            self.base / "state", str(peer), "local/peer", project_root=self.root
        )
        self.addCleanup(self.bridge.shutdown)
        self.runtime = self.bridge.runtime

    def wait(self, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.bridge.view(task_id)
            if result["status"] in {"completed", "failed", "cancelled"}:
                # Teardown must release the conversation lease before continuing.
                thread = self.runtime.threads.get(task_id)
                if thread is not None:
                    thread.join(timeout=3)
                return result
            time.sleep(0.02)
        self.fail("Local native peer did not finish")

    def database(self):
        with sqlite3.connect(self.runtime.tasks.path) as db:
            return tuple(db.iterdump())

    def git(self, root, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )

    def repository(self, root):
        self.git(root, "init", "-q")
        (root / "entry.py").write_text("print('entry')\n")
        self.git(root, "add", "entry.py")
        self.git(root, "commit", "-qm", "fixture")

    def test_preflight_has_no_database_worker_slot_or_lock_side_effects(self):
        before = self.database()
        files = set(self.runtime.tasks.root.rglob("*"))
        with (
            patch.object(
                self.runtime.worker, "execute", side_effect=AssertionError("worker")
            ),
            patch.object(
                self.runtime.slots, "acquire", side_effect=AssertionError("slot")
            ),
            patch.object(
                self.runtime.tasks, "lock", side_effect=AssertionError("lease")
            ),
            patch.object(
                self.runtime.tasks, "recover", side_effect=AssertionError("recovery")
            ),
        ):
            result = self.runtime.preflight(prompt="Inspect only", mode="analyze")
        self.assertTrue(result["admissible"])
        self.assertFalse(result["reservation"])
        self.assertEqual(self.database(), before)
        self.assertEqual(set(self.runtime.tasks.root.rglob("*")), files)

    def test_analyze_shell_and_whole_budget_mismatches_refuse_before_launch(self):
        before = self.database()
        with patch.object(
            self.runtime.slots, "acquire", side_effect=AssertionError("slot")
        ):
            for operation in (self.runtime.preflight, self.runtime.start):
                with self.assertRaisesRegex(ValueError, "shell"):
                    operation(
                        contract={
                            "goal": "Run check",
                            "requirements": {"requires_shell": True},
                        },
                        mode="analyze",
                    )
                with self.assertRaisesRegex(ValueError, "shell"):
                    operation(
                        contract={
                            "goal": "Run check",
                            "verification": {
                                "checks": [
                                    {
                                        "id": "unit",
                                        "criterion": "Units pass",
                                        "command": "pytest",
                                        "estimated_seconds": 2,
                                    }
                                ]
                            },
                        },
                        mode="analyze",
                    )
                with self.assertRaisesRegex(ValueError, "whole task timeout"):
                    operation(
                        contract={
                            "goal": "Check",
                            "verification": {
                                "preparation_seconds": 4,
                                "checks": [
                                    {
                                        "id": "review",
                                        "criterion": "Reviewed",
                                        "estimated_seconds": 7,
                                    }
                                ],
                            },
                        },
                        timeout_seconds=10,
                    )
        self.assertEqual(self.database(), before)

    def test_declared_files_use_allowed_git_roots_and_reject_nested_repository(self):
        self.repository(self.root)
        contract = {
            "goal": "Inspect",
            "requirements": {
                "entry_paths": ["entry.py"],
                "boundary_paths": ["future.py"],
            },
        }
        result = self.runtime.preflight(contract=contract)
        self.assertEqual(
            result["repository_observations"][0]["git_toplevel"],
            str(self.root.resolve()),
        )
        self.assertEqual(
            result["live_path_evidence"]["status"],
            "attributed_evidence_not_execution_proof",
        )
        child = self.root / "nested"
        child.mkdir()
        self.repository(child)
        with self.assertRaisesRegex(ValueError, "repository boundary"):
            self.runtime.preflight(
                contract={
                    "goal": "Inspect",
                    "requirements": {"entry_paths": ["nested/entry.py"]},
                }
            )
        granted = self.base / "granted"
        granted.mkdir()
        self.repository(granted)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.runtime.preflight(cwd=str(granted), contract=contract)
        allowed = self.runtime.preflight(
            cwd=str(granted), granted_roots=(granted,), contract=contract
        )
        self.assertEqual(
            allowed["repository_observations"][0]["git_toplevel"],
            str(granted.resolve()),
        )

    def test_fresh_argv_history_goal_policy_and_computation_are_preserved(self):
        first = self.runtime.start(
            contract={"goal": "Original", "constraints": ["Do not publish"]},
            mode="think",
            execution={"profile": "quick"},
            timeout_seconds=5,
        )
        self.assertEqual(self.wait(first["task_id"])["status"], "completed")
        prior = self.runtime.tasks.get(first["task_id"], refresh=False)
        session = Path(prior["session_file"])
        history = session.read_bytes()
        handoff = {
            "reason": "New context window",
            "summary": "Original answer is recorded",
            "remaining_goals": ["Retain requested current goal"],
        }
        fresh = self.runtime.start(
            prompt="Requested current goal",
            conversation_id=first["conversation_id"],
            continuation="fresh",
            handoff=handoff,
        )
        result = self.wait(fresh["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("--resume", json.loads(result["answer"]))
        self.assertNotEqual(fresh["conversation_id"], first["conversation_id"])
        task = self.runtime.tasks.get(fresh["task_id"], refresh=False)
        self.assertEqual(task["prompt"], "Requested current goal")
        self.assertEqual(task["policy_json"], prior["policy_json"])
        self.assertEqual(task["mode"], prior["mode"])
        self.assertEqual(
            json.loads(task["execution_json"])["effective"],
            json.loads(prior["execution_json"])["effective"],
        )
        self.assertEqual(task["previous_task_id"], first["task_id"])
        self.assertEqual(
            json.loads(task["handoff_json"]),
            handoff | {"evidence_artifact_ids": [], "invalidated_assumptions": []},
        )
        self.assertEqual(self.runtime.tasks.get(first["task_id"], refresh=False), prior)
        self.assertEqual(session.read_bytes(), history)
        resumed = self.runtime.start(
            prompt="Continue current goal", conversation_id=fresh["conversation_id"]
        )
        self.assertIn("--resume", json.loads(self.wait(resumed["task_id"])["answer"]))
        self.assertEqual(resumed["conversation_id"], fresh["conversation_id"])

    def test_fresh_refuses_foreign_evidence_bound_capabilities_and_uncertain_work(self):
        first = self.runtime.start(prompt="First", mode="think", timeout_seconds=5)
        self.wait(first["task_id"])
        foreign = self.runtime.start(prompt="Other", mode="think", timeout_seconds=5)
        self.wait(foreign["task_id"])
        evidence = self.runtime.artifacts.publish(
            foreign["conversation_id"],
            foreign["task_id"],
            "foreign",
            "Not source evidence",
        )
        handoff = {"reason": "Fresh", "summary": "Safe summary"}
        kwargs = {
            "prompt": "Current goal",
            "conversation_id": first["conversation_id"],
            "continuation": "fresh",
            "handoff": handoff,
        }
        before = self.database()
        with self.assertRaisesRegex(ValueError, "source conversation"):
            self.runtime.start(
                **(
                    kwargs
                    | {
                        "handoff": handoff
                        | {"evidence_artifact_ids": [evidence["artifact_id"]]}
                    }
                )
            )
        with (
            patch.object(
                self.runtime.worker.work_items,
                "native_attempt",
                return_value={"token": "not-transferable"},
            ),
            self.assertRaisesRegex(ValueError, "Bound managed"),
        ):
            self.runtime.start(**kwargs)
        with (
            patch.object(
                self.runtime.worker.work_items,
                "active_attempts",
                return_value=[
                    {
                        "state": "recovery_required",
                        "binding": {"task_id": first["task_id"]},
                    }
                ],
            ),
            self.assertRaisesRegex(ValueError, "recovery-required"),
        ):
            self.runtime.start(**kwargs)
        self.assertEqual(self.database(), before)
        self.runtime.tasks.update(first["task_id"], mode="work", status="interrupted")
        interrupted = self.database()
        with self.assertRaisesRegex(ValueError, "uncertain effects"):
            self.runtime.start(**kwargs)
        self.assertEqual(self.database(), interrupted)

    def test_provider_policy_stop_cannot_be_hidden_by_fresh(self):
        job = self.runtime.start(prompt="First", mode="think", timeout_seconds=5)
        self.wait(job["task_id"])
        task = self.runtime.tasks.get(job["task_id"], refresh=False)
        settings = json.loads(task["execution_json"])
        settings["stop"] = {"classification": "provider_policy_refusal"}
        self.runtime.tasks.update(
            job["task_id"], status="failed", execution_json=json.dumps(settings)
        )
        before = self.database()
        with self.assertRaisesRegex(ValueError, "Provider policy"):
            self.runtime.start(
                prompt="Same work",
                conversation_id=job["conversation_id"],
                continuation="fresh",
                handoff={"reason": "Rotate", "summary": "Refused"},
            )
        self.assertEqual(self.database(), before)

    def test_preflight_never_recovers_or_expires_an_active_source(self):
        job = self.runtime.start(prompt="First", mode="think", timeout_seconds=5)
        self.wait(job["task_id"])
        self.runtime.tasks.update(job["task_id"], status="waiting_input")
        before = self.database()
        with self.assertRaisesRegex(ValueError, "active or unrecovered"):
            self.runtime.preflight(
                prompt="Current goal",
                conversation_id=job["conversation_id"],
                continuation="fresh",
                handoff={"reason": "Rotate", "summary": "Waiting"},
            )
        self.assertEqual(self.database(), before)

    def test_native_context_reader_pins_snapshot_and_guards_managed_review(self):
        from omp_tandem.project_context import ContextReadRequest

        first = self.runtime.projects.publish(
            {"project_id": "product", "product_summary": "Pinned description"}
        )
        job = self.runtime.start(
            prompt="Read context",
            mode="think",
            project_context_id=first["context_id"],
            timeout_seconds=5,
        )
        self.wait(job["task_id"])
        self.runtime.projects.publish(
            {"project_id": "product", "product_summary": "Later description"},
            expected_revision=1,
        )
        request = ContextReadRequest(pointer="/product_summary")
        page = self.runtime.worker._read_context(job["task_id"], request)
        self.assertEqual(json.loads(page["content"]), "Pinned description")
        self.assertEqual(page["context_id"], first["context_id"])
        with (
            patch.object(
                self.runtime.worker.work_items,
                "stage_attempt",
                return_value={
                    "kind": "review",
                    "protocol": "independent_first",
                    "comparison_opened_at": None,
                },
            ),
            self.assertRaisesRegex(ValueError, "independent review"),
        ):
            self.runtime.worker._read_context(job["task_id"], request)
        with patch.object(
            self.runtime.worker.work_items,
            "stage_attempt",
            return_value={
                "kind": "review",
                "protocol": "independent_first",
                "comparison_opened_at": 1,
            },
        ):
            self.assertEqual(
                json.loads(
                    self.runtime.worker._read_context(job["task_id"], request)[
                        "content"
                    ]
                ),
                "Pinned description",
            )

    def test_managed_prompt_cannot_omit_reserved_requirements_or_verification(self):
        attempt = {
            "token": "reserved",
            "actor": "omp",
            "kind": "implement",
            "allow_work": True,
            "allow_shell": False,
            "requirements": {"requires_shell": True},
        }
        before = self.database()
        with (
            patch.object(
                self.runtime.worker.work_items, "attempt", return_value=attempt
            ),
            patch.object(
                self.runtime.worker.work_items, "authenticate", return_value=attempt
            ),
            patch.object(
                self.runtime.slots, "acquire", side_effect=AssertionError("slot")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "shell"):
                self.runtime.start(
                    prompt="No shell mentioned", mode="work", work_attempt_id="reserved"
                )
            attempt["requirements"] = {}
            attempt["verification"] = {
                "checks": [
                    {"id": "inspect", "criterion": "Reviewed", "estimated_seconds": 20}
                ],
            }
            with self.assertRaisesRegex(ValueError, "whole task timeout"):
                self.runtime.start(
                    prompt="No check mentioned",
                    mode="work",
                    timeout_seconds=10,
                    work_attempt_id="reserved",
                )
        self.assertEqual(self.database(), before)
