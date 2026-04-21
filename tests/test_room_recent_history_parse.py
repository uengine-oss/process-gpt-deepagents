from __future__ import annotations

import unittest

from app.http_chat import parse_room_recent_history_to_messages


class TestParseRoomRecentHistoryToMessages(unittest.TestCase):
    def test_parses_roles_and_content(self) -> None:
        txt = (
            "[user][홍길동]: 구글 스프레드시트 목록 조회해줘.\n"
            "[assistant][Process GPT Agent][agentId=process-gpt-agent]: 권한이 부족합니다.\n"
            "[user][홍길동]: 다시 시도\n"
        )
        msgs = parse_room_recent_history_to_messages(txt, max_messages=10, max_total_chars=9999)
        self.assertEqual(
            msgs,
            [
                {"role": "user", "content": "구글 스프레드시트 목록 조회해줘."},
                {"role": "assistant", "content": "권한이 부족합니다."},
                {"role": "user", "content": "다시 시도"},
            ],
        )

    def test_dedupes_exact_duplicates(self) -> None:
        txt = (
            "[assistant][Process GPT Agent][agentId=process-gpt-agent]: 같은 문장\n"
            "[assistant][Process GPT Agent][agentId=process-gpt-agent]: 같은 문장\n"
        )
        msgs = parse_room_recent_history_to_messages(txt)
        self.assertEqual(msgs, [{"role": "assistant", "content": "같은 문장"}])

    def test_truncates_total_chars(self) -> None:
        txt = "[user][A]: " + ("x" * 100) + "\n"
        msgs = parse_room_recent_history_to_messages(txt, max_total_chars=10)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(len(msgs[0]["content"]), 10)


if __name__ == "__main__":
    unittest.main()

