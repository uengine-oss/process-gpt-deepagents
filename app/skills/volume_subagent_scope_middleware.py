"""While the orchestrator runs task(subagent_type=…), scope skill-volume events to that subagent job."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from langchain_core.messages import ToolMessage

from app.skills.volume_tool_events import (
    skill_volume_active_exec_ctx,
    skill_volume_active_job_id,
    skill_volume_tool_event_binding,
)


def _tool_call_parts(request: ToolCallRequest) -> tuple[str | None, dict[str, Any]]:
    tc = request.tool_call
    if isinstance(tc, dict):
        name = tc.get("name")
        raw_args = tc.get("args") or {}
    else:
        name = getattr(tc, "name", None)
        raw_args = getattr(tc, "args", None) or {}
    if not isinstance(raw_args, dict):
        try:
            raw_args = dict(raw_args)
        except Exception:
            raw_args = {}
    return (name if isinstance(name, str) else None), raw_args


class SubagentJobScopeMiddleware(AgentMiddleware):
    """Set ContextVars so InstrumentingSkillVolumeBackend events use the subagent job_id/exec_ctx."""

    @staticmethod
    def _tool_call_id(request: ToolCallRequest) -> str:
        tc = request.tool_call
        if isinstance(tc, dict):
            cid = tc.get("id")
        else:
            cid = getattr(tc, "id", None)
        return str(cid).strip() if cid else ""

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        name, args = _tool_call_parts(request)
        binding = skill_volume_tool_event_binding.get()
        tokens: list[tuple[Any, Any]] = []
        try:
            if name == "task" and binding is not None:
                sub_type = str(args.get("subagent_type") or "").strip()
                if sub_type:
                    mapped = str(binding.subagent_name_to_job_id.get(sub_type) or "").strip()
                    jid = mapped or self._tool_call_id(request)
                    ect = binding.subagent_name_to_exec_ctx.get(sub_type)
                    if jid:
                        tokens.append((skill_volume_active_job_id, skill_volume_active_job_id.set(jid)))
                    if ect is not None:
                        tokens.append((skill_volume_active_exec_ctx, skill_volume_active_exec_ctx.set(ect)))
            return handler(request)
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        name, args = _tool_call_parts(request)
        binding = skill_volume_tool_event_binding.get()
        tokens: list[tuple[Any, Any]] = []
        try:
            if name == "task" and binding is not None:
                sub_type = str(args.get("subagent_type") or "").strip()
                if sub_type:
                    mapped = str(binding.subagent_name_to_job_id.get(sub_type) or "").strip()
                    jid = mapped or self._tool_call_id(request)
                    ect = binding.subagent_name_to_exec_ctx.get(sub_type)
                    if jid:
                        tokens.append((skill_volume_active_job_id, skill_volume_active_job_id.set(jid)))
                    if ect is not None:
                        tokens.append((skill_volume_active_exec_ctx, skill_volume_active_exec_ctx.set(ect)))
            return await handler(request)
        finally:
            for var, tok in reversed(tokens):
                var.reset(tok)
