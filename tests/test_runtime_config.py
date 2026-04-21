from __future__ import annotations

import os
import unittest

from app.runtime_config import normalize_litellm_slash_model_name, resolve_model_routing


class NormalizeLitellmSlashModelNameTests(unittest.TestCase):
    def test_openai_slash_to_colon(self) -> None:
        self.assertEqual(normalize_litellm_slash_model_name("openai/gpt-4.1"), "openai:gpt-4.1")

    def test_anthropic_slash(self) -> None:
        self.assertEqual(
            normalize_litellm_slash_model_name("anthropic/claude-sonnet-4-20250514"),
            "anthropic:claude-sonnet-4-20250514",
        )

    def test_already_colon_unchanged(self) -> None:
        self.assertEqual(normalize_litellm_slash_model_name("openai:gpt-4o"), "openai:gpt-4o")

    def test_multi_slash_unchanged(self) -> None:
        s = "accounts/fireworks/models/llama-v3p1-8b-instruct"
        self.assertIs(normalize_litellm_slash_model_name(s), s)

    def test_unknown_prefix_unchanged(self) -> None:
        s = "custom-router/my-model"
        self.assertIs(normalize_litellm_slash_model_name(s), s)

    def test_none(self) -> None:
        self.assertIsNone(normalize_litellm_slash_model_name(None))


class RuntimeConfigTests(unittest.TestCase):
    def test_proxy_mode_when_url_and_key_set(self) -> None:
        prev_url = os.environ.get("LLM_PROXY_URL")
        prev_key = os.environ.get("LLM_PROXY_API_KEY")
        prev_openai = os.environ.get("OPENAI_API_KEY")
        prev_model = os.environ.pop("LLM_MODEL", None)
        os.environ["LLM_PROXY_URL"] = "http://litellm:4000/v1"
        os.environ["LLM_PROXY_API_KEY"] = "sk-virtual-test"
        try:
            if "OPENAI_API_KEY" in os.environ:
                del os.environ["OPENAI_API_KEY"]
            cfg = resolve_model_routing({"model": "anthropic:claude-opus-4-6"})
            self.assertEqual(cfg.route_mode, "proxy")
            self.assertEqual(cfg.openai_base_url, "http://litellm:4000/v1")
            self.assertEqual(cfg.proxy_api_key, "sk-virtual-test")
            self.assertEqual(cfg.model_name, "anthropic:claude-opus-4-6")
        finally:
            self._restore_env("LLM_PROXY_URL", prev_url)
            self._restore_env("LLM_PROXY_API_KEY", prev_key)
            self._restore_env("OPENAI_API_KEY", prev_openai)
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model

    def test_openai_api_key_used_when_proxy_key_unset(self) -> None:
        prev_url = os.environ.get("LLM_PROXY_URL")
        prev_proxy_key = os.environ.get("LLM_PROXY_API_KEY")
        prev_openai = os.environ.get("OPENAI_API_KEY")
        prev_model = os.environ.pop("LLM_MODEL", None)
        os.environ["LLM_PROXY_URL"] = "http://litellm:4000/v1"
        os.environ.pop("LLM_PROXY_API_KEY", None)
        os.environ["OPENAI_API_KEY"] = "sk-from-openai-env"
        try:
            cfg = resolve_model_routing({})
            self.assertEqual(cfg.route_mode, "proxy")
            self.assertEqual(cfg.proxy_api_key, "sk-from-openai-env")
        finally:
            self._restore_env("LLM_PROXY_URL", prev_url)
            self._restore_env("LLM_PROXY_API_KEY", prev_proxy_key)
            self._restore_env("OPENAI_API_KEY", prev_openai)
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model

    @staticmethod
    def _restore_env(name: str, prev: str | None) -> None:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev

    def test_direct_fallback_when_proxy_url_but_no_key(self) -> None:
        prev_url = os.environ.get("LLM_PROXY_URL")
        prev_proxy_key = os.environ.get("LLM_PROXY_API_KEY")
        prev_openai = os.environ.get("OPENAI_API_KEY")
        prev_model = os.environ.pop("LLM_MODEL", None)
        os.environ["LLM_PROXY_URL"] = "http://litellm:4000/v1"
        os.environ.pop("LLM_PROXY_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            cfg = resolve_model_routing({"model": "anthropic:claude-opus-4-6"})
            self.assertEqual(cfg.route_mode, "direct")
            self.assertIsNone(cfg.openai_base_url)
            self.assertIsNone(cfg.proxy_api_key)
            self.assertEqual(cfg.model_name, "anthropic:claude-opus-4-6")
        finally:
            self._restore_env("LLM_PROXY_URL", prev_url)
            self._restore_env("LLM_PROXY_API_KEY", prev_proxy_key)
            self._restore_env("OPENAI_API_KEY", prev_openai)
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model

    def test_direct_fallback_without_proxy_url(self) -> None:
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

    def test_resolve_normalizes_litellm_slash_in_payload(self) -> None:
        prev_url = os.environ.pop("LLM_PROXY_URL", None)
        prev_key = os.environ.pop("LLM_PROXY_API_KEY", None)
        prev_openai = os.environ.pop("OPENAI_API_KEY", None)
        prev_model = os.environ.pop("LLM_MODEL", None)
        try:
            cfg = resolve_model_routing({"model": "openai/gpt-4.1"})
            self.assertEqual(cfg.model_name, "openai:gpt-4.1")
        finally:
            if prev_url is not None:
                os.environ["LLM_PROXY_URL"] = prev_url
            if prev_key is not None:
                os.environ["LLM_PROXY_API_KEY"] = prev_key
            if prev_openai is not None:
                os.environ["OPENAI_API_KEY"] = prev_openai
            if prev_model is not None:
                os.environ["LLM_MODEL"] = prev_model


if __name__ == "__main__":
    unittest.main()
