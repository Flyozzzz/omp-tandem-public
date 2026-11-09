"""The pinned OMP RPC SDK must accept every compaction action OMP emits.

Provider-native compaction makes OMP send ``action: "remote"``. An SDK that decodes
the action against a closed set raises ``RpcError`` inside the event reader, the worker
exits, and the task is lost. This guards the pin, not Tandem code: Tandem has no
compaction handling of its own.
"""

import unittest

from omp_rpc import parse_auto_compaction_end_event, parse_auto_compaction_start_event

ACTIONS = ("context-full", "remote", "handoff", "shake", "snapcompact")


class AutoCompactionActionTests(unittest.TestCase):
    def test_start_event_accepts_every_action_omp_emits(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                event = parse_auto_compaction_start_event(
                    {
                        "type": "auto_compaction_start",
                        "reason": "threshold",
                        "action": action,
                    }
                )
                self.assertEqual(event.action, action)

    def test_remote_compaction_start_is_not_an_rpc_error(self):
        event = parse_auto_compaction_start_event(
            {"type": "auto_compaction_start", "reason": "threshold", "action": "remote"}
        )
        self.assertEqual(event.action, "remote")

    def test_end_event_accepts_remote(self):
        event = parse_auto_compaction_end_event(
            {
                "type": "auto_compaction_end",
                "action": "remote",
                "result": None,
                "aborted": False,
                "willRetry": False,
            }
        )
        self.assertEqual(event.action, "remote")


if __name__ == "__main__":
    unittest.main()
