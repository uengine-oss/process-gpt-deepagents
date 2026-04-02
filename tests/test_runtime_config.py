from __future__ import annotations

import os
import unittest

from runtime_config import resolve_model_routing


class RuntimeConfigTests(unittest.TestCase):
    def test_proxy_mode_when_base_url_set(self) -> None:
        prev = os.environ.get("LLM_PROXY_URL")
        os.environ["LLM_PROXY_URL"] = "http://litellm:4000/v1"
        try:
            cfg = resolve_model_routing({"model": "gpt-4o"})
            self.assertEqual(cfg.route_mode, "proxy")
            self.assertEqual(cfg.model_name, "openai:gpt-4o")
        finally:
            if prev is None:
                os.environ.pop("LLM_PROXY_URL", None)
            else:
                os.environ["LLM_PROXY_URL"] = prev

    def test_direct_fallback_without_base_url(self) -> None:
        prev = os.environ.pop("LLM_PROXY_URL", None)
        try:
            cfg = resolve_model_routing({})
            self.assertEqual(cfg.route_mode, "direct-fallback")
            self.assertTrue(cfg.model_name.startswith("openai:"))
        finally:
            if prev is not None:
                os.environ["LLM_PROXY_URL"] = prev


if __name__ == "__main__":
    unittest.main()

