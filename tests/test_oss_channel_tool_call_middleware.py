from __future__ import annotations

import os
import unittest

from langchain_core.messages import AIMessage

from app.executor.oss_channel_tool_call_middleware import (
    _parse_tool_blocks_from_oss_channel_text,
    create_oss_channel_tool_call_middleware,
)


class OssChannelToolCallMiddlewareTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("LLM_COMPACT_TOOL_RESULT_MODEL_IDS", None)

    def test_parse_read_skill_document_block(self) -> None:
        blob = (
            "<|start|>assistant<|channel|>analysis to=functions.read_skill_document code"
            "<|message|>"
            '{"skill_name":"korea-econ-indicators","document_path":"SKILL.md"}'
            "<|call|>"
        )
        pairs = _parse_tool_blocks_from_oss_channel_text(blob)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "read_skill_document")
        self.assertEqual(
            pairs[0][1],
            {"skill_name": "korea-econ-indicators", "document_path": "SKILL.md"},
        )

    def test_parse_misspelled_message_tag(self) -> None:
        blob = (
            "to=functions.list_skills code<|messsage|>{}<|call|>"
        )
        pairs = _parse_tool_blocks_from_oss_channel_text(blob)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "list_skills")
        self.assertEqual(pairs[0][1], {})

    def test_parse_constrain_header_without_code_or_call(self) -> None:
        blob = (
            "<|start|>assistant<|channel|>commentary to=functions.sandbox_write_and_run "
            "<|constrain|>json<|message|>{\"workspace_id\":\"w\",\"relative_path\":\"x.py\"}"
        )
        pairs = _parse_tool_blocks_from_oss_channel_text(blob)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "sandbox_write_and_run")
        self.assertEqual(pairs[0][1], {"workspace_id": "w", "relative_path": "x.py"})

    def test_middleware_wraps_aimessage_for_frentis(self) -> None:
        os.environ["LLM_COMPACT_TOOL_RESULT_MODEL_IDS"] = "frentis-ai-model"
        mw = create_oss_channel_tool_call_middleware(model_name="anthropic:frentis-ai-model")
        self.assertIsNotNone(mw)
        blob = (
            "to=functions.read_skill_document code<|message|>"
            '{"skill_name":"k","document_path":"SKILL.md"}'
            "<|call|>"
        )
        msg = AIMessage(content=blob)

        class _Req:
            pass

        def _handler(_req: _Req):
            return msg

        out = mw.wrap_model_call(_Req(), _handler)  # type: ignore[arg-type]
        self.assertTrue(hasattr(out, "result"))
        self.assertEqual(len(out.result), 1)
        ai = out.result[0]
        self.assertIsInstance(ai, AIMessage)
        assert isinstance(ai, AIMessage)
        self.assertTrue(ai.tool_calls)
        self.assertEqual(ai.tool_calls[0]["name"], "read_skill_document")

    def test_middleware_none_for_other_models(self) -> None:
        os.environ["LLM_COMPACT_TOOL_RESULT_MODEL_IDS"] = "frentis-ai-model"
        mw = create_oss_channel_tool_call_middleware(model_name="anthropic:claude-opus-4-6")
        self.assertIsNone(mw)


if __name__ == "__main__":
    unittest.main()
