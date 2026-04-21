import unittest


class TestAskUserEventTransform(unittest.TestCase):
    def test_select_items_when_structured_items_exist(self) -> None:
        from app.ask_user_events import transform_input_required_to_ask_user_event

        evt = {
            "type": "input_required",
            "conversation_id": "c1",
            "question": "목록에서 선택하세요",
            "items": [
                {"id": "a", "label": "A", "description": "desc"},
                {"id": "b", "label": "B"},
            ],
            "run_state": {"correlation_id": "qid-1", "reason": "need pick"},
        }
        out = transform_input_required_to_ask_user_event(evt)
        self.assertIsInstance(out, dict)
        assert isinstance(out, dict)
        self.assertEqual(out["type"], "ask_user")
        self.assertEqual(out["user_request_type"], "select_items")
        self.assertEqual(out["conversation_id"], "c1")
        self.assertEqual(out["question_id"], "qid-1")
        self.assertEqual(out["question"], "목록에서 선택하세요")
        self.assertIn("items", out)
        self.assertTrue(isinstance(out["items"], list) and len(out["items"]) == 2)

    def test_suggestions_when_suggestions_exist(self) -> None:
        from app.ask_user_events import transform_input_required_to_ask_user_event

        evt = {
            "type": "input_required",
            "conversation_id": "c2",
            "question": "원하는 옵션을 고르세요",
            "suggestions": ["옵션1", "옵션2"],
            "run_state": {"correlation_id": "qid-2"},
        }
        out = transform_input_required_to_ask_user_event(evt)
        self.assertIsInstance(out, dict)
        assert isinstance(out, dict)
        self.assertEqual(out["type"], "ask_user")
        self.assertEqual(out["user_request_type"], "suggestions")
        self.assertEqual(out["question_id"], "qid-2")
        self.assertEqual(out["suggestions"], ["옵션1", "옵션2"])

    def test_default_approve_reject_with_edit(self) -> None:
        from app.ask_user_events import transform_input_required_to_ask_user_event

        evt = {
            "type": "input_required",
            "conversation_id": "c3",
            "question": "추가 정보를 입력해 주세요",
            "run_state": {"checkpoint_thread_id": "th-3"},
        }
        out = transform_input_required_to_ask_user_event(evt)
        self.assertIsInstance(out, dict)
        assert isinstance(out, dict)
        self.assertEqual(out["type"], "ask_user")
        self.assertEqual(out["user_request_type"], "approve_reject_with_edit")
        self.assertEqual(out["question_id"], "th-3")

    def test_idempotent_by_question_id(self) -> None:
        from app.ask_user_events import transform_input_required_to_ask_user_event

        evt = {
            "type": "input_required",
            "conversation_id": "c4",
            "question": "진행할까요?",
            "run_state": {"correlation_id": "qid-4"},
        }
        out1 = transform_input_required_to_ask_user_event(evt)
        out2 = transform_input_required_to_ask_user_event(evt)
        self.assertIsNotNone(out1)
        self.assertIsNone(out2)

    def test_cached_run_state_resolves_by_question_id(self) -> None:
        from app.ask_user_events import resolve_cached_run_state_for_question_id, transform_input_required_to_ask_user_event

        evt = {
            "type": "input_required",
            "conversation_id": "c5",
            "question": "q",
            "run_state": {"correlation_id": "qid-5", "pending_field": "human_response"},
        }
        out = transform_input_required_to_ask_user_event(evt)
        self.assertIsNotNone(out)
        rs = resolve_cached_run_state_for_question_id("qid-5")
        self.assertIsInstance(rs, dict)
        assert isinstance(rs, dict)
        self.assertEqual(rs.get("pending_field"), "human_response")


if __name__ == "__main__":
    unittest.main()

