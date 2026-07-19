from __future__ import annotations

import json
import unittest

from ..progress import CodexProgressBridge


def completed_item(item_type: str, text: str) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"type": item_type, "text": text},
        }
    )


class CodexProgressBridgeTest(unittest.TestCase):
    def test_forwards_only_agent_message_proven_to_be_intermediate(self) -> None:
        updates: list[str] = []
        bridge = CodexProgressBridge(updates.append)

        bridge.handle_line(completed_item("agent_message", "first progress"))
        bridge.handle_line(completed_item("agent_message", "final answer"))
        bridge.finish()

        self.assertEqual(updates, ["first progress"])

    def test_reasoning_replaces_pending_candidate_without_leaking_it(self) -> None:
        updates: list[str] = []
        bridge = CodexProgressBridge(updates.append)

        bridge.handle_line(completed_item("agent_message", "possible final"))
        bridge.handle_line(completed_item("reasoning", "newer public reasoning"))
        bridge.finish()

        self.assertEqual(updates, ["newer public reasoning"])

    def test_work_item_proves_buffered_message_is_live_progress(self) -> None:
        updates: list[str] = []
        bridge = CodexProgressBridge(updates.append)

        bridge.handle_line(completed_item("agent_message", "checking code"))
        bridge.handle_line(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "rg pattern"},
                }
            )
        )

        self.assertEqual(updates, ["checking code"])

        bridge.handle_line(completed_item("agent_message", "final answer"))
        bridge.finish()
        self.assertEqual(updates, ["checking code"])

    def test_supports_legacy_public_event_messages(self) -> None:
        updates: list[str] = []
        bridge = CodexProgressBridge(updates.append)

        bridge.handle_line(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_reasoning", "text": "thinking"},
                }
            )
        )
        bridge.handle_line("not-json")
        bridge.handle_line(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "final"},
                }
            )
        )
        bridge.finish()

        self.assertEqual(updates, ["thinking"])


if __name__ == "__main__":
    unittest.main()
