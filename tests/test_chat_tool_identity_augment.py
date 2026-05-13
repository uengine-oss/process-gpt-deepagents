"""채팅 루트 MCP 도구에 식별자 병합(augment) 동작 회귀 테스트."""

from __future__ import annotations

import unittest

from deepagents_executor import make_http_chat_work_assistant_tool_input_augmenter


class _Tool:
    def __init__(self, name: str, server: str) -> None:
        self.name = name
        self._mcp_server = server


class TestChatToolIdentityAugment(unittest.TestCase):
    def test_merge_by_chat_root_ids_ignores_mismatched_mcp_server_tag(self) -> None:
        """테넌트 MCP 키가 work-assistant가 아니어도, 채팅 루트로 올라온 인스턴스면 병합한다."""
        t = _Tool("get_current_user", "process-gpt-mcp")
        aug = make_http_chat_work_assistant_tool_input_augmenter(
            input_data={},
            row={"tenant_id": "t1", "user_id": "u1"},
            extras={"user_email": "a@b.com", "user_jwt": "jwt1"},
            tenant_id=None,
            chat_identity_tool_ids=frozenset({id(t)}),
        )
        out = aug(
            t,
            {
                "tenant_id": None,
                "user_jwt": None,
                "email": None,
                "user_uid": None,
                "user_email": None,
            },
        )
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("tenant_id"), "t1")
        self.assertEqual(out.get("user_jwt"), "jwt1")
        self.assertEqual(out.get("email"), "a@b.com")
        self.assertEqual(out.get("user_uid"), "u1")

    def test_string_args_json_parsed_and_merged(self) -> None:
        t = _Tool("get_current_user", "work-assistant")
        aug = make_http_chat_work_assistant_tool_input_augmenter(
            input_data={},
            row={},
            extras={"tenant_id": "tx", "user_jwt": "j"},
            tenant_id=None,
            chat_identity_tool_ids=frozenset({id(t)}),
        )
        out = aug(
            t,
            {
                "type": "tool_call",
                "name": "get_current_user",
                "args": '{"tenant_id":null}',
                "id": "1",
            },
        )
        self.assertEqual(out.get("type"), "tool_call")
        args = out.get("args")
        self.assertIsInstance(args, dict)
        assert isinstance(args, dict)
        self.assertEqual(args.get("tenant_id"), "tx")
        self.assertEqual(args.get("user_jwt"), "j")


if __name__ == "__main__":
    unittest.main()
