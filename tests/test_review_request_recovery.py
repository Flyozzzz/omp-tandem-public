"""Request transport, the recovery a partial result names, and the repeated-gap guard."""

import asyncio
import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from omp_tandem import __file__ as package_file
from omp_tandem.blockers import blocked_question, extract_request, recovery
from omp_tandem.presentation import MAX_RESPONSE_BYTES, wire_size
from omp_tandem.review_runs import RUN_ACTIVE
from tests.helpers import RpcHarness, patch_peer
from tests.test_review_runs import RUN_PEER

# A reviewer that names exactly what it is missing, then reports partial. This is
# the measured shape: the stage ends with findings and with a request the caller
# never saw, because an independent stage cannot take a free-text answer.
RECOVERY_PEER = patch_peer(
    RUN_PEER,
    "        elif scenario == 'partial':",
    """        elif scenario == 'context-request':
            emit({'type': 'host_tool_call', 'id': 'question', 'toolCallId': 'question-call', 'toolName': 'tandem_ask',
                  'arguments': {'question': 'The authorization is not in this capture.',
                                'context': json.dumps({'requested_paths': ['specs/context.md', 'specs/plan.md'],
                                                       'requested_material': ['The complete plan text'],
                                                       'reason': 'Two conditional changes are present without a captured decision.'})}})
        elif scenario == 'malformed-context':
            emit({'type': 'host_tool_call', 'id': 'question', 'toolCallId': 'question-call', 'toolName': 'tandem_ask',
                  'arguments': {'question': 'What authorized this?', 'context': 'not json at all'}})
        elif scenario == 'partial':""",
)
RECOVERY_PEER = patch_peer(
    RECOVERY_PEER,
    "        if response.get('clarification_requires_new_snapshot'):",
    """        if response.get('clarification_requires_new_snapshot') and scenario in ('context-request', 'malformed-context'):
            finish({'outcome': 'partial', 'summary': 'Assessed what was present', 'answer': 'Two findings; scope approval unresolved.'})
        elif response.get('clarification_requires_new_snapshot'):""",
)


