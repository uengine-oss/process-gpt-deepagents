"""Tests for todo list normalization helpers in workitem_text."""

from __future__ import annotations

import json
import unittest


class TestNormalizedTodoEntries(unittest.TestCase):
    def test_join_matches_normalized(self) -> None:
        from app.executor.workitem_text import _join_todo_contents, _normalized_todo_entries

        raw = {
            "todos": [
                {"content": "a", "status": "pending"},
                {"content": "  ", "status": "pending"},
                {"content": "b", "status": "in_progress"},
            ]
        }
        self.assertEqual(_normalized_todo_entries(raw), ["a", "b"])
        self.assertEqual(_join_todo_contents(raw), "a\nb")


class TestWorkitemEchoStreamCut(unittest.TestCase):
    def test_strips_template_then_answer(self) -> None:
        from app.executor.workitem_text import strip_leading_workitem_echo_for_sse

        body = {"tenant_id": "t1", "message": "hi"}
        t = (
            "[Description]\nHTTP 채팅 요청\n\n[Instruction]\nhi\n\n[InputData]\n"
            + json.dumps(body, ensure_ascii=False)
            + "\n\n실제 답변입니다."
        )
        self.assertEqual(strip_leading_workitem_echo_for_sse(t).lstrip(), "실제 답변입니다.")

    def test_incomplete_inputdata_json_hides_all(self) -> None:
        from app.executor.workitem_text import cut_index_after_leading_workitem_echo_for_stream

        t = "[Description]\nD\n\n[Instruction]\nI\n\n[InputData]\n{\"a\":"
        self.assertEqual(cut_index_after_leading_workitem_echo_for_stream(t), len(t))

    def test_plain_answer_not_stripped(self) -> None:
        from app.executor.workitem_text import cut_index_after_leading_workitem_echo_for_stream

        self.assertEqual(cut_index_after_leading_workitem_echo_for_stream("그냥 답변"), 0)


if __name__ == "__main__":
    unittest.main()
