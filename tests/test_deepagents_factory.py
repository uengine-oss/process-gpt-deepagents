from __future__ import annotations

import unittest
from unittest.mock import patch

class DeepAgentsFactoryTests(unittest.TestCase):
    def test_build_runtime_agent_calls_create_deep_agent(self) -> None:
        try:
            import app.agent.factory as deepagents_factory
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"deepagents_factory import unavailable: {exc}")
            return

        with patch("app.agent.factory.create_deep_agent") as create_mock:
            create_mock.return_value = object()
            agent = deepagents_factory.build_runtime_agent(
                model_name="anthropic:claude-opus-4-6",
                subagents=[{"name": "a", "description": "d", "system_prompt": "s", "tools": []}],
                tools=[],
                middleware=[],
            )
            self.assertIsNotNone(agent)
            self.assertTrue(create_mock.called)
            _, kwargs = create_mock.call_args
            self.assertIn("backend", kwargs)
            self.assertTrue(callable(kwargs["backend"]))
            self.assertIn("skills", kwargs)
            self.assertIn("memory", kwargs)
            self.assertIn("checkpointer", kwargs)
            self.assertIn("store", kwargs)
            self.assertEqual(kwargs.get("model"), "anthropic:claude-opus-4-6")

    def test_proxy_routing_passes_chat_openai_model(self) -> None:
        try:
            import app.agent.factory as deepagents_factory
            from app.runtime_config import ModelRoutingConfig
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"deepagents_factory import unavailable: {exc}")
            return

        routing = ModelRoutingConfig(
            model_name="anthropic:claude-opus-4-6",
            route_mode="proxy",
            openai_base_url="http://litellm:4000/v1",
            proxy_api_key="sk-virtual",
        )
        fake_llm = object()
        with (
            patch("app.agent.factory.create_deep_agent") as create_mock,
            patch("langchain_openai.ChatOpenAI", return_value=fake_llm) as chat_mock,
        ):
            create_mock.return_value = object()
            deepagents_factory.build_runtime_agent(
                model_name="anthropic:claude-opus-4-6",
                subagents=[],
                tools=[],
                middleware=[],
                model_routing=routing,
            )
            chat_mock.assert_called_once()
            _, ckwargs = chat_mock.call_args
            self.assertEqual(ckwargs.get("model"), "claude-opus-4-6")
            self.assertEqual(ckwargs.get("base_url"), "http://litellm:4000/v1")
            self.assertEqual(ckwargs.get("api_key"), "sk-virtual")
            _, kwargs = create_mock.call_args
            self.assertIs(kwargs.get("model"), fake_llm)

    def test_default_orchestrator_prompt_instructs_native_task_delegation(self) -> None:
        try:
            from app.agent.factory import default_orchestrator_prompt
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"deepagents_factory import unavailable: {exc}")
            return

        p = default_orchestrator_prompt()
        self.assertIn("task()", p)
        self.assertIn("서브에이전트", p)
        self.assertIn("DeepAgents", p)


if __name__ == "__main__":
    unittest.main()

