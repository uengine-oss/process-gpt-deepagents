"""Tests for merging HTTP chat identity into work-assistant MCP tool args."""

from __future__ import annotations

import unittest

from deepagents_executor import merge_http_chat_identity_into_work_assistant_args


class TestMergeHttpChatIdentity(unittest.TestCase):
    def test_fills_null_args_from_input_data_and_row(self) -> None:
        args = {
            "user_jwt": None,
            "tenant_id": "localhost",
            "user_uid": None,
            "user_email": None,
            "email": None,
        }
        out = merge_http_chat_identity_into_work_assistant_args(
            args,
            input_data={
                "tenant_id": "localhost",
                "user_jwt": "jwt-abc",
                "user_uid": "uid-1",
                "user_email": "u@example.com",
            },
            row={"tenant_id": "ignored", "user_id": "fallback-uid"},
            extras={"notify_user_emails": ["notify@example.com"]},
            tenant_id="localhost",
        )
        self.assertEqual(out["user_jwt"], "jwt-abc")
        self.assertEqual(out["tenant_id"], "localhost")
        self.assertEqual(out["user_uid"], "uid-1")
        self.assertEqual(out["user_email"], "u@example.com")
        self.assertEqual(out["email"], "u@example.com")

    def test_notify_email_fallback(self) -> None:
        args: dict[str, object] = {"email": None}
        out = merge_http_chat_identity_into_work_assistant_args(
            args,
            input_data={"tenant_id": "t1"},
            row={},
            extras={"notify_user_emails": ["notify@example.com"]},
            tenant_id="t1",
        )
        self.assertEqual(out["email"], "notify@example.com")
        self.assertEqual(out["user_email"], "notify@example.com")

    def test_does_not_overwrite_explicit_values(self) -> None:
        args = {"user_jwt": "already", "tenant_id": "t2", "email": "e@x.com"}
        out = merge_http_chat_identity_into_work_assistant_args(
            args,
            input_data={"user_jwt": "other", "tenant_id": "t3", "user_email": "u@y.com"},
            row={},
            extras={},
            tenant_id=None,
        )
        self.assertEqual(out["user_jwt"], "already")
        self.assertEqual(out["tenant_id"], "t2")
        self.assertEqual(out["email"], "e@x.com")


if __name__ == "__main__":
    unittest.main()
