"""Conservative presentation never changes the permitted callback catalog."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omp_rpc import host_tool

from omp_tandem.focus_tools import (
    CORE_ESSENTIAL,
    discovery_capability,
    optional_tool_candidates,
    tool_presentation,
)


def catalog():
    return tuple(
        host_tool(
            name=name,
            description="Trusted description for " + name,
            parameters={"type": "object", "properties": {}},
            decode=lambda request: request,
            execute=lambda request, _: str(request),
        )
        for name in (
            "tandem_finish",
            "tandem_work",
            "tandem_ask",
            "tandem_publish_artifact",
            "tandem_read_artifact",
            "tandem_context_read",
            "tandem_review_read",
            "future_unknown_callback",
        )
    )


class ToolPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tools = catalog()
        self.candidates = optional_tool_candidates(
            self.tools, enabled=True, task={"mode": "analyze"}, native_tools=("read",)
        )

    def select(self, label="irrelevant", candidates=None, **options):
        candidates = self.candidates if candidates is None else candidates
        selection = {
            "status": "completed",
            "labels": {candidate.id: label for candidate in candidates},
            "assessed_ids": [candidate.id for candidate in candidates],
        }
        selection.update(options)
        return tool_presentation(self.tools, candidates, selection, can_discover=True)[
            "load_modes"
        ]

    def test_only_assessed_low_rank_optional_callbacks_are_deferred(self):
        modes = self.select()
        self.assertEqual(list(modes), [tool.name for tool in self.tools])
        self.assertEqual(
            {name for name, mode in modes.items() if mode == "discoverable"},
            {"tandem_work", "tandem_publish_artifact"},
        )
        self.assertTrue(all(modes[name] == "essential" for name in CORE_ESSENTIAL))
        self.assertEqual(modes["future_unknown_callback"], "essential")
        for label in ("relevant", "conflicting", "unclear", "unknown"):
            with self.subTest(label=label):
                self.assertEqual(set(self.select(label).values()), {"essential"})

    def test_failure_unassessed_and_missing_transport_keep_full_catalog(self):
        self.assertEqual(set(self.select(status="fallback").values()), {"essential"})
        self.assertEqual(set(self.select(assessed_ids=[]).values()), {"essential"})
        modes = tool_presentation(
            self.tools,
            self.candidates,
            {"status": "completed", "labels": {}, "assessed_ids": []},
            can_discover=False,
        )["load_modes"]
        self.assertEqual(set(modes.values()), {"essential"})

    def test_stale_or_protected_candidate_cannot_hide_schema(self):
        original = self.candidates[0]
        changed = original.model_copy(update={"text": "altered descriptor"})
        protected = original.model_copy(update={"protected": True})
        for candidate in (changed, protected):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    set(self.select(candidates=[candidate]).values()), {"essential"}
                )

    def test_think_and_protected_tasks_do_not_export_tool_candidates(self):
        for task, native_tools in (
            ({"mode": "think"}, ("read",)),
            ({"mode": "analyze"}, ()),
            ({"mode": "work", "review_id": "pinned"}, ("read", "write")),
            ({"mode": "work", "native_attempt": {"kind": "implement"}}, ("read",)),
            ({"mode": "work", "managed": True}, ("read",)),
        ):
            with self.subTest(task=task):
                self.assertEqual(
                    optional_tool_candidates(
                        self.tools, enabled=True, task=task, native_tools=native_tools
                    ),
                    [],
                )

    def test_candidate_set_cannot_add_an_unavailable_callback(self):
        permitted = [tool for tool in self.tools if tool.name != "tandem_work"]
        modes = tool_presentation(
            permitted,
            self.candidates,
            {
                "status": "completed",
                "labels": {candidate.id: "supporting" for candidate in self.candidates},
                "assessed_ids": [candidate.id for candidate in self.candidates],
            },
            can_discover=True,
        )["load_modes"]
        self.assertNotIn("tandem_work", modes)
        self.assertEqual(modes["tandem_publish_artifact"], "discoverable")


class DiscoveryGateTests(unittest.TestCase):
    def test_only_verified_version_with_native_read_can_use_overlay(self):
        with patch("omp_tandem.focus_tools.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout="omp/18.1.13\n")
            self.assertTrue(
                discovery_capability("omp", native_tools=("read",))["supported"]
            )
            run.return_value = SimpleNamespace(stdout="omp/18.1.14\n")
            self.assertFalse(
                discovery_capability("omp", native_tools=("read",))["supported"]
            )
            run.side_effect = subprocess.TimeoutExpired("omp", 1)
            self.assertEqual(
                discovery_capability("omp", native_tools=("read",))["reason"],
                "runtime_unavailable",
            )

    def test_no_transport_or_deadline_does_not_launch_runtime(self):
        with patch("omp_tandem.focus_tools.subprocess.run") as run:
            self.assertFalse(discovery_capability("omp", native_tools=())["supported"])
            self.assertFalse(
                discovery_capability("omp", native_tools=("read",), timeout_seconds=0)[
                    "supported"
                ]
            )
            run.assert_not_called()
