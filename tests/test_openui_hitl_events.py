import unittest

from app.openui_hitl_events import (
    OPENUI_HITL_SCHEMA_VERSION,
    _rewrite_nested_card_inline_to_named_assignments,
    build_openui_lang_for_hitl,
    expand_input_required_to_openui_hitl_events,
    run_state_without_openui_lang,
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

    def test_build_lang_m1_card_form_dsl_passthrough(self) -> None:
        lang = (
            "root = Card([header, formBlock])\n"
            'header = CardHeader("요청 접수", "예시")\n'
            'formBlock = Form([nameField], "설명", "제출")\n'
            'nameField = Input("requestTitle", "제목", "")\n'
        )
        out = build_openui_lang_for_hitl(
            question="ignored",
            context="ignored",
            run_state={"openui_lang": lang},
        )
        self.assertEqual(out.strip(), lang.strip())

    def test_named_kwargs_normalized_to_positional(self) -> None:
        lang = (
            "root = Card([header, formBlock])\n"
            'header = CardHeader(title="제목", subtitle="부제")\n'
            'formBlock = Form(fields=[nameField], description="설명", submitLabel="전송")\n'
            'nameField = Input(name="rid", label="제목", default="")\n'
        )
        out = build_openui_lang_for_hitl(
            question="Q",
            context="C",
            run_state={"openui_lang": lang},
        )
        self.assertIn('CardHeader("제목", "부제")', out)
        self.assertIn('Form([nameField], "설명", "전송")', out)
        self.assertIn('Input("rid", "제목", "")', out)
        self.assertNotIn("title=", out)
        self.assertNotIn("name=", out)

    def test_build_lang_rejects_incompatible_custom_and_falls_back(self) -> None:
        lang = "root = confirm_delete\n\ncomponent confirm_delete {\n  type: form\n}\n"
        out = build_openui_lang_for_hitl(
            question="Q1",
            context="C1",
            run_state={"openui_lang": lang},
        )
        self.assertIn("root = Card([header, formBlock])", out)
        self.assertIn("Form([questionField", out)
        self.assertIn("hitl_approval", out)

    def test_bare_card_gets_root_prefix(self) -> None:
        lang = (
            'Card([header, formBlock])\n'
            'header = CardHeader("A", "B")\n'
            'formBlock = Form([nameField], "d", "s")\n'
            'nameField = Input("id", "L", "")\n'
        )
        out = build_openui_lang_for_hitl(
            question="Q",
            context="C",
            run_state={"openui_lang": lang},
        )
        self.assertTrue(out.strip().startswith("root = "))

    def test_rewrite_nested_card_to_named_assignments(self) -> None:
        nested = (
            "root = Card(\n"
            '  CardHeader("product 테이블 삭제 확인", "Supabase DB에서 테이블 삭제 전 최종 확인이 필요합니다."),\n'
            "  Form(\n"
            '    Select("environment", "대상 환경", {"local": "로컬"}, "dev", "hint"),\n'
            '    Input("project_ref", "프로젝트", "예", "도움")\n'
            "  )\n"
            ")\n"
        )
        out = _rewrite_nested_card_inline_to_named_assignments(nested)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertIn("root = Card([hdr, frm])", out)
        self.assertIn("hdr = CardHeader(", out)
        self.assertIn("frm = Form([env, proj]", out)
        self.assertIn("env = Select(", out)
        self.assertIn("proj = Input(", out)

    def test_expand_prefers_evt_openui_lang_over_stripped_run_state(self) -> None:
        qid = "openui-evt-lang-1"
        self.assertIsNone(resolve_cached_run_state_for_question_id(qid))
        canonical = 'root = Card([h])\nh = CardHeader("X", "Y")\n'
        evt = {
            "type": "input_required",
            "conversation_id": "conv-evt-lang-1",
            "question": "ignored for lang",
            "run_state": {"correlation_id": qid},
            "openui_lang": canonical,
        }
        rows = expand_input_required_to_openui_hitl_events(evt)
        assert isinstance(rows, list)
        self.assertEqual(rows[-1].get("openui_lang"), canonical.strip())

    def test_expand_strips_openui_lang_from_run_state_payloads(self) -> None:
        qid = "openui-strip-lang-1"
        self.assertIsNone(resolve_cached_run_state_for_question_id(qid))
        junk = 'Card(CardHeader("a","b"))'
        evt = {
            "type": "input_required",
            "conversation_id": "conv-strip-1",
            "question": "Hi?",
            "run_state": {"correlation_id": qid, "openui_lang": junk, "reason": "x"},
        }
        rows = expand_input_required_to_openui_hitl_events(evt)
        assert isinstance(rows, list)
        self.assertGreaterEqual(len(rows), 2)
        rs0 = rows[0].get("run_state")
        self.assertIsInstance(rs0, dict)
        assert isinstance(rs0, dict)
        self.assertNotIn("openui_lang", rs0)
        self.assertEqual(rs0.get("reason"), "x")
        rsend = rows[-1].get("run_state")
        self.assertIsInstance(rsend, dict)
        assert isinstance(rsend, dict)
        self.assertNotIn("openui_lang", rsend)
        self.assertTrue(str(rows[-1].get("openui_lang") or "").strip().startswith("root = "))

    def test_run_state_without_openui_lang_none(self) -> None:
        self.assertIsNone(run_state_without_openui_lang(None))

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
