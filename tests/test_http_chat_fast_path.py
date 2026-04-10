from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch


class _Ctx:
    def __init__(self, *, query: str, row: dict, extras: dict) -> None:
        self._query = query
        self._row = row
        self._extras = extras

    def get_user_input(self) -> str:  # RequestContext compatibility
        return self._query

    def get_context_data(self) -> dict:
        return {"row": self._row, "extras": self._extras}


class _Queue:
    def enqueue_event(self, event) -> None:  # EventQueue compatibility
        del event


class _FakeOrchestrator:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, state_input):  # DeepAgents graph compatibility
        del state_input
        return {"messages": [{"role": "assistant", "content": self._content}]}


class HttpChatFastPathTests(unittest.TestCase):
    def test_fast_path_handles_simple_chat_without_subagents(self) -> None:
        # Arrange: minimal http_chat extras with empty agents list.
        result_box: dict = {}
        extras = {
            "http_chat": True,
            "tenant_mcp": {"mcpServers": {}},
            "agents": [],
            "_http_chat_result_box": result_box,
        }
        row = {
            "id": "conv-1",
            "tenant_id": "t1",
            "proc_inst_id": "conv-1",
            "root_proc_inst_id": "conv-1",
            "user_id": "",
            "agent_mode": "COMPLETE",
        }
        ctx = _Ctx(query="[Description]\nHTTP\n\n[Instruction]\n안녕?\n\n[InputData]\n{}", row=row, extras=extras)
        q = _Queue()

        # Patch heavy dependencies so the test does not require real MCP/langchain.
        with patch("deepagents_executor._truthy_env", return_value=True), patch(
            "deepagents_executor.TaggingSafeToolLoader.create_tools_from_names",
            return_value=[object()],
        ), patch("deepagents_executor._ensure_langchain_tools", side_effect=lambda x: x), patch(
            "deepagents_executor._instrument_tools", side_effect=lambda tools, **kw: tools
        ), patch(
            "deepagents_executor.build_orchestrator",
            return_value=_FakeOrchestrator("안녕하세요"),
        ), patch(
            "deepagents_executor.build_subagent_configs",
            side_effect=AssertionError("build_subagent_configs should not be called on fast path success"),
        ):
            from deepagents_executor import DeepAgentsExecutor

            # Act
            asyncio.run(DeepAgentsExecutor().execute(ctx, q))  # type: ignore[arg-type]

        # Assert
        self.assertIn("response", result_box)
        self.assertIn("안녕하세요", str(result_box["response"]))

    def test_fast_path_skipped_when_files_present_and_missing_agents_raises(self) -> None:
        # If files exist, fast path is skipped; with no agents configured, executor should raise.
        extras = {
            "http_chat": True,
            "tenant_mcp": {"mcpServers": {}},
            "agents": [],
            "_http_chat_result_box": {},
        }
        row = {
            "id": "conv-2",
            "tenant_id": "t1",
            "proc_inst_id": "conv-2",
            "root_proc_inst_id": "conv-2",
            "user_id": "",
            "agent_mode": "COMPLETE",
        }
        # InputData contains file_count>0
        ctx = _Ctx(
            query='[Description]\nHTTP\n\n[Instruction]\n파일 처리\n\n[InputData]\n{"file_count": 1}',
            row=row,
            extras=extras,
        )
        q = _Queue()

        with patch("deepagents_executor._truthy_env", return_value=True), patch(
            "deepagents_executor.TaggingSafeToolLoader.create_tools_from_names",
            return_value=[],
        ), patch("deepagents_executor._ensure_langchain_tools", side_effect=lambda x: x), patch(
            "deepagents_executor._prefetch_tenant_context_once", return_value={}
        ):
            from deepagents_executor import DeepAgentsExecutor, MissingSubagentsConfigurationError

            with self.assertRaises(MissingSubagentsConfigurationError):
                asyncio.run(DeepAgentsExecutor().execute(ctx, q))  # type: ignore[arg-type]

    def test_fast_path_escalates_on_sentinel_and_missing_agents_raises(self) -> None:
        # Fast path emits escalation JSON; then it should fall back to full path and raise if agents missing.
        extras = {
            "http_chat": True,
            "tenant_mcp": {"mcpServers": {}},
            "agents": [],
            "_http_chat_result_box": {},
        }
        row = {
            "id": "conv-3",
            "tenant_id": "t1",
            "proc_inst_id": "conv-3",
            "root_proc_inst_id": "conv-3",
            "user_id": "",
            "agent_mode": "COMPLETE",
        }
        ctx = _Ctx(query="[Description]\nHTTP\n\n[Instruction]\n복잡한 작업\n\n[InputData]\n{}", row=row, extras=extras)
        q = _Queue()

        escalation_json = (
            '{"needs_escalation": true, "reason": "needs_execution", "sentinel": "__DEEPAGENTS_NEEDS_ESCALATION__"}'
        )

        with patch("deepagents_executor._truthy_env", return_value=True), patch(
            "deepagents_executor.TaggingSafeToolLoader.create_tools_from_names",
            return_value=[object()],
        ), patch("deepagents_executor._ensure_langchain_tools", side_effect=lambda x: x), patch(
            "deepagents_executor._instrument_tools", side_effect=lambda tools, **kw: tools
        ), patch(
            "deepagents_executor.build_orchestrator",
            return_value=_FakeOrchestrator(escalation_json),
        ), patch(
            "deepagents_executor._prefetch_tenant_context_once", return_value={}
        ):
            from deepagents_executor import DeepAgentsExecutor, MissingSubagentsConfigurationError

            with self.assertRaises(MissingSubagentsConfigurationError):
                asyncio.run(DeepAgentsExecutor().execute(ctx, q))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

