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

