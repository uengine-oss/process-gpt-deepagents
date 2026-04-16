from __future__ import annotations

import unittest


class TestHttpChatRouteNormalize(unittest.TestCase):
    def test_normalize(self) -> None:
        from app.executor.http_chat_route_middleware import normalize_http_chat_route

        self.assertEqual(normalize_http_chat_route("work-assistant-only"), "work_assistant_only")
        self.assertEqual(normalize_http_chat_route("WORK_ASSISTANT_ONLY"), "work_assistant_only")
        self.assertEqual(normalize_http_chat_route("needs_task"), "needs_task")
        self.assertEqual(normalize_http_chat_route("delegate"), "needs_task")
        self.assertEqual(normalize_http_chat_route("ask_user_first"), "ask_user_first")
        self.assertEqual(normalize_http_chat_route("HITL"), "ask_user_first")


if __name__ == "__main__":
    unittest.main()

