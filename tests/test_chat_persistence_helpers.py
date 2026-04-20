from __future__ import annotations

import unittest

from app.database import build_chat_attachment_rows, merge_chat_room_context_from_event


class TestChatPersistenceHelpers(unittest.TestCase):
    def test_build_chat_attachment_rows_extracts_name_and_path_and_dedups(self) -> None:
        rows = build_chat_attachment_rows(
            tenant_id="localhost",
            chat_room_id="room-1",
            user_name="홍길동",
            file={"fileName": "a.pdf", "fileUrl": "https://x/a.pdf"},
            files=[
                {"name": "b.pdf", "publicUrl": "https://x/b.pdf"},
                # duplicate by same path+name
                {"fileName": "a.pdf", "fileUrl": "https://x/a.pdf"},
            ],
        )
        self.assertEqual(len(rows), 2)
        names = sorted([(r.get("file_name"), r.get("file_path")) for r in rows])
        self.assertEqual(
            names,
            [
                ("a.pdf", "https://x/a.pdf"),
                ("b.pdf", "https://x/b.pdf"),
            ],
        )
        for r in rows:
            self.assertEqual(r.get("tenant_id"), "localhost")
            self.assertEqual(r.get("chat_room_id"), "room-1")
            self.assertEqual(r.get("user_name"), "홍길동")
            self.assertTrue(isinstance(r.get("id"), str) and len(str(r.get("id"))) > 10)

    def test_merge_context_plan_tools_and_tool_status(self) -> None:
        ctx0 = {"todos": [], "tools": [], "skills": [], "enabled": {"todos": False, "tools": True, "skills": False}}
        ctx1 = merge_chat_room_context_from_event(
            ctx0,
            {
                "type": "plan_tools",
                "tools": [
                    {"tool": "work-assistant__get_organization", "name": "get_organization", "args": {}, "id": "call_1"},
                ],
            },
        )
        self.assertIsInstance(ctx1, dict)
        tools = ctx1.get("tools")
        self.assertIsInstance(tools, list)
        self.assertEqual(tools[0]["id"], "call_1")
        self.assertEqual(tools[0]["status"], "planned")

        ctx2 = merge_chat_room_context_from_event(
            ctx1,
            {
                "type": "tool_start",
                "tool": "work-assistant__get_organization",
                "input": {"id": "call_1", "name": "get_organization"},
            },
        )
        self.assertIsInstance(ctx2, dict)
        self.assertEqual(ctx2["tools"][0]["status"], "running")

        ctx3 = merge_chat_room_context_from_event(
            ctx2,
            {
                "type": "tool_end",
                "tool": "work-assistant__get_organization",
                "input": {"id": "call_1", "name": "get_organization"},
            },
        )
        self.assertEqual(ctx3["tools"][0]["status"], "done")

