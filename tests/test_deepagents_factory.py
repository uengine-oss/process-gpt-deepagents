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
                model_name="openai:gpt-4o",
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


if __name__ == "__main__":
    unittest.main()

