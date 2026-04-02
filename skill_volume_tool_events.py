"""Context + helpers to emit tool_usage_* for skill volume (mounted FS) access."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

_MAX_EVENT_CHARS = 12000

skill_volume_tool_event_binding: ContextVar["SkillVolumeToolEventBinding | None"] = ContextVar(
    "skill_volume_tool_event_binding", default=None
)
skill_volume_active_job_id: ContextVar[str | None] = ContextVar("skill_volume_active_job_id", default=None)
skill_volume_active_exec_ctx: ContextVar["Any | None"] = ContextVar(
    "skill_volume_active_exec_ctx", default=None
)


@dataclass
class SkillVolumeToolEventBinding:
    """Per-run metadata for skill-volume FS tool_usage events (events table)."""

    event_queue: Any
    todo_id: str
    proc_inst_id: str
    crew_type: str
    default_job_id: str
    root_exec_ctx: Any
    subagent_name_to_job_id: dict[str, str] = field(default_factory=dict)
    subagent_name_to_exec_ctx: dict[str, Any] = field(default_factory=dict)


def _truncate_for_event(val: Any, max_chars: int = _MAX_EVENT_CHARS) -> Any:
    if val is None:
        return None
    if isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        if len(val) <= max_chars:
            return val
        return val[: max_chars // 2] + "\n…[truncated]…\n" + val[-max_chars // 4 :]
    try:
        s = json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        s = str(val)
    return _truncate_for_event(s, max_chars=max_chars)


def _emit_pair(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any,
    error: str | None,
) -> None:
    binding = skill_volume_tool_event_binding.get()
    if binding is None or binding.event_queue is None:
        return
    job_id = skill_volume_active_job_id.get() or binding.default_job_id
    exec_ctx = skill_volume_active_exec_ctx.get() or binding.root_exec_ctx
    try:
        from deepagents_action_executor import _enqueue_status
    except Exception:
        return
    safe_in = {k: _truncate_for_event(v) for k, v in tool_input.items()}
    safe_out = _truncate_for_event(tool_output)
    data_started = {
        "tool_name": tool_name,
        "mcp_server": None,
        "input": safe_in,
        "skill_volume": True,
    }
    try:
        _enqueue_status(
            binding.event_queue,
            "tool_usage_started",
            data_started,
            binding.proc_inst_id or "",
            binding.todo_id or "",
            binding.crew_type,
            job_id=job_id,
            exec_ctx=exec_ctx,
            logical_event_type="tool_call",
            phase="tool_call",
            agent_name=None,
        )
    except Exception:
        pass
    data_finished: dict[str, Any] = {
        "tool_name": tool_name,
        "mcp_server": None,
        "input": safe_in,
        "output": safe_out,
        "skill_volume": True,
    }
    if error:
        data_finished["error"] = error
    try:
        _enqueue_status(
            binding.event_queue,
            "tool_usage_finished",
            data_finished,
            binding.proc_inst_id or "",
            binding.todo_id or "",
            binding.crew_type,
            job_id=job_id,
            exec_ctx=exec_ctx,
            logical_event_type="tool_result",
            phase="tool_result",
            agent_name=None,
        )
    except Exception:
        pass


def log_skill_volume_tool(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any,
    error: str | None = None,
) -> None:
    """Emit tool_usage_started + tool_usage_finished for a synthetic FS tool (read_file, ls, …)."""
    _emit_pair(
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
        error=error,
    )


