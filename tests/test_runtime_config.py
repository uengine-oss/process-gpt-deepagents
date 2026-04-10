from __future__ import annotations

import os
import unittest

from app.runtime_config import resolve_model_routing


class RuntimeConfigTests(unittest.TestCase):
    def test_proxy_env_is_ignored(self) -> None:
        prev = os.environ.get("LLM_PROXY_URL")
        prev_model = os.environ.pop("LLM_MODEL", None)
        prev_openai_key = os.environ.get("OPENAI_API_KEY")
        prev_openai_base = os.environ.get("OPENAI_BASE_URL")
        os.environ["LLM_PROXY_URL"] = "http://litellm:4000/v1"
        try:
            cfg = resolve_model_routing({"model": "anthropic:claude-opus-4-6"})
            self.assertEqual(cfg.route_mode, "direct")
            self.assertEqual(cfg.openai_base_url, None)
            self.assertEqual(cfg.model_name, "anthropic:claude-opus-4-6")
            # Runtime must not mutate OpenAI envs.
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), prev_openai_key)
            self.assertEqual(os.environ.get("OPENAI_BASE_URL"), prev_openai_base)
        finally:
            if prev is None:
                os.environ.pop("LLM_PROXY_URL", None)
            else:
                os.environ["LLM_PROXY_URL"] = prev
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model
            if prev_openai_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_openai_key
            if prev_openai_base is None:
                os.environ.pop("OPENAI_BASE_URL", None)
            else:
                os.environ["OPENAI_BASE_URL"] = prev_openai_base

    def test_direct_fallback_without_base_url(self) -> None:
        prev = os.environ.pop("LLM_PROXY_URL", None)
        prev_model = os.environ.pop("LLM_MODEL", None)
        try:
            cfg = resolve_model_routing({})
            self.assertEqual(cfg.route_mode, "direct")
            self.assertEqual(cfg.model_name, "anthropic:claude-opus-4-6")
        finally:
            if prev is not None:
                os.environ["LLM_PROXY_URL"] = prev
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model


if __name__ == "__main__":
    unittest.main()

