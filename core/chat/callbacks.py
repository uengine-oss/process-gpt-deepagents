"""LangChain async callback handler for tool/skill tracking in chat sessions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from langchain_core.callbacks.base import AsyncCallbackHandler

from core.skills.tools import _skills_from_tool_args


_SENSITIVE_KEY = re.compile(r"(^|_)(authorization|password|passwd|secret|token|jwt|api_?key|access_?key)($|_)", re.I)


def _safe_preview(value: Any, depth: int = 0) -> Any:
    """Keep execution details useful without streaming secrets or huge file bodies."""
    if depth > 8:
        return "[omitted]"
    if isinstance(value, str):
        return value if len(value) <= 6000 else value[:6000] + "\n…(truncated)"
    if isinstance(value, list):
        return [_safe_preview(v, depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        return {
            str(k): "[redacted]" if _SENSITIVE_KEY.search(str(k)) else _safe_preview(v, depth + 1)
            for k, v in list(value.items())[:100]
        }
    return value


class ToolTrackingCallback(AsyncCallbackHandler):
    """Captures tool and skill usage from both root agent and subagents.

    Subagents are invoked via ainvoke (not astream), so their tool calls never
    appear in the parent graph's event stream. LangGraph propagates the parent
    config (including callbacks) into subagent runs via ensure_config, so this
    callback fires for every tool execution regardless of nesting depth.

    push_ctx is a synchronous callable (CtxAccumulator.push) — no tasks, no
    async DB writes here. All persistence is deferred to a single write after
    the streaming loop finishes.
    """

    def __init__(
        self,
        *,
        streamer: Any,
        push_ctx: Callable[[dict], None],
        mcp_tool_servers: dict[str, list[str]] | None = None,
    ) -> None:
        self._streamer = streamer
        self._push_ctx = push_ctx
        self._run_to_tool: dict[str, str] = {}
        self._mcp_tool_servers: dict[str, list[str]] = mcp_tool_servers or {}
        self._emitted_connectors: set[str] = set()

    async def _emit(self, payload: dict) -> None:
        if self._streamer:
            await self._streamer.send_json(payload)
        self._push_ctx(payload)

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        run_id: Any,
        inputs: dict | None = None,
        **kwargs: Any,
    ) -> None:
        tool_name = (serialized.get("name") or "").strip()
        if not tool_name:
            return
        self._run_to_tool[str(run_id)] = tool_name
        servers = self._mcp_tool_servers.get(tool_name)

        args: dict = inputs if isinstance(inputs, dict) else {}
        if not args and isinstance(input_str, str):
            try:
                parsed = json.loads(input_str)
                if isinstance(parsed, dict):
                    args = parsed
            except Exception:
                pass

        await self._emit({
            "type": "tool_start",
            "tool": tool_name,
            "input": _safe_preview(args) if args else None,
            "connectors": list(servers or []),
        })

        if servers:
            new = [s for s in servers if s not in self._emitted_connectors]
            if new:
                self._emitted_connectors.update(new)
                await self._emit({"type": "plan_connectors", "connectors": new})

        skill_names = _skills_from_tool_args(args)
        if skill_names:
            await self._emit({"type": "plan_skills", "skills": skill_names})

        if tool_name == "write_todos":
            todos = args.get("todos") if isinstance(args, dict) else None
            if isinstance(todos, list):
                await self._emit({"type": "plan_todos", "todos": todos})

    async def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        tool_name = self._run_to_tool.pop(str(run_id), "")
        if tool_name:
            result_str = output if isinstance(output, str) else str(output)
            result_str = _safe_preview(result_str)
            await self._emit({"type": "tool_end", "tool": tool_name, "output": result_str})
