import unittest

from app.openui_hitl_events import (
    OPENUI_HITL_SCHEMA_VERSION,
    build_openui_lang_for_hitl,
    expand_input_required_to_openui_hitl_events,
)
from app.hitl_events import (
    question_id_from_run_state,
    register_run_state_for_resume,
    resolve_cached_run_state_for_question_id,
)


class TestOpenuiHitlEvents(unittest.TestCase):
    def test_build_lang_m1_from_run_state_function_dsl_only(self) -> None:
        lang = 'root = Column()\napproval = RadioGroup(label="삭제 승인", options=["승인", "보류"])\nsubmit = Button(label="제출")\n'
        out = build_openui_lang_for_hitl(
            question="ignored",
            context="ignored",
            run_state={"openui_lang": lang},
        )
        self.assertEqual(out.strip(), lang.strip())

    def test_build_lang_rejects_incompatible_custom_and_falls_back(self) -> None:
        lang = "root = confirm_delete\n\ncomponent confirm_delete {\n  type: form\n}\n"
        out = build_openui_lang_for_hitl(
            question="Q1",
            context="C1",
            run_state={"openui_lang": lang},
        )
        self.assertIn("root = Column()", out)
        self.assertIn("RadioGroup", out)

    def test_expand_emits_delta_end_and_registers_resume(self) -> None:
        qid = "openui-test-qid-99"
        # Best-effort: ensure a clean cache; resolve_cached... should return None first.
        self.assertIsNone(resolve_cached_run_state_for_question_id(qid))

        evt = {
            "type": "input_required",
            "conversation_id": "conv-openui-1",
            "question": "Hello?",
            "run_state": {"correlation_id": qid, "reason": "need input"},
        }
        rows = expand_input_required_to_openui_hitl_events(evt)
        self.assertIsInstance(rows, list)
        assert isinstance(rows, list)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "openui")
        self.assertEqual(rows[0]["op"], "delta")
        self.assertEqual(rows[0]["schema_version"], OPENUI_HITL_SCHEMA_VERSION)
        self.assertEqual(rows[0]["seq"], 0)
        self.assertIn("openui_lang_chunk", rows[0])
        self.assertEqual(rows[0]["question"], "Hello?")
        self.assertEqual(rows[-1]["type"], "openui")
        self.assertEqual(rows[-1]["op"], "end")
        self.assertTrue(rows[-1].get("finished"))
        self.assertIn("openui_lang", rows[-1])
        rs = resolve_cached_run_state_for_question_id(qid)
        self.assertIsInstance(rs, dict)
        assert isinstance(rs, dict)
        self.assertEqual(rs.get("reason"), "need input")

        rows2 = expand_input_required_to_openui_hitl_events(evt)
        self.assertEqual(len(rows2), 1)
        self.assertEqual(rows2[0]["type"], "input_required")

    def test_hitl_question_id_matches_ask_user(self) -> None:
        rs = {"correlation_id": "abc"}
        self.assertEqual(question_id_from_run_state(rs), "abc")

    def test_register_hitl_idempotent(self) -> None:
        qid = "reg-test-88"
        self.assertTrue(
            register_run_state_for_resume(
                question_id=qid,
                conversation_id="c",
                run_state={"x": 1},
            )
        )
        self.assertFalse(
            register_run_state_for_resume(
                question_id=qid,
                conversation_id="c",
                run_state={"x": 2},
            )
        )


if __name__ == "__main__":
    unittest.main()
