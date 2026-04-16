from __future__ import annotations

import os
import unittest

from langchain_core.messages import ToolMessage

from app.executor.compact_tool_result_middleware import (
    create_compact_tool_result_middleware,
    should_compact_tool_results_for_model,
)


class CompactToolResultMiddlewareTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in (
            "LLM_COMPACT_TOOL_RESULT_MODEL_IDS",
            "LLM_COMPACT_TOOL_RESULT_MAX_CHARS",
        ):
            os.environ.pop(key, None)

    def test_should_compact_default_frentis(self) -> None:
        self.assertTrue(should_compact_tool_results_for_model("anthropic:frentis-ai-model"))
        self.assertTrue(should_compact_tool_results_for_model("frentis-ai-model"))

    def test_should_compact_env_override(self) -> None:
        os.environ["LLM_COMPACT_TOOL_RESULT_MODEL_IDS"] = "custom-oss"
        self.assertTrue(should_compact_tool_results_for_model("openai:custom-oss"))
        self.assertFalse(should_compact_tool_results_for_model("anthropic:claude-opus-4-6"))

    def test_middleware_truncates_tool_message(self) -> None:
        os.environ["LLM_COMPACT_TOOL_RESULT_MODEL_IDS"] = "frentis-ai-model"
        os.environ["LLM_COMPACT_TOOL_RESULT_MAX_CHARS"] = "100"
        mw = create_compact_tool_result_middleware(model_name="anthropic:frentis-ai-model")
        self.assertIsNotNone(mw)

        class _Req:
            pass

        long_body = "x" * 500

        def _handler(_req: _Req) -> ToolMessage:
            return ToolMessage(content=long_body, name="find_helpful_skills", tool_call_id="tc1")

        out = mw.wrap_tool_call(_Req(), _handler)  # type: ignore[arg-type]
        self.assertIsInstance(out, ToolMessage)
        assert isinstance(out, ToolMessage)
        text = out.content if isinstance(out.content, str) else str(out.content)
        self.assertLess(len(text), len(long_body))
        self.assertIn("[[COMPACTED]]", text)

    def test_middleware_none_for_other_models(self) -> None:
        os.environ["LLM_COMPACT_TOOL_RESULT_MODEL_IDS"] = "frentis-ai-model"
        mw = create_compact_tool_result_middleware(model_name="anthropic:claude-opus-4-6")
        self.assertIsNone(mw)


if __name__ == "__main__":
    unittest.main()