class RequestRecoveryTests(RpcHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.root / "peer.py").write_text(RECOVERY_PEER)
        (self.root / "scenario").write_text("snapshot-reader")
        (self.root / "sample.txt").write_text("saved bytes")
        self.runs = self.bridge.review_runs
        capture_command = self.runs._capture_command
        source = str(Path(package_file).parent.parent)

        def selected_capture(run):
            command = capture_command(run)
            code = (
                f"import sys; sys.path.insert(0, {source!r}); "
                "from omp_tandem.capture_worker import main; raise SystemExit(main())"
            )
            return [*command[:2], "-c", code, *command[4:]]

        source_patch = patch.object(
            self.runs, "_capture_command", side_effect=selected_capture
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def request(self, **values):
        return {
            "requirements": "Assess the saved sample",
            "paths": ["sample.txt"],
            **values,
        }

    def write_request(self, name="review-request.json", request=None):
        """Write the request the way an orchestrator's own code would."""
        body = json.dumps(request if request is not None else self.request())
        (self.root / name).write_text(body)
        return name, hashlib.sha256(body.encode()).hexdigest()

    def scenario(self, name):
        (self.root / "scenario").write_text(name)

    async def begin(self, **values):
        return await asyncio.to_thread(self.runs.start, str(uuid4()), **values)

    async def wait_run(self, run_id):
        async def wait():
            while True:
                state = await asyncio.to_thread(self.runs.state, run_id)
                if state not in RUN_ACTIVE:
                    return await asyncio.to_thread(self.runs.view, run_id)
                await asyncio.sleep(0.03)

        return await asyncio.wait_for(wait(), timeout=12)

    def payload(self, run_id):
        with self.bridge.tasks.connect() as db:
            return db.execute(
                "SELECT payload_json FROM review_runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]

    # Transport

    async def test_a_request_read_from_a_file_is_the_same_logical_request(self):
        name, digest = self.write_request()
        imported = await self.begin(request_file=name, request_sha256=digest)
        inline = await self.begin(request=self.request())
        self.assertEqual(
            self.payload(imported["run_id"]), self.payload(inline["run_id"])
        )
        self.assertEqual(
            imported["request_transport"],
            {
                "kind": "file",
                "path": name,
                "sha256": digest,
                "bytes": (self.root / name).stat().st_size,
            },
        )
        self.assertNotIn("request_transport", inline)

    async def test_an_accepted_reference_replays_after_its_file_is_gone(self):
        name, digest = self.write_request()
        key = str(uuid4())
        first = await asyncio.to_thread(
            self.runs.start, key, request_file=name, request_sha256=digest
        )
        os.remove(self.root / name)
        repeat = await asyncio.to_thread(
            self.runs.start, key, request_file=name, request_sha256=digest
        )
        self.assertEqual(repeat["run_id"], first["run_id"])

    async def test_a_digest_that_does_not_match_refuses_and_names_the_real_one(self):
        name, digest = self.write_request()
        (self.root / name).write_text(json.dumps(self.request(requirements="Other")))
        with self.assertRaises(ValueError) as refusal:
            await self.begin(request_file=name, request_sha256=digest)
        self.assertIn("digest does not match", str(refusal.exception))
        self.assertIn(
            hashlib.sha256((self.root / name).read_bytes()).hexdigest(),
            str(refusal.exception),
        )

    async def test_an_absent_or_escaping_or_linked_request_file_is_refused(self):
        outside = Path(self.root).parent / "outside.json"
        outside.write_text(json.dumps(self.request()))
        self.addCleanup(outside.unlink, True)
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        os.symlink(outside, self.root / "linked.json")
        for path, expected in (
            ("missing.json", "does not exist"),
            ("../outside.json", "Unsafe review path"),
            ("linked.json", "Unsafe or unreadable review path"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError) as refusal:
                await self.begin(request_file=path, request_sha256=digest)
            self.assertIn(expected, str(refusal.exception))

    async def test_a_request_file_that_is_not_one_clean_json_object_is_refused(self):
        cases = {
            "duplicate.json": '{"requirements": "a", "requirements": "b"}',
            "constant.json": '{"requirements": NaN}',
            "array.json": "[1, 2]",
            "text.json": "not json",
        }
        for name, body in cases.items():
            (self.root / name).write_text(body)
            digest = hashlib.sha256(body.encode()).hexdigest()
            with self.subTest(name=name), self.assertRaises(ValueError):
                await self.begin(request_file=name, request_sha256=digest)
        (self.root / "bytes.json").write_bytes(b'{"requirements": "\xff\xfe"}')
        digest = hashlib.sha256((self.root / "bytes.json").read_bytes()).hexdigest()
        with self.assertRaises(ValueError) as refusal:
            await self.begin(request_file="bytes.json", request_sha256=digest)
        self.assertIn("not valid UTF-8", str(refusal.exception))

    async def test_exactly_one_request_form_is_accepted(self):
        name, digest = self.write_request()
        for values in (
            {},
            {"request": self.request(), "request_file": name, "request_sha256": digest},
        ):
            with (
                self.subTest(values=sorted(values)),
                self.assertRaises(ValueError) as refusal,
            ):
                await self.begin(**values)
            self.assertIn(
                "exactly one of request and request_file", str(refusal.exception)
            )
        with self.assertRaises(ValueError) as refusal:
            await self.begin(request_file=name)
        self.assertIn("request_sha256 is required", str(refusal.exception))
        with self.assertRaises(ValueError) as refusal:
            await self.begin(request=self.request(), request_sha256=digest)
        self.assertIn("forbidden without it", str(refusal.exception))

    # Recovery projection

    async def test_a_partial_result_names_the_material_it_still_needs(self):
        self.scenario("context-request")
        run = await self.wait_run((await self.begin(request=self.request()))["run_id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["outcome"], "partial")
        blocked = run["blocked_on"]
        self.assertEqual(blocked["count"], 1)
        self.assertEqual(blocked["kinds"], ["context_required"])
        self.assertIs(blocked["requires_new_capture"], True)
        self.assertIs(blocked["reply_allowed"], False)
        item = blocked["items"][0]
        self.assertEqual(item["requested_paths"], ["specs/context.md", "specs/plan.md"])
        self.assertEqual(item["requested_material"], ["The complete plan text"])
        self.assertEqual(item["parse_status"], "parsed")
        self.assertEqual(item["recovery_action"], "new_capture")
        self.assertEqual(item["stage"], "independent")
        self.assertIn("Two conditional changes", item["reason"])
        self.assertIn("findings_counts", run)

    async def test_an_unreadable_context_is_reported_rather_than_shown_as_empty(self):
        self.scenario("malformed-context")
        run = await self.wait_run((await self.begin(request=self.request()))["run_id"])
        item = run["blocked_on"]["items"][0]
        self.assertEqual(item["parse_status"], "invalid_json")
        self.assertEqual(item["requested_paths"], [])
        self.assertEqual(item["context"], "not json at all")
        # With nothing parsed, the question itself is the reason; none is invented.
        self.assertEqual(item["reason"], item["question"])

    async def test_a_clean_run_carries_no_recovery_object(self):
        run = await self.wait_run((await self.begin(request=self.request()))["run_id"])
        self.assertNotIn("blocked_on", run)

    def envelope(self, items):
        return {
            "count": len(items),
            "kinds": sorted({item["kind"] for item in items}),
            "requires_new_capture": True,
            "reply_allowed": False,
            "items": items,
            "truncated": False,
        }

    def blocker(self, detail=400, paths=80):
        return blocked_question(
            {
                "question_id": str(uuid4()),
                "task_id": str(uuid4()),
                "question": "Which authorization applies? " + "detail " * detail,
                "context": json.dumps(
                    {"requested_paths": [f"docs/{index}.md" for index in range(paths)]}
                ),
                "state": "context_required",
            },
            "independent",
        )

    def test_the_envelope_is_never_replaced_by_a_generic_truncation_stub(self):
        """Keep the recovery core even when the envelope is the biggest thing left.

        Emptying the item bodies usually makes the envelope small enough that the
        generic pass never reaches it, so that pass alone does not prove the core
        is safe. Here the kept bodies are the largest remaining field and the rest
        of the response is many smaller ones, which is exactly the shape that
        evicts a field by size.
        """
        raw = {
            "run_id": str(uuid4()),
            "status": "completed",
            "outcome": "partial",
            "blocked_on": self.envelope(
                [self.blocker(detail=60, paths=10) for _ in range(2)]
            ),
            "next_action": "review_result",
            **{f"detail_{index}": "value " * 580 for index in range(4)},
        }
        # The bodies are small enough to keep, which makes the envelope the
        # largest field the size-ordered pass reaches first.
        self.assertLess(wire_size(raw["blocked_on"]["items"]), 4000)
        self.assertGreater(wire_size(raw), MAX_RESPONSE_BYTES)
        self.assertGreater(wire_size(raw["blocked_on"]), wire_size(raw["detail_0"]))
        projected = self.bridge.presentation._project(raw, "review_run", details=False)
        self.assertLessEqual(wire_size(projected), MAX_RESPONSE_BYTES)
        self.assertIs(projected["truncated"], True)
        kept = projected["blocked_on"]
        self.assertEqual(kept["count"], 2)
        self.assertEqual(kept["kinds"], ["context_required"])
        self.assertIs(kept["requires_new_capture"], True)
        self.assertIs(kept["reply_allowed"], False)

    def test_the_bodies_are_dropped_before_the_envelope_is(self):
        items = [
            blocked_question(
                {
                    "question_id": str(uuid4()),
                    "task_id": str(uuid4()),
                    "question": "Which authorization applies? " + "detail " * 400,
                    "context": json.dumps(
                        {"requested_paths": [f"docs/{index}.md" for index in range(80)]}
                    ),
                    "state": "context_required",
                },
                "independent",
            )
            for index in range(12)
        ]
        envelope = {
            "count": len(items),
            "kinds": ["context_required"],
            "requires_new_capture": True,
            "reply_allowed": False,
            "items": items,
            "truncated": False,
        }
        self.assertGreater(wire_size(envelope), MAX_RESPONSE_BYTES)
        projected = self.bridge.presentation._project(
            {
                "run_id": str(uuid4()),
                "status": "completed",
                "outcome": "partial",
                "blocked_on": envelope,
                "findings_counts": {"independent": 2, "comparison": 0},
                "next_action": "review_result",
            },
            "review_run",
            details=False,
        )
        self.assertLessEqual(wire_size(projected), MAX_RESPONSE_BYTES)
        kept = projected["blocked_on"]
        self.assertEqual(kept["count"], 12)
        self.assertEqual(kept["items"], [])
        self.assertIs(kept["truncated"], True)
        self.assertIn("blocked_on.items", projected["truncated_fields"])
        self.assertIn("reader", kept)
        self.assertEqual(
            projected["findings_counts"], {"independent": 2, "comparison": 0}
        )

    # The repeated-gap guard

    async def test_the_same_capture_after_an_unmet_request_starts_no_reviewer(self):
        self.scenario("context-request")
        first = await self.wait_run(
            (await self.begin(request=self.request()))["run_id"]
        )
        self.assertEqual(first["outcome"], "partial")
        repeat = await self.wait_run(
            (await self.begin(request=self.request()))["run_id"]
        )
        self.assertEqual(repeat["status"], "context_required")
        self.assertEqual(repeat["next_action"], "supply_context")
        self.assertIsNone(repeat["independent"])
        self.assertIn(first["run_id"], repeat["error"])
        self.assertIn("fresh request_key alone does not repair", repeat["error"])
        self.assertEqual(repeat["prior_equivalent_run"]["run_id"], first["run_id"])

    async def test_repeating_a_complete_review_is_reported_but_not_refused(self):
        first = await self.wait_run(
            (await self.begin(request=self.request()))["run_id"]
        )
        self.assertEqual(first["status"], "completed")
        repeat = await self.wait_run(
            (await self.begin(request=self.request()))["run_id"]
        )
        # Identical bytes are not a cached verdict: a second reading may differ,
        # so this one is allowed, and only the earlier run is named.
        self.assertEqual(repeat["status"], "completed")
        self.assertEqual(repeat["prior_equivalent_run"]["run_id"], first["run_id"])
        self.assertEqual(repeat["prior_equivalent_run"]["outcome"], "success")
        self.assertIsNotNone(repeat["independent"])

    async def test_changed_requirements_are_not_treated_as_the_same_capture(self):
        self.scenario("context-request")
        await self.wait_run((await self.begin(request=self.request()))["run_id"])
        repaired = await self.wait_run(
            (
                await self.begin(
                    request=self.request(
                        requirements="Assess the saved sample. Decision: both changes are allowed."
                    )
                )
            )["run_id"]
        )
        self.assertEqual(repaired["status"], "completed")
        self.assertNotIn("prior_equivalent_run", repaired)


class ExtractionTests(unittest.TestCase):
    def test_each_kind_of_unusable_context_is_told_apart_from_an_empty_request(self):
        self.assertEqual(extract_request(None)["parse_status"], "absent")
        self.assertEqual(extract_request("  ")["parse_status"], "absent")
        self.assertEqual(extract_request("{")["parse_status"], "invalid_json")
        self.assertEqual(extract_request("[1]")["parse_status"], "unrecognized")
        self.assertEqual(extract_request("{}")["parse_status"], "unrecognized")

    def test_one_malformed_member_keeps_the_members_beside_it(self):
        request = extract_request(
            json.dumps({"requested_paths": ["a", "a"], "requested_material": 7})
        )
        self.assertEqual(request["requested_paths"], ["a"])
        self.assertEqual(request["requested_material"], [])
        self.assertEqual(request["parse_status"], "unrecognized")

    def test_only_an_independent_stage_or_a_missing_capture_demands_a_new_one(self):
        self.assertEqual(recovery("context_required", "comparison")[1], "new_capture")
        self.assertEqual(recovery("expired", "independent")[1], "new_capture")
        self.assertEqual(
            recovery("expired", "comparison")[1], "resolve_then_new_task_or_attempt"
        )
        self.assertEqual(
            recovery("decision_unresolved", None)[1],
            "resolve_then_new_task_or_attempt",
        )
