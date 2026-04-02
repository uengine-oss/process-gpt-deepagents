from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch


class SubagentsRequiredTests(unittest.TestCase):
    def test_execute_raises_without_agents(self) -> None:
        try:
            from deepagents_action_executor import (
                DeepAgentsActionExecutor,
                MissingSubagentsConfigurationError,
            )
        except Exception as exc:  # pragma: no cover
            self.skipTest(str(exc))
            return

        async def run() -> None:
            ex = DeepAgentsActionExecutor()
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
                "deepagents_action_executor.TaggingSafeToolLoader"
            ) as mock_loader:
                mock_loader.return_value.create_tools_from_names.return_value = []
                await ex.execute(ctx, eq)

        with self.assertRaises(MissingSubagentsConfigurationError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
