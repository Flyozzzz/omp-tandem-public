"""Exact focus views cannot change pinned inputs or hide protected diagnostics."""

from __future__ import annotations

import hashlib
import json
import unittest
from uuid import uuid4

from omp_tandem.focus_materials import (
    apply_startup,
    reader_candidates,
    render_reader,
    startup_candidates,
)
from omp_tandem.project_context import ProjectContext, _json_bytes, task_context_packet


class FocusMaterialsTests(unittest.TestCase):
    def setUp(self):
        context = ProjectContext.model_validate(
            {
                "project_id": "focus",
                "product_summary": "Current product policy",
                "rules": [
                    {
                        "id": "required",
                        "text": "MUST retain approval",
                        "source": "owner",
                        "positive_examples": ["Пример café\u0000批准"],
                    },
                    {
                        "id": "optional",
                        "requirement": "advisory",
                        "text": "Prefer chronological sorting",
                        "source": "research",
                        "positive_examples": ["Oldest first"],
                        "negative_examples": ["Newest first"],
                    },
                ],
                "decisions": [
                    {
                        "id": "current",
                        "status": "accepted",
                        "text": "Current decision",
                        "source": "owner",
                    },
                    {
                        "id": "old",
                        "status": "superseded",
                        "text": "Historical decision",
                        "source": "owner",
                    },
                ],
            }
        ).model_dump(mode="json")
        self.snapshot = {
            "context_id": str(uuid4()),
            "project_id": "focus",
            "revision": 1,
            "sha256": hashlib.sha256(_json_bytes(context)).hexdigest(),
            "created": 1.0,
            "publisher": "test",
            "context": context,
        }
        self.task = {
            "project_context_id": self.snapshot["context_id"],
            "contract_json": None,
            "prompt": "Inspect sorting",
        }

    def selection(self, candidates, label="relevant"):
        ids = [item.id for item in candidates if not item.protected]
        return {
            "status": "completed",
            "labels": dict.fromkeys(ids, label),
            "assessed_ids": ids,
            "unassessed_ids": [],
        }

    def message(self, task=None):
        task = task or self.task
        contract = json.loads(task["contract_json"] or "{}")
        return json.dumps(
            {
                "task": {
                    "goal": "Inspect sorting",
                    "constraints": ["Never mutate current input"],
                },
                "work_policy": {"mode": "analyze"},
                "project_context": task_context_packet(
                    self.snapshot, contract.get("context_options")
                ),
            },
            ensure_ascii=False,
        )

    def artifact(self, text, *, offset=0, media_type="text/plain", more=False):
        return {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "media_type": media_type,
            "content": text,
            "characters": offset + len(text) + int(more),
            "next_offset": offset + len(text) if more else None,
        }

    def context_window(self, pointer, text, *, start=0, length=16384):
        encoded = _json_bytes(text)
        end = min(start + length, len(encoded))
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        return {
            **{key: value for key, value in self.snapshot.items() if key != "context"},
            "pointer": pointer,
            "encoding": "canonical-json-utf8",
            "pointer_sha256": hashlib.sha256(encoded).hexdigest(),
            "total_bytes": len(encoded),
            "offset_bytes": start,
            "returned_bytes": end - start,
            "next_offset_bytes": end if end < len(encoded) else None,
            "complete": end == len(encoded),
            "content": encoded[start:end].decode(),
        }

    def test_optional_extraction_excludes_current_explicit_and_full_inputs(self):
        candidates = startup_candidates(self.task, self.snapshot)
        pointers = {item.source["pointer"] for item in candidates}
        self.assertEqual(
            pointers,
            {
                "/rules/0/positive_examples/0",
                "/rules/1/text",
                "/rules/1/positive_examples/0",
                "/rules/1/negative_examples/0",
                "/decisions/1/text",
            },
        )
        selected = {
            **self.task,
            "contract_json": json.dumps(
                {
                    "context_options": {
                        "advisory_rule_ids": ["optional"],
                        "decision_ids": ["old"],
                    }
                }
            ),
        }
        self.assertEqual(
            [
                item.source["pointer"]
                for item in startup_candidates(selected, self.snapshot)
            ],
            ["/rules/0/positive_examples/0"],
        )
        full = {
            **self.task,
            "contract_json": json.dumps({"context_options": {"delivery": "full"}}),
        }
        self.assertEqual(startup_candidates(full, self.snapshot), [])
        raw = self.message(full)
        self.assertEqual(
            apply_startup(raw, full, self.snapshot, [], self.selection([]), 8192)[0],
            raw,
        )
        pointer = "/rules/1/text"
        window = self.context_window(
            pointer, self.snapshot["context"]["rules"][1]["text"]
        )
        for task in (selected, full):
            self.assertIsNone(
                reader_candidates(
                    window,
                    kind="context",
                    task=task,
                    snapshot=self.snapshot,
                    request={"pointer": pointer},
                )
            )

    def test_startup_enrichment_preserves_mandatory_packet_and_exact_values(self):
        candidates = startup_candidates(self.task, self.snapshot)
        original = self.message()
        rendered, metrics = apply_startup(
            original,
            self.task,
            self.snapshot,
            candidates,
            self.selection(candidates),
            8192,
        )
        before, after = json.loads(original), json.loads(rendered)
        self.assertEqual(after["task"], before["task"])
        self.assertEqual(after["work_policy"], before["work_policy"])
        previous, current = (
            before["project_context"]["context"],
            after["project_context"]["context"],
        )
        self.assertEqual(current["rules"][0], previous["rules"][0])
        self.assertEqual(current["decisions"][0], previous["decisions"][0])
        self.assertEqual(
            current["rules"][1]["text"], self.snapshot["context"]["rules"][1]["text"]
        )
        focus = after["project_context"]["focus"]
        examples = {
            item["source"]["pointer"]: item["text"]
            for item in focus["material"]
            if "text" in item
        }
        self.assertEqual(
            examples["/rules/0/positive_examples/0"], "Пример café\u0000批准"
        )
        self.assertEqual(metrics["message_bytes"], len(rendered.encode()))
        self.assertEqual(
            metrics["added_message_bytes"],
            len(rendered.encode()) - len(original.encode()),
        )
        self.assertGreater(metrics["added_message_bytes"], 0)
        self.assertEqual(
            after["project_context"]["delivered_context_bytes"],
            len(_json_bytes(current)),
        )
        self.assertEqual(json.loads(original), before)

    def test_unclear_failure_and_byte_caps_return_original_startup(self):
        candidates = startup_candidates(self.task, self.snapshot)
        original = self.message()
        for selection, cap in (
            (self.selection(candidates, "unclear"), 8192),
            ({"status": "fallback"}, 8192),
            (self.selection(candidates), 1),
        ):
            with self.subTest(selection=selection, cap=cap):
                self.assertEqual(
                    apply_startup(
                        original, self.task, self.snapshot, candidates, selection, cap
                    )[0],
                    original,
                )
        oversized = json.loads(original)
        oversized["unchanged_padding"] = "я" * 100000
        original = json.dumps(oversized, ensure_ascii=False)
        self.assertEqual(
            apply_startup(
                original,
                self.task,
                self.snapshot,
                candidates,
                self.selection(candidates),
                32000,
            )[0],
            original,
        )

    def test_tampered_startup_source_is_rejected(self):
        candidates = startup_candidates(self.task, self.snapshot)
        candidates[0] = candidates[0].model_copy(update={"text": "invented"})
        with self.assertRaises(ValueError):
            apply_startup(
                self.message(),
                self.task,
                self.snapshot,
                candidates,
                self.selection(candidates),
                8192,
            )
        changed = {
            **self.snapshot,
            "context": {**self.snapshot["context"], "product_summary": "mutated"},
        }
        with self.assertRaises(ValueError):
            startup_candidates(self.task, changed)

    def test_artifact_codepoint_ranges_reconstruct_multibyte_nul_window(self):
        text = "я\u0000批准 café\n" + "Ж" * 1300 + "\ntrailing\n"
        raw = self.artifact(text, offset=13, more=True)
        request = {
            "artifact_id": raw["artifact_id"],
            "offset": 13,
            "limit": len(text),
            "view": "focused",
        }
        candidates = reader_candidates(
            raw, kind="artifact", task=self.task, request=request
        )
        self.assertEqual("".join(item.text for item in candidates), text)
        view = render_reader(
            raw,
            kind="artifact",
            request=request,
            candidates=candidates,
            selection=self.selection(candidates),
            max_output_bytes=32000,
        )
        self.assertEqual(view["view"], "focused")
        self.assertFalse(view["complete"])
        self.assertFalse(view["source_window_complete"])
        self.assertEqual(view["next_offset"], raw["next_offset"])
        for excerpt in view["excerpts"]:
            self.assertEqual(
                text[excerpt["start"] - 13 : excerpt["end"] - 13], excerpt["text"]
            )
            self.assertEqual(excerpt["unit"], "unicode_code_points")
        self.assertEqual(view["raw_request"], {**request, "view": "raw"})
        self.assertNotIn("content", view)

    def test_context_utf8_ranges_are_canonical_json_not_decoded_string_offsets(self):
        pointer = "/rules/0/positive_examples/0"
        value = self.snapshot["context"]["rules"][0]["positive_examples"][0]
        raw = self.context_window(pointer, value, start=1, length=16)
        request = {
            "pointer": pointer,
            "offset_bytes": 1,
            "max_bytes": 16,
            "view": "focused",
        }
        candidates = reader_candidates(
            raw, kind="context", task=self.task, snapshot=self.snapshot, request=request
        )
        view = render_reader(
            raw,
            kind="context",
            request=request,
            candidates=candidates,
            selection=self.selection(candidates),
            max_output_bytes=8192,
        )
        self.assertEqual(view["view"], "focused")
        encoded = _json_bytes(value)
        for excerpt in view["excerpts"]:
            self.assertEqual(
                encoded[excerpt["start"] : excerpt["end"]], excerpt["text"].encode()
            )
            self.assertEqual(excerpt["unit"], "utf8_bytes")
        self.assertEqual(view["next_offset_bytes"], raw["next_offset_bytes"])
        self.assertEqual(view["pointer_sha256"], raw["pointer_sha256"])
        changed = {**raw, "content": "invented", "returned_bytes": 8}
        with self.assertRaises(ValueError):
            reader_candidates(
                changed,
                kind="context",
                task=self.task,
                snapshot=self.snapshot,
                request=request,
            )

    def test_encoded_context_diagnostic_remains_available_after_irrelevant_choice(self):
        pointer = "/rules/1/text"
        value = "before\nERROR: disk offline\nafter"
        self.snapshot["context"]["rules"][1]["text"] = value
        self.snapshot["sha256"] = hashlib.sha256(
            _json_bytes(self.snapshot["context"])
        ).hexdigest()
        raw = self.context_window(pointer, value)
        request = {"pointer": pointer}
        candidates = reader_candidates(
            raw, kind="context", task=self.task, snapshot=self.snapshot, request=request
        )
        result = render_reader(
            raw,
            kind="context",
            request=request,
            candidates=candidates,
            selection=self.selection(candidates or [], "irrelevant"),
            max_output_bytes=8192,
        )
        retained = result.get(
            "content", "".join(item["text"] for item in result.get("excerpts", []))
        )
        self.assertEqual(retained, raw["content"])
        self.assertEqual(result["next_offset_bytes"], raw["next_offset_bytes"])

    def test_artifact_page_edges_preserve_diagnostic_remainders_and_neighbors(self):
        cases = (
            (
                "ERROR: disk offline\nneighbor\nmiddle\n",
                7,
                100,
                "disk offline\nneighbor\n",
            ),
            ("ERROR: disk offline\nneighbor\nmiddle\n", 20, 100, "neighbor\n"),
            ("neighbor\nordinary WARNING: bad\n", 0, 18, "neighbor\nordinary "),
        )
        for source, offset, limit, protected in cases:
            with self.subTest(offset=offset, limit=limit):
                text = source[offset : offset + limit]
                raw = self.artifact(
                    text, offset=offset, more=offset + len(text) < len(source)
                )
                request = {"offset": offset, "limit": limit}
                candidates = reader_candidates(
                    raw, kind="artifact", task=self.task, request=request
                )
                result = render_reader(
                    raw,
                    kind="artifact",
                    request=request,
                    candidates=candidates,
                    selection=self.selection(candidates, "irrelevant"),
                    max_output_bytes=8192,
                )
                retained = result.get(
                    "content",
                    "".join(item["text"] for item in result.get("excerpts", [])),
                )
                self.assertIn(protected, retained)
                self.assertEqual(result["next_offset"], raw["next_offset"])

    def test_mixed_required_and_structured_values_never_become_candidates(self):
        for pointer in (
            "",
            "/rules",
            "/rules/0",
            "/rules/0/text",
            "/decisions/0/text",
            "/product_summary",
        ):
            with self.subTest(pointer=pointer):
                self.assertIsNone(
                    reader_candidates(
                        {"content": "raw"},
                        kind="context",
                        task=self.task,
                        snapshot=self.snapshot,
                        request={"pointer": pointer},
                    )
                )
        for media_type in ("application/json", "application/octet-stream", "text/csv"):
            raw = self.artifact('{"error": false}', media_type=media_type)
            self.assertIsNone(
                reader_candidates(raw, kind="artifact", task=self.task, request={})
            )

    def test_context_window_may_split_a_json_escape_without_changing_bytes(self):
        pointer = "/rules/0/positive_examples/0"
        value = self.snapshot["context"]["rules"][0]["positive_examples"][0]
        encoded = _json_bytes(value)
        start = encoded.index(b"\\u0000") + 2
        raw = self.context_window(pointer, value, start=start, length=4)
        request = {"pointer": pointer, "offset_bytes": start, "max_bytes": 4}
        candidates = reader_candidates(
            raw, kind="context", task=self.task, snapshot=self.snapshot, request=request
        )
        result = render_reader(
            raw,
            kind="context",
            request=request,
            candidates=candidates,
            selection=self.selection(candidates),
            max_output_bytes=8192,
        )
        self.assertEqual(result["view"], "focused")
        excerpt = result["excerpts"][0]
        self.assertEqual(excerpt["text"], "0000")
        self.assertEqual(
            encoded[excerpt["start"] : excerpt["end"]], excerpt["text"].encode()
        )
        self.assertEqual(result["next_offset_bytes"], start + 4)

    def test_diagnostics_constraints_and_neighbors_survive_irrelevant_labels(self):
        text = "noise\nnoise\nbefore\nERROR: original stack\nafter\nnoise\nnoise\nMust preserve approval\nneighbor\nnoise\nnoise\n"
        raw = self.artifact(text)
        candidates = reader_candidates(raw, kind="artifact", task=self.task, request={})
        selection = self.selection(candidates, "irrelevant")
        # Even hostile labels for protected chunks cannot override protection.
        selection["labels"].update(
            {item.id: "irrelevant" for item in candidates if item.protected}
        )
        view = render_reader(
            raw,
            kind="artifact",
            request={},
            candidates=candidates,
            selection=selection,
            max_output_bytes=16000,
        )
        delivered = "".join(item["text"] for item in view["excerpts"])
        self.assertIn("before\nERROR: original stack\nafter\n", delivered)
        self.assertIn("Must preserve approval\nneighbor\n", delivered)
        self.assertTrue(view["coverage"]["omitted"])
        self.assertFalse(view["focus"]["diagnostic_completeness"])
        protected = next(item for item in candidates if item.protected)
        demoted = [
            item.model_copy(update={"protected": False})
            if item.id == protected.id
            else item
            for item in candidates
        ]
        with self.assertRaises(ValueError):
            render_reader(
                raw,
                kind="artifact",
                request={},
                candidates=demoted,
                selection=self.selection(demoted),
                max_output_bytes=32000,
            )

    def test_unassessed_retained_and_oversized_protected_returns_exact_raw(self):
        raw = self.artifact("normal\nERROR " + "я" * 3000 + "\ncontext\n")
        candidates = reader_candidates(raw, kind="artifact", task=self.task, request={})
        selection = {
            "status": "completed",
            "labels": {},
            "assessed_ids": [],
            "unassessed_ids": [item.id for item in candidates],
        }
        result = render_reader(
            raw,
            kind="artifact",
            request={},
            candidates=candidates,
            selection=selection,
            max_output_bytes=256,
        )
        self.assertEqual(result["view"], "raw")
        for key, value in raw.items():
            self.assertEqual(result[key], value)
        view = render_reader(
            raw,
            kind="artifact",
            request={},
            candidates=candidates,
            selection=selection,
            max_output_bytes=32000,
        )
        self.assertEqual(
            "".join(item["text"] for item in view["excerpts"]), raw["content"]
        )
        self.assertEqual(view["coverage"]["examined"], [])
        self.assertTrue(view["coverage"]["unexamined"])

    def test_reader_reranks_conflicting_and_relevant_excerpts_without_moving_cursor(
        self,
    ):
        text = "leading edge\nleading neighbor\nbackground detail\ncontradictory observation\nrelevant observation\ntrailing neighbor\ntrailing edge\n"
        raw = self.artifact(text, offset=11, more=True)
        request = {"offset": 11, "limit": 4096}
        candidates = reader_candidates(
            raw, kind="artifact", task=self.task, request=request
        )
        classes = {
            "background detail\n": "supporting",
            "contradictory observation\n": "conflicting",
            "relevant observation\n": "relevant",
        }
        labels = {
            item.id: classes[item.text] for item in candidates if not item.protected
        }
        selection = {
            "status": "completed",
            "labels": labels,
            "assessed_ids": list(labels),
            "unassessed_ids": [],
        }
        result = render_reader(
            raw,
            kind="artifact",
            request=request,
            candidates=candidates,
            selection=selection,
            max_output_bytes=8192,
        )
        self.assertEqual(
            [item["text"] for item in result["excerpts"] if not item["protected"]],
            [
                "contradictory observation\n",
                "relevant observation\n",
                "background detail\n",
            ],
        )
        for item in result["excerpts"]:
            self.assertEqual(text[item["start"] - 11 : item["end"] - 11], item["text"])
        self.assertEqual(result["next_offset"], raw["next_offset"])
        self.assertEqual(
            result["raw_request"],
            {**request, "artifact_id": raw["artifact_id"], "view": "raw"},
        )

    def test_reader_failure_unknown_and_unclear_fallback_preserve_raw_cursor(self):
        raw = self.artifact(
            "first\nsecond\nmiddle\nfourth\nlast\n", offset=7, more=True
        )
        request = {"offset": 7, "limit": len(raw["content"])}
        candidates = reader_candidates(
            raw, kind="artifact", task=self.task, request=request
        )
        for selection in (
            {"status": "fallback", "reason": "quota"},
            self.selection(candidates, "unclear"),
            self.selection(candidates, "invented"),
        ):
            result = render_reader(
                raw,
                kind="artifact",
                request=request,
                candidates=candidates,
                selection=selection,
                max_output_bytes=8192,
            )
            self.assertEqual(result["view"], "raw")
            self.assertEqual(result["content"], raw["content"])
            self.assertEqual(result["next_offset"], raw["next_offset"])


if __name__ == "__main__":
    unittest.main()
