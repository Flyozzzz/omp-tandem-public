"""Project snapshot validation and consumer regressions with disposable databases."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from omp_tandem.project_context import (
    ContextConflict,
    ProductRule,
    ProjectContext,
    ProjectContextStore,
    ProjectDecision,
)


def context_data(project_id="product", summary="A product for café owners.", **kwargs):
    return {"project_id": project_id, "product_summary": summary, **kwargs}


def decision(decision_id, **kwargs):
    return {
        "id": decision_id,
        "text": "Keep offline checkout available.",
        "status": "accepted",
        "source": "Product review 2026-09-09",
        **kwargs,
    }


def canonical_bytes(context):
    return json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ProjectContextValidationTests(unittest.TestCase):
    def test_rules_require_actionable_text_and_source(self):
        rule = {
            "id": "offline",
            "text": "Checkout must work offline.",
            "source": "Brief",
        }
        invalid_rules = [
            {key: value for key, value in rule.items() if key != "source"},
            {**rule, "source": " \t\n"},
            {**rule, "text": " "},
            {**rule, "id": "\t"},
            {**rule, "text": "x" * 4001},
            {**rule, "source": "x" * 2001},
            {**rule, "requirement": "optional"},
            {**rule, "applies_to": ["checkout", " "]},
            {**rule, "positive_examples": ["\n"]},
            {**rule, "negative_examples": ["x" * 2001]},
            {**rule, "positive_examples": ["example"] * 11},
            {**rule, "unrecognized": "must not be silently dropped"},
        ]
        for invalid in invalid_rules:
            with self.subTest(rule=invalid), self.assertRaises(ValidationError):
                ProductRule.model_validate(invalid)

    def test_decision_sources_and_canonical_evidence_ids_are_required(self):
        artifact_id = str(uuid4())
        invalid_decisions = [
            {
                key: value
                for key, value in decision("offline").items()
                if key != "source"
            },
            decision("offline", source=" "),
            decision("offline", status="proposed"),
            decision("offline", evidence_artifact_ids=[artifact_id.replace("-", "")]),
            decision("offline", evidence_artifact_ids=["not-a-uuid"]),
            decision("offline", evidence_artifact_ids=[artifact_id] * 17),
            decision("offline", unexpected=True),
        ]
        for invalid in invalid_decisions:
            with self.subTest(decision=invalid), self.assertRaises(ValidationError):
                ProjectDecision.model_validate(invalid)

    def test_snapshot_ids_and_supersession_graph_are_unambiguous(self):
        rule = {"id": "offline", "text": "Keep checkout available.", "source": "Brief"}
        invalid_snapshots = [
            context_data(rules=[rule, rule]),
            context_data(rules=[rule], decisions=[decision("offline")]),
            context_data(decisions=[decision("old"), decision("old")]),
            context_data(decisions=[decision("new", supersedes="missing")]),
            context_data(
                rules=[rule], decisions=[decision("new", supersedes="offline")]
            ),
            context_data(decisions=[decision("self", supersedes="self")]),
            context_data(
                decisions=[
                    decision("a", supersedes="b"),
                    decision("b", supersedes="c"),
                    decision("c", supersedes="a"),
                ]
            ),
        ]
        for invalid in invalid_snapshots:
            with self.subTest(snapshot=invalid), self.assertRaises(ValidationError):
                ProjectContext.model_validate(invalid)

    def test_project_names_and_lists_reject_ambiguous_identifiers(self):
        for project_id in ("Product", "a/b", " leading", "", "x" * 101, "product\n"):
            with (
                self.subTest(project_id=project_id),
                self.assertRaises(ValidationError),
            ):
                ProjectContext.model_validate(context_data(project_id))
        for invalid in (
            context_data(components=["checkout", "\t"]),
            context_data(components=["checkout"] * 33),
            context_data(artifact_ids=[str(uuid4()).replace("-", "")]),
            context_data(unexpected="unrecognized context"),
        ):
            with self.subTest(snapshot=invalid), self.assertRaises(ValidationError):
                ProjectContext.model_validate(invalid)


class ProjectContextStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "tasks.sqlite3"
        self.store = ProjectContextStore(self.db_path)

    def test_pinned_revisions_survive_new_publications_and_consumer_mutation(self):
        original = ProjectContext.model_validate(
            context_data(
                decisions=[
                    decision("old", status="superseded"),
                    decision("new", supersedes="old"),
                ]
            )
        )
        first = self.store.publish(original, publisher="reviewer")
        pinned = self.store.get(first["context_id"])
        original.product_summary = "The next product direction."
        second = self.store.publish(original, expected_revision=1)
        pinned["context"]["decisions"].clear()
        reopened = ProjectContextStore(self.db_path)
        self.assertEqual(
            reopened.get(first["context_id"])["context"]["product_summary"],
            "A product for café owners.",
        )
        self.assertEqual(
            [
                item["id"]
                for item in reopened.get(first["context_id"])["context"]["decisions"]
            ],
            ["old", "new"],
        )
        self.assertEqual(
            reopened.get(second["context_id"])["context"]["product_summary"],
            "The next product direction.",
        )
        self.assertEqual((first["revision"], second["revision"]), (1, 2))
        self.assertEqual(
            [entry["context_id"] for entry in reopened.list("product")],
            [second["context_id"], first["context_id"]],
        )

    def test_digest_identifies_canonical_utf8_content_not_input_key_order(self):
        data = context_data(components=["café", "雪"])
        first = self.store.publish(data)
        reordered = dict(reversed(tuple(data.items())))
        second = self.store.publish(reordered, expected_revision=1)
        content = self.store.get(first["context_id"])["context"]
        self.assertEqual(
            first["sha256"], hashlib.sha256(canonical_bytes(content)).hexdigest()
        )
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["context_id"], second["context_id"])

    def test_two_stores_cannot_overwrite_the_same_expected_revision(self):
        first = self.store.publish(context_data())
        stores = [self.store, ProjectContextStore(self.db_path)]
        barrier = threading.Barrier(2)

        def publish(index):
            barrier.wait(timeout=10)
            try:
                result = stores[index].publish(
                    context_data(summary=f"Author {index}"), expected_revision=1
                )
                return index, result
            except ContextConflict:
                return index, None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish, range(2)))
        winners = [(index, result) for index, result in results if result is not None]
        self.assertEqual(len(winners), 1)
        winner, accepted = winners[0]
        self.assertEqual(accepted["revision"], 2)
        self.assertEqual(
            self.store.get(accepted["context_id"])["context"]["product_summary"],
            f"Author {winner}",
        )
        self.assertEqual(
            [entry["context_id"] for entry in self.store.list("product")],
            [accepted["context_id"], first["context_id"]],
        )
        reconciled = stores[1 - winner].publish(
            context_data(summary="Reconciled"), expected_revision=2
        )
        self.assertEqual(reconciled["revision"], 3)
        self.assertEqual(
            self.store.get(first["context_id"])["context"]["product_summary"],
            "A product for café owners.",
        )

    def test_conflicts_leave_no_rows_and_require_explicit_current_revision(self):
        with self.assertRaises(ContextConflict):
            self.store.publish(context_data(), expected_revision=1)
        self.assertEqual(self.store.list(), [])
        first = self.store.publish(context_data(), expected_revision=0)
        for expected in (None, 0, 2, True, 1.0):
            with self.subTest(expected=expected), self.assertRaises(ContextConflict):
                self.store.publish(
                    context_data(summary="Rejected"), expected_revision=expected
                )
        self.assertEqual(self.store.list(), [first])
        next_revision = self.store.publish(
            context_data(summary="Accepted"), expected_revision=1
        )
        self.assertEqual(next_revision["revision"], 2)

    def test_byte_cap_counts_canonical_utf8_and_rejection_consumes_no_revision(self):
        data = ProjectContext.model_validate(
            context_data(
                summary="雪" * 12000,
                components=["雪" * 5000],
                rules=[{"id": "rule", "text": "雪" * 4000, "source": "Brief"}],
            )
        ).model_dump(mode="json")
        data["components"][0] += "x" * (64000 - len(canonical_bytes(data)))
        first = self.store.publish(data)
        self.assertEqual(
            len(canonical_bytes(self.store.get(first["context_id"])["context"])), 64000
        )
        data["components"][0] += "é"
        with self.assertRaises(ValueError):
            self.store.publish(data, expected_revision=1)
        self.assertEqual(self.store.list(), [first])
        accepted = self.store.publish(
            context_data(summary="Smaller revision"), expected_revision=1
        )
        self.assertEqual(accepted["revision"], 2)

    def test_project_namespaces_allocate_and_filter_independently(self):
        a1 = self.store.publish(context_data("product-a"))
        b1 = self.store.publish(context_data("product.b"), expected_revision=0)
        a2 = self.store.publish(
            context_data("product-a", summary="Revised A"), expected_revision=1
        )
        self.assertEqual((a1["revision"], b1["revision"], a2["revision"]), (1, 1, 2))
        self.assertEqual(self.store.list("product-a"), [a2, a1])
        self.assertEqual(self.store.list("product.b"), [b1])
        self.assertEqual(self.store.list(limit=2), [a2, b1])
        self.assertEqual(self.store.list("unknown"), [])

    def test_publication_revalidates_mutated_nested_models(self):
        model = ProjectContext.model_validate(
            context_data(
                rules=[
                    {
                        "id": "rule",
                        "text": "Keep checkout available.",
                        "source": "Brief",
                    }
                ]
            )
        )
        model.rules[0].source = " "
        with self.assertRaises(ValidationError):
            self.store.publish(model)
        self.assertEqual(self.store.list(), [])

    def test_lookup_rejects_invalid_and_unknown_snapshot_ids(self):
        for lookup in (self.store.get, self.store.info):
            for context_id in ("../not-a-snapshot", str(uuid4())):
                with (
                    self.subTest(lookup=lookup.__name__, context_id=context_id),
                    self.assertRaises(ValueError),
                ):
                    lookup(context_id)


if __name__ == "__main__":
    unittest.main()
