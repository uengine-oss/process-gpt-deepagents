"""Subagent task() event middleware and timeline fallbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from app.executor.pipeline_outputs_extract import merge_task_command_pipeline_outputs
from app.skills.volume_tool_events import skill_volume_active_exec_ctx, skill_volume_active_job_id

from .a2a_events import enqueue_status_update
from .context import ExecutionContext
from .tool_calls import _request_with_merged_tool_args, _tool_call_args_from_request, _tool_call_as_dict
from .workitem_text import _extract_plain_text

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]
    ToolCallRequest = None  # type: ignore[misc, assignment]

try:
    from langchain_core.messages import ToolMessage
except Exception:  # pragma: no cover
    ToolMessage = None  # type: ignore[misc, assignment]


def _create_subagent_event_middleware(
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    name_to_context: dict[str, dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    structured_skill_handoff_by_job: dict[str, dict[str, Any]] | None = None,
    on_before_subagent_start: Any | None = None,
    planning_gate: dict[str, Any] | None = None,
    route_gate: dict[str, Any] | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """Create middleware that emits task_started/task_completed around each subagent (task tool) run.

    Only used when AgentMiddleware is available; otherwise returns (None, state) and executor
    falls back to emitting subagent events after the run.
    """
    # 미들웨어가 실제로 subagent task() 호출을 감쌌는지 추적하기 위한 상태.
    # 도구 호출(log only)만 발생하고 task() 자체는 다른 계층에서 실행되는 경우,
    # 이 플래그가 False 로 남아 있어 fallback 이벤트 발행 로직을 트리거한다.
    state: dict[str, Any] = {
        "saw_task_call": False,
        # task() 미들웨어가 task_started를 enqueue한 job_id / task_completed까지 보낸 job_id (보정용).
        "subagent_task_jobs_started": set(),
        "subagent_task_jobs_completed": set(),
        "skill_handoff_structured": structured_skill_handoff_by_job or {},
    }

    if AgentMiddleware is None or ToolCallRequest is None:
        return None, state

    def _blocked_task_tool_message(request: Any, payload: dict[str, Any]) -> Any:
        """Return ToolMessage/Command-compatible value for blocked task().

        deepagents FilesystemMiddleware expects tool results to be ToolMessage|Command.
        Returning a raw dict here can crash the tool pipeline when large-result eviction runs.
        """
        tc = _tool_call_as_dict(request)
        tool_call_id = str(tc.get("id") or "").strip()
        try:
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            content = str(payload)
        if ToolMessage is not None and tool_call_id:
            return ToolMessage(content=content, name="task", tool_call_id=tool_call_id)
        # Fallback (should be rare): preserve non-crashing string.
        return content

    def _derive_subagent_goal_from_task_args(args: dict[str, Any], default_goal: str) -> str:
        """Prefer delegated task() instruction text over static DB-config goal."""
        if not isinstance(args, dict):
            return default_goal or ""
        for key in ("goal", "instruction", "description", "prompt", "query", "task"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            text = _extract_plain_text(value).strip()
            if text:
                return text
        return default_goal or ""

    def _goal_for_task_started_event(raw: str, *, max_chars: int = 1200) -> str:
        """UI/이벤트용 goal: 내부 주입 없이 짧게 유지."""
        s = (raw or "").strip()
        if not s:
            return ""
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 1].rstrip() + "…"

    class SubagentEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            tool_name = getattr(getattr(request, "tool", None), "name", None)
            if tool_name != "task":
                return await handler(request)

            # Normalize subagent identifiers to reduce avoidable task() failures.
            # Models often emit human-friendly names with spaces; our registry keys frequently use underscores.
            try:
                args0 = dict(_tool_call_args_from_request(request))
                sub0 = args0.get("subagent_type") or args0.get("subagent_name")
                if isinstance(sub0, str) and sub0.strip():
                    normalized = sub0.strip().replace("-", "_").replace(" ", "_")
                    if normalized != sub0:
                        if "subagent_type" in args0:
                            args0["subagent_type"] = normalized
                        elif "subagent_name" in args0:
                            args0["subagent_name"] = normalized
                        request = _request_with_merged_tool_args(request, args0)
            except Exception:
                # Never break task execution due to normalization errors.
                pass

            # HTTP chat routing: task() is only allowed when route=needs_task.
            if isinstance(route_gate, dict):
                route = str(route_gate.get("route") or "").strip()
                if route and route != "needs_task":
                    return _blocked_task_tool_message(
                        request,
                        {
                            "error": "task_not_allowed",
                            "message": (
                                f"현재 라우팅은 `{route}` 입니다. "
                                "`task()`는 `needs_task` 라우팅에서만 허용됩니다."
                            ),
                            "requires_human": False,
                        },
                    )

            if planning_gate is not None and not (
                planning_gate.get("saw_write_todos") or planning_gate.get("saw_write_pipeline_spec")
            ):
                return _blocked_task_tool_message(
                    request,
                    {
                        "error": "plan_submission_required",
                        "message": (
                            "실행 단계(task)로 넘어가기 전에 작업 계획을 기록해야 합니다. "
                            "`write_pipeline_spec`으로 파이프라인 스펙을 제출하거나, "
                            "`write_todos`로 할 일을 기록하세요."
                        ),
                        "requires_human": False,
                    },
                )

            # 서브에이전트 시작 직전 콜백: 플래닝 완료 이벤트를 선발행할 수 있게 한다.
            if on_before_subagent_start is not None:
                try:
                    callback_result = on_before_subagent_start()
                    if hasattr(callback_result, "__await__"):
                        await callback_result
                except Exception:
                    logger.exception(
                        "on_before_subagent_start callback failed; continuing task() execution",
                        exc_info=True,
                    )

            args = dict(_tool_call_args_from_request(request))
            subagent_type = args.get("subagent_type") or args.get("subagent_name") or ""
            ctx = name_to_context.get(subagent_type) if subagent_type else None
            if not ctx:
                raw = await handler(request)
                if planning_gate is not None:
                    planning_gate["saw_task_delegation"] = True
                return merge_task_command_pipeline_outputs(raw, str(subagent_type or ""))

            # task_started.goal 은 주입([SKILL_HANDOFF_*], [TENANT_CONTEXT_*]) 전의 위임 문구만 쓴다.
            # 주입 후 args 에서 다시 파생하면 UI에 raw 마커·JSON 전체가 노출된다.
            event_goal_pre_injection = _derive_subagent_goal_from_task_args(
                dict(args), str(ctx.get("goal") or "")
            )


            # 실제 매핑 가능한 subagent task()를 감싼 경우만 True.
            # unknown task()만 있었던 런에서는 fallback pair를 유지해야 한다.
            state["saw_task_call"] = True

            # Prefer stable job_id from subagent context (executor-assigned); align task/tool events.
            configured_jid = str(ctx.get("job_id") or "").strip()
            job_id_from_call = ""
            try:
                tc = getattr(request, "tool_call", None)
                if isinstance(tc, dict):
                    job_id_from_call = str(tc.get("id") or "").strip()
                else:
                    job_id_from_call = str(getattr(tc, "id", "") or "").strip()
            except Exception:
                job_id_from_call = ""
            job_id = configured_jid or job_id_from_call or str(uuid.uuid4())
            display_goal = _goal_for_task_started_event(
                event_goal_pre_injection.strip() or str(ctx.get("goal") or "")
            )
            enqueue_status_update(
                event_queue,
                "task_started",
                {
                    "role": ctx["role"],
                    "name": ctx["name"],
                    "goal": display_goal,
                    "agent_profile": ctx["agent_profile"],
                    "agent_id": ctx.get("agent_id"),
                },
                proc_inst_id,
                task_id,
                "deepagents",
                job_id=job_id,
                exec_ctx=ctx.get("exec_ctx"),
            )
            try:
                state["subagent_task_jobs_started"].add(job_id)
            except Exception:
                logger.exception("Failed to record subagent task_started job_id", exc_info=True)
            try:
                # Skill discovery must not "finish early". Retry until checklist is satisfied.
                result = None
                # Scope downstream tool events (skill volume + tool_usage) to this delegated task call.
                tok_j = skill_volume_active_job_id.set(job_id)
                tok_e = skill_volume_active_exec_ctx.set(ctx.get("exec_ctx"))
                try:
                    result = await handler(request)
                finally:
                    skill_volume_active_exec_ctx.reset(tok_e)
                    skill_volume_active_job_id.reset(tok_j)

                if planning_gate is not None:
                    planning_gate["saw_task_delegation"] = True

                used_tools = subagent_tool_log.get(job_id, [])
                used_skills = subagent_skill_log.get(job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                if used_skills:
                    작업_텍스트 += f" | 참고/사용한 스킬: {', '.join(sorted(set(used_skills)))}"
                enqueue_status_update(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": 작업_텍스트,
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
                try:
                    state["subagent_task_jobs_completed"].add(job_id)
                except Exception:
                    logger.exception("Failed to record subagent task_completed job_id", exc_info=True)
                return merge_task_command_pipeline_outputs(result, str(subagent_type or ""))
            except Exception:
                enqueue_status_update(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": "작업 실행 중 오류가 발생했습니다.",
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
                try:
                    state["subagent_task_jobs_completed"].add(job_id)
                except Exception:
                    logger.exception("Failed to record subagent task_completed job_id (error path)", exc_info=True)
                raise

    return SubagentEventMiddleware(), state


def _emit_subagent_task_started(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    exec_ctx: ExecutionContext | None,
    ctx: dict[str, Any],
) -> None:
    enqueue_status_update(
        event_queue,
        "task_started",
        {
            "message": f"서브 에이전트 '{ctx['name']}' 실행을 시작했습니다.",
            "role": ctx.get("role"),
            "goal": ctx.get("goal"),
            "agent_profile": ctx.get("agent_profile"),
            "agent_id": ctx.get("agent_id"),
        },
        proc_inst_id,
        task_id,
        "deepagents",
        job_id=ctx["job_id"],
        exec_ctx=exec_ctx,
        logical_event_type="agent_started",
        phase="reasoning",
        agent_name=ctx["name"],
    )


def _emit_subagent_task_completed(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    exec_ctx: ExecutionContext | None,
    ctx: dict[str, Any],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    message_override: str | None = None,
) -> None:
    sub_job_id = ctx["job_id"]
    used_tools = subagent_tool_log.get(sub_job_id, [])
    used_skills = subagent_skill_log.get(sub_job_id, [])
    작업_텍스트 = (
        f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
        if used_tools
        else "도구 호출 이벤트 없이 작업을 수행했습니다."
    )
    if used_skills:
        작업_텍스트 += f" | 참고/사용한 스킬: {', '.join(sorted(set(used_skills)))}"
    done_message = message_override or f"서브 에이전트 '{ctx['name']}' 실행이 완료되었습니다."
    enqueue_status_update(
        event_queue,
        "task_completed",
        {
            "message": done_message,
            "summary": 작업_텍스트,
            "role": ctx.get("role"),
            "name": ctx.get("name"),
        },
        proc_inst_id,
        task_id,
        "deepagents",
        job_id=sub_job_id,
        exec_ctx=exec_ctx,
        logical_event_type="agent_finished",
        phase="reasoning",
        agent_name=ctx["name"],
    )


async def _emit_subagent_timeline_fallback(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    root_exec_ctx: ExecutionContext | None,
    from_orchestrator_error: bool,
) -> None:
    """Emit task_started/task_completed for each configured subagent (middleware miss path).

    When the orchestrator raises before the success tail runs, the normal fallback is skipped;
    this path ensures the UI still receives a per-subagent timeline for auditing.
    """
    for ctx in subagent_contexts:
        _emit_subagent_task_started(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
        )
    for ctx in subagent_contexts:
        msg_override = None
        if from_orchestrator_error:
            msg_override = (
                f"서브 에이전트 '{ctx['name']}'에 대해 오케스트레이터 오류로 실행이 중단된 것으로 "
                "타임라인을 보정합니다."
            )
        _emit_subagent_task_completed(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            message_override=msg_override,
        )
    await asyncio.sleep(1.2)


async def _reconcile_partial_subagent_task_completed(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    subagent_event_state: dict[str, Any],
    root_exec_ctx: ExecutionContext | None,
    from_orchestrator_error: bool,
) -> None:
    """If middleware emitted task_started for a job but never task_completed, emit completed now."""
    started = subagent_event_state.get("subagent_task_jobs_started")
    completed = subagent_event_state.get("subagent_task_jobs_completed")
    if not isinstance(started, set) or not isinstance(completed, set):
        return
    pending_ctxs: list[dict[str, Any]] = []
    for ctx in subagent_contexts:
        jid = ctx.get("job_id")
        if isinstance(jid, str) and jid in started and jid not in completed:
            pending_ctxs.append(ctx)
    if not pending_ctxs:
        return
    logger.info(
        "서브에이전트 task_completed 누락 보정 | task_id=%s jobs=%s",
        task_id,
        [c.get("job_id") for c in pending_ctxs],
    )
    for ctx in pending_ctxs:
        msg_override = None
        if from_orchestrator_error:
            msg_override = (
                f"서브 에이전트 '{ctx['name']}' 실행 중 상위 오케스트레이터가 실패하여 "
                "task_completed를 보정 발행합니다."
            )
        _emit_subagent_task_completed(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            message_override=msg_override,
        )
        try:
            completed.add(str(ctx["job_id"]))
        except Exception:
            pass
    await asyncio.sleep(0.6)
