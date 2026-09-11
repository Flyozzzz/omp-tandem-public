"""No provider calls: real child-process boundaries and native failure handling."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from omp_tandem.work_adapters import ClaudeWorkAdapter, OmpWorkAdapter


class AttemptStore:
    def __init__(self, attempt):
        self.value = attempt

    def authenticate(self, token):
        if token != "private-attempt-secret":
            raise ValueError("Invalid token")
        return dict(self.value)

    def attempt(self, identifier):
        if identifier != self.value["attempt_id"]:
            raise ValueError("Wrong attempt")
        return dict(self.value)

    def started(self, identifier, **fields):
        self.attempt(identifier)
        self.value.update(fields, state="running", started_at=time.time())


class WorkAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        identifier = str(uuid4())
        self.workspace = {
            "path": str(self.state / "worktrees" / identifier),
            "base_commit": "saved",
        }
        Path(self.workspace["path"]).mkdir(parents=True)
        self.token = self.state / "token"
        self.token.write_text("private-attempt-secret\n")
        self.token.chmod(0o600)
        self.attempt = {
            "attempt_id": identifier,
            "work_id": str(uuid4()),
            "step_id": "change",
            "kind": "implement",
            "actor": "claude",
            "plan_revision": 1,
            "state": "reserved",
            "started_at": None,
            "heartbeat_at": None,
            "deadline": time.time() + 30,
            "remaining_cost_usd": 1.0,
            "allow_work": True,
            "allow_tests": False,
            "claude_model": "claude-opus-4-6",
            "omp_model": "provider/authorized-model",
            "model_provenance": {"claude": "explicit", "omp": "explicit"},
        }
        self.store = AttemptStore(self.attempt)
        self.plan = {
            "title": "Change",
            "goal": "Saved goal",
            "constraints": [],
            "acceptance": ["Saved behavior"],
            "steps": [
                {
                    "id": "change",
                    "goal": "Saved behavior",
                    "owner": "claude",
                    "reviewer": "omp",
                    "owned_files": ["code.py"],
                    "acceptance": ["Saved behavior"],
                    "depends_on": [],
                }
            ],
        }
        self.bridge = SimpleNamespace(
            scope=SimpleNamespace(
                directory=self.state, root=self.root, base=self.state
            ),
            work_items=self.store,
        )

    def adapter(self, body):
        executable = self.root / "fixture-cli"
        executable.write_text(
            f"#!{sys.executable}\nimport json, os, signal, sys, time\n" + body
        )
        executable.chmod(0o700)
        adapter = ClaudeWorkAdapter(self.bridge, str(executable))
        adapter.stop_seconds = 2
        self.addCleanup(adapter.close)
        return adapter

    def launch(self, adapter, *, ack=True):
        handle = adapter.start(
            self.attempt, self.plan, self.workspace, token_file=self.token
        )
        if ack:
            self.attempt["heartbeat_at"] = time.time()
        return handle

    def finish(self, adapter, handle):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            result = adapter.poll(handle)
            if result is not None:
                return result
            time.sleep(0.01)
        self.fail("Fixture process did not finish")

    def result_program(self, change=""):
        return (
            "session = sys.argv[sys.argv.index('--session-id') + 1]\n"
            "result = {'type':'result','subtype':'success','is_error':False,'session_id':session,"
            "'total_cost_usd':0.1,'usage':{'input_tokens':4,'output_tokens':8},"
            "'structured_output':{'outcome':'success','answer':'Checked saved output',"
            "'evidence':['saved check passed']}}\n"
            + change
            + "\nprint(json.dumps(result), flush=True)\n"
        )

    def test_success_requires_structured_outcome_and_saved_private_context(self):
        adapter = self.adapter(self.result_program())
        handle = self.launch(adapter)
        result = self.finish(adapter, handle)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["cost_usd"], 0.1)
        self.assertEqual(result["evidence"], ["saved check passed"])
        self.assertEqual(handle.process.poll(), 0)
        self.assertFalse(handle.reader.is_alive())
        for path in handle.directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertNotIn(
            "private-attempt-secret", (handle.directory / "context.txt").read_text()
        )

    def test_claude_launch_uses_trusted_selection_not_supplied_attempt(self):
        adapter = self.adapter(
            self.result_program(
                "result['structured_output']['answer'] = sys.argv[sys.argv.index('--model') + 1]"
            )
        )
        supplied = {**self.attempt, "claude_model": "untrusted-model"}
        handle = adapter.start(
            supplied, self.plan, self.workspace, token_file=self.token
        )
        self.attempt["heartbeat_at"] = time.time()
        result = self.finish(adapter, handle)
        self.assertEqual(result["answer"], "claude-opus-4-6")
        selection = json.loads((handle.directory / "launch.json").read_text())[
            "model_selection"
        ]
        self.assertEqual(selection["model_provenance"]["claude"], "explicit")

    def test_legacy_claude_launch_labels_historical_default(self):
        for key in ("claude_model", "omp_model", "model_provenance"):
            self.attempt.pop(key)
        adapter = self.adapter(
            self.result_program(
                "result['structured_output']['answer'] = sys.argv[sys.argv.index('--model') + 1]"
            )
        )
        handle = self.launch(adapter)
        self.assertEqual(self.finish(adapter, handle)["answer"], "sonnet")
        selection = json.loads((handle.directory / "launch.json").read_text())[
            "model_selection"
        ]
        self.assertEqual(selection["model_provenance"]["claude"], "legacy_default")
        self.assertNotIn("claude_model", self.attempt)

    def test_incomplete_or_invalid_selection_never_launches(self):
        adapter = self.adapter(self.result_program())
        original = dict(self.attempt)
        for value in (None, "", "  ", "--model", "opus\nsonnet"):
            with self.subTest(value=value):
                self.attempt.update(original, claude_model=value)
                with self.assertRaises(ValueError):
                    self.launch(adapter)
        self.attempt.update(original)
        self.attempt.pop("claude_model")
        with self.assertRaises(ValueError):
            self.launch(adapter)
        self.assertFalse((self.state / "work-adapters").exists())

    def test_legacy_omp_or_conflicting_process_override_never_launches(self):
        self.attempt.update(actor="omp", omp_model=None)
        self.attempt["model_provenance"]["omp"] = "default"
        self.bridge.runtime = SimpleNamespace(model="process-override")
        adapter = OmpWorkAdapter(self.bridge)
        self.addCleanup(adapter.close)
        with self.assertRaisesRegex(ValueError, "Process-wide"):
            self.launch(adapter)
        for key in ("claude_model", "omp_model", "model_provenance"):
            self.attempt.pop(key)
        with self.assertRaisesRegex(ValueError, "reauthorize"):
            self.launch(adapter)
        self.assertFalse((self.state / "work-adapters").exists())

    def test_exit_zero_with_confident_prose_is_failure(self):
        adapter = self.adapter("print('Everything is done and approved')\n")
        result = self.finish(adapter, self.launch(adapter))
        self.assertEqual(result["outcome"], "failed")
        self.assertIsNone(result["cost_usd"])

    def test_provider_budget_error_is_not_success(self):
        adapter = self.adapter(
            self.result_program(
                "result.update(subtype='error_max_budget_usd', is_error=True)"
            )
        )
        result = self.finish(adapter, self.launch(adapter))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["cost_usd"], 0.1)

    def test_missing_cost_never_becomes_zero(self):
        adapter = self.adapter(self.result_program("result.pop('total_cost_usd')"))
        result = self.finish(adapter, self.launch(adapter))
        self.assertEqual(result["outcome"], "failed")
        self.assertIsNone(result["cost_usd"])

    def test_result_from_another_session_cannot_complete_attempt(self):
        adapter = self.adapter(
            self.result_program("result['session_id'] = 'other-session'")
        )
        result = self.finish(adapter, self.launch(adapter))
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("identity", result["error"])

    def test_success_without_shared_heartbeat_is_failure(self):
        adapter = self.adapter(self.result_program())
        result = self.finish(adapter, self.launch(adapter, ack=False))
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("acknowledgement", result["error"])

    def test_missing_heartbeat_stops_still_running_child(self):
        adapter = self.adapter("time.sleep(60)\n")
        adapter.startup_seconds = 0
        handle = self.launch(adapter, ack=False)
        result = self.finish(adapter, handle)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertIsNotNone(handle.process.poll())
        self.assertFalse(handle.reader.is_alive())

    def test_cancel_reaps_process_and_preserves_partial_output(self):
        adapter = self.adapter("print('partial work', flush=True)\ntime.sleep(60)\n")
        handle = self.launch(adapter)
        adapter.cancel(handle)
        result = adapter.poll(handle)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertIsNotNone(handle.process.poll())
        self.assertFalse(handle.reader.is_alive())
        adapter.close()  # Repeated cleanup must not signal a reused process group.

    def test_output_flood_is_bounded_and_child_stopped(self):
        adapter = self.adapter("while True:\n os.write(1, b'x' * 65536)\n")
        adapter.max_output_bytes = 4096
        handle = self.launch(adapter)
        result = self.finish(adapter, handle)
        self.assertNotEqual(result["outcome"], "success")
        self.assertLessEqual(
            sum(
                (handle.directory / name).stat().st_size
                for name in ("stdout.jsonl", "stderr.txt")
            ),
            4096,
        )
        self.assertIsNotNone(handle.process.poll())

    def test_lost_launch_acknowledgement_never_replays_attempt(self):
        adapter = self.adapter(self.result_program())
        with (
            patch(
                "omp_tandem.work_adapters.subprocess.Popen",
                side_effect=OSError("launch uncertain"),
            ),
            self.assertRaises(OSError),
        ):
            self.launch(adapter)
        with self.assertRaises(ValueError):
            self.launch(adapter)

    def test_expired_or_unknown_budget_never_launches(self):
        adapter = self.adapter(self.result_program())
        self.attempt["remaining_cost_usd"] = None
        with self.assertRaises(ValueError):
            self.launch(adapter)
        self.assertFalse((self.state / "work-adapters").exists())

    def test_reviewer_cannot_receive_write_or_ungranted_shell_tools(self):
        self.attempt.update(kind="review", allow_work=True, allow_tests=False)
        body = (
            "tools = sys.argv[sys.argv.index('--tools') + 1].split(',')\n"
            "if any(tool in tools for tool in ('Edit','Write','Bash')):\n"
            " open('forbidden-edit', 'w').write('unsafe')\n"
        ) + self.result_program()
        adapter = self.adapter(body)
        result = self.finish(adapter, self.launch(adapter))
        self.assertEqual(result["outcome"], "success")
        self.assertFalse((Path(self.workspace["path"]) / "forbidden-edit").exists())

    def test_native_unknown_inflight_cost_stops_instead_of_spending_more(self):
        self.attempt["actor"] = "omp"
        cancelled = []
        self.bridge.runtime = SimpleNamespace(guard=threading.Lock(), threads={})
        self.bridge.cancel = cancelled.append

        def start(**kwargs):
            self.store.started(
                self.attempt["attempt_id"],
                native_task_id="native-task",
                workspace=self.workspace["path"],
            )
            self.attempt["heartbeat_at"] = time.time()
            return {"task_id": "native-task", "conversation_id": "native-session"}

        self.bridge.start = start
        self.bridge.view = lambda *args, **kwargs: {
            "status": "running",
            "usage": {
                "task": {
                    "response_count": 1,
                    "cost": {
                        "value": None,
                        "known_subtotal": None,
                        "status": "unknown",
                    },
                }
            },
        }
        adapter = OmpWorkAdapter(self.bridge)
        self.addCleanup(adapter.close)
        handle = self.launch(adapter)
        result = adapter.poll(handle)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertIsNone(result["cost_usd"])
        self.assertIn("native-task", cancelled)

    def test_interrupted_priced_response_is_not_complete_attempt_cost(self):
        self.attempt["actor"] = "omp"
        self.bridge.runtime = SimpleNamespace(guard=threading.Lock(), threads={})
        stopped = []
        self.bridge.cancel = stopped.append

        def start(**kwargs):
            self.store.started(
                self.attempt["attempt_id"],
                native_task_id="priced-task",
                workspace=self.workspace["path"],
            )
            self.attempt["heartbeat_at"] = time.time()
            return {"task_id": "priced-task", "conversation_id": "priced-session"}

        self.bridge.start = start
        self.bridge.view = lambda *args, **kwargs: {
            "status": "cancelled" if stopped else "running",
            "usage": {
                "task": {
                    "response_count": 1,
                    "coverage": "partial",
                    "cost": {"value": 100, "known_subtotal": 100, "status": "complete"},
                }
            },
        }
        adapter = OmpWorkAdapter(self.bridge)
        self.addCleanup(adapter.close)
        result = adapter.poll(self.launch(adapter))
        self.assertEqual(result["outcome"], "interrupted")
        self.assertIsNone(result["cost_usd"])

    def test_native_launch_error_after_binding_cancels_bound_task(self):
        self.attempt["actor"] = "omp"
        cancelled = []
        self.bridge.runtime = SimpleNamespace(guard=threading.Lock(), threads={})
        self.bridge.cancel = cancelled.append

        def start(**kwargs):
            self.store.started(
                self.attempt["attempt_id"],
                native_task_id="lost-task",
                workspace=self.workspace["path"],
            )
            raise RuntimeError("Lost start acknowledgement")

        self.bridge.start = start
        adapter = OmpWorkAdapter(self.bridge)
        self.addCleanup(adapter.close)
        with self.assertRaises(RuntimeError):
            self.launch(adapter)
        self.assertEqual(cancelled, ["lost-task"])
        with self.assertRaises(ValueError):
            self.launch(adapter)


if __name__ == "__main__":
    unittest.main()
