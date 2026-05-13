from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch


class SubagentsRequiredTests(unittest.TestCase):
    def test_execute_raises_without_agents(self) -> None:
        try:
            from deepagents_executor import (
                DeepAgentsExecutor,
                MissingSubagentsConfigurationError,
            )
        except Exception as exc:  # pragma: no cover
            self.skipTest(str(exc))
            return

        async def run() -> None:
            ex = DeepAgentsExecutor()
            ctx = MagicMock()
            ctx.get_user_input.return_value = "[Description] x\n[Instruction] y\n[InputData] {}"
            ctx.get_context_data.return_value = {
                "row": {
                    "id": "todo-test-1",
                    "proc_inst_id": "proc-1",
                    "tenant_id": "tenant-1",
                },
                "extras": {"agents": []},
            }
            eq = MagicMock()
            with patch(
                "deepagents_executor.TaggingSafeToolLoader"
            ) as mock_loader:
                mock_loader.return_value.create_tools_from_names.return_value = []
                await ex.execute(ctx, eq)

        with self.assertRaises(MissingSubagentsConfigurationError):
            asyncio.run(run())

    def test_execute_allows_chat_without_agents(self) -> None:
        try:
            from deepagents_executor import DeepAgentsExecutor
        except Exception as exc:  # pragma: no cover
            self.skipTest(str(exc))
            return

        async def run() -> None:
            ex = DeepAgentsExecutor()
            ctx = MagicMock()
            ctx.get_user_input.return_value = "[Description] x\n[Instruction] y\n[InputData] {}"
            ctx.get_context_data.return_value = {
                "row": {
                    "id": "todo-test-2",
                    "proc_inst_id": "proc-2",
                    "tenant_id": "tenant-1",
                },
                "extras": {"agents": [], "request_kind": "chat"},
            }
            eq = MagicMock()
            with patch("deepagents_executor.TaggingSafeToolLoader") as mock_loader:
                mock_loader.return_value.create_tools_from_names.return_value = []
                await ex.execute(ctx, eq)

        # Should not raise MissingSubagentsConfigurationError in chat mode.
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
