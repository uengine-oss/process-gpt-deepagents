"""Monkey-patch tool invoke/ainvoke for ProcessGPT event logging and HITL."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .a2a_events import enqueue_status_update
from .context import ExecutionContext
from .exceptions import HitlInterrupt
from .hitl_utils import (
    _extract_hitl_decision_from_output,
    _record_context_tool_attempt,
    _should_defer_hitl_interrupt,
)
from .tool_calls import _json_safe
from .workitem_text import _coerce_tool_input_dict
from app.sandbox.output_delivery import note_sandbox_tool_output_paths
from app.skills.volume_tool_events import align_tool_event_job_id

logger = logging.getLogger(__name__)

def http_chat_tool_display_name(base_tool: Any, internal_name: str) -> str:
    """``_mcp_server``가 있으면 ``{mcp명}__{도구명}``으로 표시한다.

    LangChain 도구의 실제 ``.name``(및 A2A ``tool_usage_*``의 tool_name)은 바꾸지 않고,
    HTTP ``tool_*`` 스트림과 ``job_tool_log`` 집계에만 적용한다. 로컬 도구는 접두사 없음.
    """
    raw = (internal_name or "").strip()
    if not raw:
        return raw
    srv = str(getattr(base_tool, "_mcp_server", "") or "").strip()
    if not srv:
        return raw
    mcp_label = srv.lower().replace("_", "-")
    prefix = f"{mcp_label}__"
    if raw.startswith(prefix):
        return raw
    return f"{prefix}{raw}"


def _coerce_tool_output_for_tool_node(
    output: Any,
    tool_input: Any,
    tool_name: str,
) -> Any:
    """LangGraph ToolNode requires ``ToolMessage`` or ``Command`` for tool results.

    Normal ``BaseTool.ainvoke`` wraps plain strings when given a full ToolCall dict.
    Our instrumentation returns early (guard block) or forwards results without that
    step, so we mirror the coercion here when ``tool_input`` is ToolCall-shaped.
    """
    if output is None:
        return output
    try:
        from langchain_core.messages import ToolMessage
        from langchain_core.tools.base import _is_message_content_type, _stringify
    except ImportError:
        return output

    if isinstance(output, ToolMessage):
        return output
    try:
        from langgraph.types import Command
    except ImportError:
        pass
    else:
        if isinstance(output, Command):
            return output

    if not isinstance(tool_input, dict) or tool_input.get("type") != "tool_call":
        return output
    call_id = tool_input.get("id")
    if not call_id:
        return output

    if _is_message_content_type(output):
        content: Any = output
    else:
        content = _stringify(output)

    return ToolMessage(
        content=content,
        name=tool_name or "tool",
        tool_call_id=str(call_id),
    )


def _instrument_tools(
    tools: list[Any],
    *,
    todo_id: str | None,
    proc_inst_id: str | None,
    crew_type: str = "deepagents",
    job_id_getter: callable | None = None,
    job_tool_log: dict[str, list[str]] | None = None,
    job_skill_log: dict[str, list[str]] | None = None,
    tool_result_hook: callable | None = None,
    exec_ctx: ExecutionContext | None = None,
    event_queue: Any | None = None,
    tool_input_augmenter: callable | None = None,
    tool_call_guard: callable | None = None,
    context_tool_tracker: dict[str, Any] | None = None,
) -> list[Any]:
    """Wrap tools in-place so that all calls are logged into the events table.

    IMPORTANT: we do NOT change the tool type, only monkey-patch invoke/ainvoke
    on existing tool instances so that DeepAgents / LangChain can still treat
    them as normal tools.
    """
    if not tools:
        return []

    def _semantic_downgrade_sandbox_like_result(tool_output: Any) -> Any:
        """Mark 'semantic failure' for sandbox-style results when stdout/stderr show errors.

        Rationale: some generated scripts print an error message but still exit(0). That makes
        downstream behavior brittle (exit_code=0 is treated as success). This function adds a
        conservative `semantic_ok` flag and downgrades `exit_code` to non-zero when we see
        high-signal error patterns.
        """
        if not isinstance(tool_output, dict):
            return tool_output

        # Heuristic: sandbox wrappers typically return these keys.
        if "exit_code" not in tool_output or ("stdout" not in tool_output and "stderr" not in tool_output):
            return tool_output

        exit_code = tool_output.get("exit_code")
        if not isinstance(exit_code, int):
            return tool_output

        stdout = tool_output.get("stdout") if isinstance(tool_output.get("stdout"), str) else ""
        stderr = tool_output.get("stderr") if isinstance(tool_output.get("stderr"), str) else ""
        combined = (stdout + "\n" + stderr).strip()

        semantic_ok = exit_code == 0
        semantic_error: str | None = None

        if combined:
            lowered = combined.lower()
            looks_like_traceback = "traceback (most recent call last):" in lowered
            starts_with_error = combined.lstrip().startswith(("Error:", "ERROR:", "error:"))
            contains_uncaught = any(tok in lowered for tok in ("uncaught exception", "unhandled exception"))
            contains_fatal = any(tok in lowered for tok in ("fatal:", "segmentation fault", "core dumped"))

            if looks_like_traceback or starts_with_error or contains_uncaught or contains_fatal:
                semantic_ok = False
                semantic_error = combined[:5000]

        # Preserve original object when nothing changes.
        if semantic_ok and "semantic_ok" not in tool_output and "semantic_error" not in tool_output:
            out = dict(tool_output)
            out["semantic_ok"] = True
            return out
        if semantic_ok:
            return tool_output

        out = dict(tool_output)
        out["semantic_ok"] = False
        if semantic_error is not None:
            out["semantic_error"] = semantic_error
        if exit_code == 0:
            out["exit_code"] = 2
        return out

    def _augment_tool_input(base_tool: Any, tool_input: Any) -> Any:
        if tool_input_augmenter is None:
            return tool_input
        try:
            augmented = tool_input_augmenter(base_tool, tool_input)
            return tool_input if augmented is None else augmented
        except Exception:
            logger.exception("tool_input_augmenter failed; using original tool_input", exc_info=True)
            return tool_input

    def _resolve_job_id() -> str:
        """Return the job_id to use for this tool call.

        If a job_id_getter is provided and returns a truthy value, that value
        is used so that tool_usage_started/finished share the same job_id as
        the surrounding task_started event. Otherwise, fall back to a fresh
        UUID per tool call.
        """
        if job_id_getter is not None:
            try:
                value = job_id_getter()
                if value:
                    return str(value)
            except Exception:
                logger.exception("job_id_getter failed; falling back to new UUID", exc_info=True)
        return str(uuid.uuid4())

    def _tool_node_safe_output(out: Any, inp: Any, base_tool: Any) -> Any:
        tname = getattr(base_tool, "name", None) or base_tool.__class__.__name__
        return _coerce_tool_output_for_tool_node(out, inp, str(tname))

    def _run_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
        if tool_call_guard is None:
            return None
        try:
            return tool_call_guard(base_tool, tool_input)
        except Exception:
            logger.exception("tool_call_guard failed; proceeding with normal execution", exc_info=True)
            return None

    def _log_started(base_tool: Any, job_id: str, tool_input: Any) -> None:
        """도구 호출 시작 이벤트를 SDK EventQueue 경유로 기록."""
        if event_queue is None:
            return
        try:
            safe_input = _json_safe(tool_input)
            tool_name = getattr(base_tool, "name", base_tool.__class__.__name__)
            # "사용한 스킬"은 결정적 시그널 기반으로만 기록한다.
            # Primary: sandbox_copy_mounted_skill 입력에서 skill_name/skill_names_csv 추출.
            if job_skill_log is not None:
                try:
                    if tool_name == "sandbox_copy_mounted_skill":
                        raw = tool_input if isinstance(tool_input, dict) else _coerce_tool_input_dict(tool_input)
                        raw = raw if isinstance(raw, dict) else {}
                        skill_names: list[str] = []
                        one = raw.get("skill_name")
                        if isinstance(one, str) and one.strip():
                            skill_names.append(one.strip())
                        many = raw.get("skill_names_csv")
                        if isinstance(many, str) and many.strip():
                            skill_names.extend([s.strip() for s in many.split(",") if s.strip()])
                        many2 = raw.get("skill_names")
                        if isinstance(many2, list):
                            skill_names.extend([str(s).strip() for s in many2 if str(s).strip()])
                        if skill_names:
                            bucket = job_skill_log.setdefault(job_id, [])
                            for s in skill_names:
                                if s and s not in bucket:
                                    bucket.append(s)
                except Exception:
                    logger.exception("Failed to record used skills from tool input", exc_info=True)
            data = {
                "tool_name": tool_name,
                "mcp_server": getattr(base_tool, "_mcp_server", None),
                "input": safe_input,
            }
            enqueue_status_update(
                event_queue,
                "tool_usage_started",
                data,
                proc_inst_id or "",
                str(todo_id) if todo_id else "",
                crew_type,
                job_id=job_id,
                exec_ctx=exec_ctx,
                logical_event_type="tool_call",
                phase="tool_call",
                agent_name=None,
            )
        except Exception:
            logger.exception("Failed to enqueue tool_usage_started event", exc_info=True)

    def _log_finished(
        base_tool: Any,
        job_id: str,
        tool_input: Any,
        tool_output: Any,
        error: Exception | None = None,
    ) -> dict[str, Any] | None:
        """Log tool result and return a potential HITL decision (if any)."""
        safe_input = _json_safe(tool_input)
        safe_output = _json_safe(tool_output)
        tool_name = getattr(base_tool, "name", base_tool.__class__.__name__)
        data: dict[str, Any] = {
            "tool_name": tool_name,
            "mcp_server": getattr(base_tool, "_mcp_server", None),
            "input": safe_input,
            "output": safe_output,
        }
        if error is not None:
            data["error"] = str(error)

        decision: dict[str, Any] | None = None
        # Only attempt to extract a HITL decision when there is no error and
        # the original tool_output is available.
        if error is None and isinstance(tool_output, dict):
            decision = _extract_hitl_decision_from_output(tool_output)
        try:
            if event_queue is not None:
                enqueue_status_update(
                    event_queue,
                    "tool_usage_finished",
                    data,
                    proc_inst_id or "",
                    str(todo_id) if todo_id else "",
                    crew_type,
                    job_id=job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="tool_result",
                    phase="tool_result",
                    agent_name=None,
                )
            # 서브 에이전트 작업 요약 작성을 위해 job_id별 사용된 도구 이름을 수집한다.
            if job_tool_log is not None:
                try:
                    name = data.get("tool_name")
                    if isinstance(name, str) and name:
                        job_tool_log.setdefault(job_id, []).append(
                            http_chat_tool_display_name(base_tool, name)
                        )
                except Exception:
                    logger.exception("Failed to collect tool usage for job summary", exc_info=True)
        except Exception:
            logger.exception("Failed to enqueue tool_usage_finished event", exc_info=True)
        if error is None and isinstance(tool_output, dict):
            try:
                note_sandbox_tool_output_paths(
                    str(todo_id) if todo_id else "",
                    str(tool_name),
                    tool_output,
                    error,
                )
            except Exception:
                logger.exception("note_sandbox_tool_output_paths failed", exc_info=True)
        _record_context_tool_attempt(base_tool, context_tool_tracker)
        if tool_result_hook is not None:
            try:
                tool_result_hook(
                    base_tool=base_tool,
                    job_id=job_id,
                    tool_input=tool_input,
                    tool_output=tool_output,
                    error=error,
                )
            except Exception:
                logger.exception("tool_result_hook failed; continuing", exc_info=True)
        return decision

    for t in tools:
        # Avoid double-wrapping the same tool instance.
        if getattr(t, "_processgpt_instrumented", False):
            continue

        orig_invoke = getattr(t, "invoke", None)
        orig_ainvoke = getattr(t, "ainvoke", None)

        if orig_invoke is not None and callable(orig_invoke):

            def make_sync_wrapper(base_tool: Any, original):
                def wrapper(tool_input: Any, *args: Any, **kwargs: Any) -> Any:
                    job_id = align_tool_event_job_id(
                        resolved_job_id=_resolve_job_id(),
                        exec_ctx=exec_ctx,
                    )
                    effective_input = _augment_tool_input(base_tool, tool_input)
                    blocked_output = _run_tool_call_guard(base_tool, effective_input)
                    if blocked_output is not None:
                        _log_started(base_tool, job_id, effective_input)
                        _log_finished(base_tool, job_id, effective_input, blocked_output, None)
                        return _tool_node_safe_output(blocked_output, effective_input, base_tool)
                    _log_started(base_tool, job_id, effective_input)
                    try:
                        result = original(effective_input, *args, **kwargs)
                        result = _semantic_downgrade_sandbox_like_result(result)
                        decision = _log_finished(base_tool, job_id, effective_input, result, None)
                        if decision is not None:
                            would_defer = _should_defer_hitl_interrupt(decision)
                            if would_defer:
                                logger.info(
                                    "Deferring HITL: returning requires_human tool result to model for more tool attempts | tool=%s job_id=%s",
                                    getattr(base_tool, "name", base_tool.__class__.__name__),
                                    job_id,
                                )
                                return _tool_node_safe_output(result, effective_input, base_tool)
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return _tool_node_safe_output(result, effective_input, base_tool)
                    except HitlInterrupt:
                        # _log_finished already recorded the tool result once.
                        raise
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, effective_input, None, e)
                        raise

                return wrapper

            # Pydantic-based tools (e.g. StructuredTool) forbid normal setattr;
            # use object.__setattr__ to bypass the field check.
            try:
                object.__setattr__(t, "invoke", make_sync_wrapper(t, orig_invoke))
            except Exception:
                logger.exception("Failed to patch tool.invoke for instrumentation", exc_info=True)

        if orig_ainvoke is not None and callable(orig_ainvoke):

            def make_async_wrapper(base_tool: Any, original):
                async def wrapper(tool_input: Any, *args: Any, **kwargs: Any) -> Any:
                    job_id = align_tool_event_job_id(
                        resolved_job_id=_resolve_job_id(),
                        exec_ctx=exec_ctx,
                    )
                    effective_input = _augment_tool_input(base_tool, tool_input)
                    blocked_output = _run_tool_call_guard(base_tool, effective_input)
                    if blocked_output is not None:
                        _log_started(base_tool, job_id, effective_input)
                        _log_finished(base_tool, job_id, effective_input, blocked_output, None)
                        return _tool_node_safe_output(blocked_output, effective_input, base_tool)
                    _log_started(base_tool, job_id, effective_input)
                    try:
                        result = await original(effective_input, *args, **kwargs)
                        result = _semantic_downgrade_sandbox_like_result(result)
                        decision = _log_finished(base_tool, job_id, effective_input, result, None)
                        if decision is not None:
                            would_defer = _should_defer_hitl_interrupt(decision)
                            if would_defer:
                                logger.info(
                                    "Deferring HITL: returning requires_human tool result to model for more tool attempts | tool=%s job_id=%s",
                                    getattr(base_tool, "name", base_tool.__class__.__name__),
                                    job_id,
                                )
                                return _tool_node_safe_output(result, effective_input, base_tool)
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return _tool_node_safe_output(result, effective_input, base_tool)
                    except HitlInterrupt:
                        # _log_finished already recorded the tool result once.
                        raise
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, effective_input, None, e)
                        raise

                return wrapper

            try:
                object.__setattr__(t, "ainvoke", make_async_wrapper(t, orig_ainvoke))
            except Exception:
                logger.exception("Failed to patch tool.ainvoke for instrumentation", exc_info=True)

        setattr(t, "_processgpt_instrumented", True)

    return tools

