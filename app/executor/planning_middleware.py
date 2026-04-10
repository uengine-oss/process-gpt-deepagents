"""Root orchestrator middleware: tool_usage events + planning task_completed on write_todos (and pipeline spec elsewhere)."""

from __future__ import annotations

import logging
from typing import Any

from .a2a_events import _enqueue_status
from .json_safe import _json_safe
from .tool_calls import _tool_call_args_from_request
from .workitem_text import _join_todo_contents

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]

def _create_write_todos_planning_middleware(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    job_uuid: str,
    exec_ctx: Any,
    planning_gate: dict[str, Any],
) -> Any | None:
    """루트 오케스트레이터 도구 호출마다 tool_usage_* 를 발행하고, write_todos 성공 시 플래닝 task_completed 를 발행한다."""
    if AgentMiddleware is None:
        return None

    class WriteTodosPlanningEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            base_tool = getattr(request, "tool", None)
            tool_name = getattr(base_tool, "name", None) or ""
            if getattr(base_tool, "_processgpt_instrumented", False):
                return await handler(request)

            args = _tool_call_args_from_request(request)
            safe_input = _json_safe(args)
            mcp_server = getattr(base_tool, "_mcp_server", None)
            event_tool_name = str(tool_name) if tool_name else type(base_tool).__name__

            async def _run_with_tool_usage_events() -> Any:
                # 루트 오케스트레이터 run과 동일한 job_id로 수집·표시 정합성 유지 (매 호출 uuid4 금지)
                tool_call_job_id = str(job_uuid)
                try:
                    _enqueue_status(
                        event_queue,
                        "tool_usage_started",
                        {
                            "tool_name": event_tool_name,
                            "mcp_server": mcp_server,
                            "input": safe_input,
                        },
                        proc_inst_id,
                        task_id,
                        "action",
                        job_id=tool_call_job_id,
                        exec_ctx=exec_ctx,
                        logical_event_type="tool_call",
                        phase="tool_call",
                        agent_name=None,
                    )
                except Exception:
                    logger.exception(
                        "Failed to enqueue tool_usage_started for orchestrator tool=%s",
                        event_tool_name,
                        exc_info=True,
                    )

                result_inner: Any = None
                err_inner: Exception | None = None
                try:
                    result_inner = await handler(request)
                except Exception as exc:
                    err_inner = exc
                    raise
                finally:
                    finished_payload: dict[str, Any] = {
                        "tool_name": event_tool_name,
                        "mcp_server": mcp_server,
                        "input": safe_input,
                        "output": _json_safe(result_inner) if err_inner is None else None,
                    }
                    if err_inner is not None:
                        finished_payload["error"] = str(err_inner)
                    try:
                        _enqueue_status(
                            event_queue,
                            "tool_usage_finished",
                            finished_payload,
                            proc_inst_id,
                            task_id,
                            "action",
                            job_id=tool_call_job_id,
                            exec_ctx=exec_ctx,
                            logical_event_type="tool_result",
                            phase="tool_result",
                            agent_name=None,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to enqueue tool_usage_finished for orchestrator tool=%s",
                            event_tool_name,
                            exc_info=True,
                        )

                return result_inner

            if tool_name != "write_todos":
                return await _run_with_tool_usage_events()

            todos_arg = args.get("todos") if isinstance(args, dict) else None
            result = await _run_with_tool_usage_events()

            planning_gate["saw_write_todos"] = True
            if planning_gate.get("planning_completed_emitted"):
                return result

            plan_message = ""
            try:
                plan_message = _join_todo_contents(todos_arg)
            except Exception:
                logger.exception("Failed to join todo contents from write_todos args", exc_info=True)

            try:
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"message": plan_message or "플래닝이 완료되었습니다."},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_finished",
                    phase="planning",
                    agent_name="작업 실행 기획자",
                )
                planning_gate["planning_completed_emitted"] = True
            except Exception:
                logger.exception("Failed to enqueue planning task_completed from write_todos", exc_info=True)

            return result

    return WriteTodosPlanningEventMiddleware()
