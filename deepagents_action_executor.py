"""DeepAgents executor: ProcessGPT A2A 래퍼 + DeepAgents orchestrator."""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from typing import Any

from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskStatusUpdateEvent, TaskState, TaskArtifactUpdateEvent
from a2a.utils import new_agent_text_message, new_text_artifact

from deepagents_hitl import extract_interrupt_payload
from subagents import build_subagent_configs
from deepagents_factory import (
    build_orchestrator,
    build_orchestrator_system_prompt,
    default_orchestrator_prompt,
    build_planner_agent,
)
from output_utils import convert_agent_output
from processgpt_agent_utils.tools.safe_tool_loader import SafeToolLoader
from processgpt_agent_utils.utils.context_manager import set_context

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain.agents.middleware.types import ToolCallRequest
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]
    ToolCallRequest = None  # type: ignore[misc, assignment]

try:
    from langchain_core.tools import BaseTool as LangChainBaseTool
    from langchain_core.tools import StructuredTool as LangChainStructuredTool
    from pydantic import BaseModel as PydanticBaseModel
    _LANGCHAIN_TOOLS_AVAILABLE = True
except ImportError:
    LangChainBaseTool = None  # type: ignore[misc, assignment]
    LangChainStructuredTool = None  # type: ignore[misc, assignment]
    PydanticBaseModel = None  # type: ignore[misc, assignment]
    _LANGCHAIN_TOOLS_AVAILABLE = False


def _ensure_langchain_tools(tools: list[Any]) -> list[Any]:
    """CrewAI/MCP 툴을 LangChain BaseTool로 래핑하여 tool decorator __name__ 오류를 방지합니다.

    crewai_tools.MCPServerAdapter가 반환하는 CrewAIMCPTool 등은 LangChain 파이프라인에서
    tool() 데코레이터에 전달될 때 'callable with __name__' 요구로 인해 오류가 발생합니다.
    이 함수는 LangChain BaseTool이 아닌 툴을 LangChain StructuredTool로 감싸 반환합니다.
    """
    if not _LANGCHAIN_TOOLS_AVAILABLE or not tools:
        return tools

    result: list[Any] = []
    for t in tools:
        if isinstance(t, LangChainBaseTool):
            result.append(t)
            continue

        # CrewAI/기타 툴: run 또는 invoke/ainvoke 위임 래퍼로 LangChain 툴 생성
        name = getattr(t, "name", None) or getattr(t, "__class__", type(t)).__name__
        description = getattr(t, "description", None) or str(t)

        def _make_sync_wrapper(original: Any) -> Any:
            def _run(**kwargs: Any) -> Any:
                if hasattr(original, "run"):
                    return original.run(**kwargs)
                if hasattr(original, "invoke"):
                    return original.invoke(kwargs)
                raise RuntimeError(f"Tool {getattr(original, 'name', original)} has no run/invoke")

            return _run

        def _make_async_wrapper(original: Any) -> Any:
            async def _arun(**kwargs: Any) -> Any:
                if hasattr(original, "arun"):
                    return await original.arun(**kwargs)
                if hasattr(original, "ainvoke"):
                    return await original.ainvoke(kwargs)
                # sync fallback
                sync_run = _make_sync_wrapper(original)
                return sync_run(**kwargs)

            return _arun

        sync_func = _make_sync_wrapper(t)
        async_func = _make_async_wrapper(t)
        safe_name = (name or "unknown").replace(" ", "_")
        sync_func.__name__ = safe_name  # type: ignore[attr-defined]
        async_func.__name__ = safe_name  # type: ignore[attr-defined]

        args_schema = getattr(t, "args_schema", None)
        use_schema = (
            args_schema is not None
            and isinstance(args_schema, type)
            and PydanticBaseModel is not None
            and issubclass(args_schema, PydanticBaseModel)
        )

        try:
            if use_schema:
                wrapped = LangChainStructuredTool(
                    name=name,
                    description=description or name,
                    func=sync_func,
                    coroutine=async_func,
                    args_schema=args_schema,
                )
            else:
                # 스키마 없음/호환 불가 시 최소 스키마로 생성 (입력 dict 허용)
                _DynamicSchema = type(
                    f"_Schema_{safe_name}",
                    (PydanticBaseModel,),
                    {"model_config": {"extra": "allow"}, "__annotations__": {}},
                )
                wrapped = LangChainStructuredTool(
                    name=name,
                    description=description or name,
                    func=sync_func,
                    coroutine=async_func,
                    args_schema=_DynamicSchema,
                )
        except Exception as e:
            logger.warning(
                "CrewAI/MCP tool wrap failed, using minimal schema: %s | tool=%s",
                e,
                name,
            )
            _FallbackSchema = type(
                f"_Fallback_{safe_name}",
                (PydanticBaseModel,),
                {"model_config": {"extra": "allow"}, "__annotations__": {}},
            )
            wrapped = LangChainStructuredTool(
                name=name,
                description=description or name,
                func=sync_func,
                coroutine=async_func,
                args_schema=_FallbackSchema,
            )

        if hasattr(t, "_mcp_server"):
            setattr(wrapped, "_mcp_server", getattr(t, "_mcp_server"))
        result.append(wrapped)

    return result


class ExecutionContext:
    """Execution-scoped context for multi-agent runs and tracing.

    DeepAgents 오케스트레이션에 공통으로 필요한 상위 메타데이터(correlation_id,
    agent_path, task_id, proc_inst_id 등)를 담고, events.data.meta 에 직렬화하기
    위한 최소 정보를 제공한다.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        agent_path: list[str] | None = None,
        current_agent: str | None = None,
        task_id: str | None = None,
        proc_inst_id: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.agent_path: list[str] = list(agent_path) if agent_path else []
        # If current_agent is not provided, derive it from the path when possible.
        self.current_agent: str | None = current_agent or (
            self.agent_path[-1] if self.agent_path else None
        )
        self.task_id = task_id
        self.proc_inst_id = proc_inst_id

    @property
    def depth(self) -> int:
        return len(self.agent_path)

    def push_agent(self, agent_name: str) -> None:
        self.agent_path.append(agent_name)
        self.current_agent = agent_name

    def pop_agent(self) -> str | None:
        if not self.agent_path:
            return None
        popped = self.agent_path.pop()
        self.current_agent = self.agent_path[-1] if self.agent_path else None
        return popped

    def as_dict(self) -> dict[str, Any]:
        """Return a minimal, serialisable snapshot of execution metadata."""
        meta: dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "agent_path": list(self.agent_path),
            "agent_name": self.current_agent,
            "depth": self.depth,
        }
        if self.task_id is not None:
            meta["task_id"] = self.task_id
        if self.proc_inst_id is not None:
            meta["proc_inst_id"] = self.proc_inst_id
        return meta


def build_event_payload(
    exec_ctx: ExecutionContext | None,
    *,
    logical_event_type: str,
    phase: str,
    tool_name: str | None = None,
    requires_human: bool | None = None,
    input_schema: Any | None = None,
    output_schema: Any | None = None,
    agent_name: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """events.data 로 저장될 **평탄화(flat)** 된 JSON 페이로드를 생성한다.

    - correlation_id, agent_path, phase 등 실행 메타데이터와
      goal, plans, tool_name 같은 도메인 필드를 **동일한 깊이의 키**로 둔다.
    """
    merged: dict[str, Any] = {}

    # 실행 컨텍스트 메타데이터 먼저 병합
    if exec_ctx is not None:
        merged.update(exec_ctx.as_dict())

    # 호출 측에서 명시한 agent_name 이 있다면 덮어쓰기
    if agent_name is not None:
        merged["agent_name"] = agent_name

    # 논리적 이벤트 타입 / 페이즈 정보
    merged["logical_event_type"] = logical_event_type
    merged["phase"] = phase

    # 선택적 필드들
    if tool_name is not None:
        merged["tool_name"] = tool_name
    if requires_human is not None:
        merged["requires_human"] = requires_human
    if input_schema is not None:
        merged["input_schema"] = input_schema
    if output_schema is not None:
        merged["output_schema"] = output_schema

    # 도메인 데이터(목표, 플랜, 메시지 등)를 같은 레벨에 병합
    if extra_data:
        merged.update(extra_data)

    return merged


def _enqueue_status(
    eq: EventQueue,
    event_type: str,
    data: dict[str, Any],
    proc_inst_id: str,
    task_id: str,
    crew_type: str,
    job_id: str | None = None,
    *,
    exec_ctx: ExecutionContext | None = None,
    logical_event_type: str | None = None,
    phase: str | None = None,
    agent_name: str | None = None,
    status: str | None = None,
) -> None:
    """Enqueue a TaskStatusUpdateEvent so that processgpt_agent_sdk handles persistence."""
    effective_job_id = job_id or str(uuid.uuid4())
    logical = logical_event_type or ("agent_started" if event_type == "task_started" else "agent_finished")
    effective_phase = phase or "reasoning"

    # 1) 우리가 원하는 평탄화 payload(메타+도메인)를 먼저 만든다.
    payload = build_event_payload(
        exec_ctx,
        logical_event_type=logical,
        phase=effective_phase,
        agent_name=agent_name,
        extra_data=data,
    )

    # 2) a2a 메시지 객체 생성 (텍스트는 payload JSON)
    try:
        message = new_agent_text_message(
            json.dumps(payload, ensure_ascii=False),
            proc_inst_id,
            task_id,
        )
    except Exception:
        logger.exception("Failed to build agent text message for status event", exc_info=True)
        return

    # 3) TaskState 매핑
    if event_type == "human_asked":
        state = TaskState.input_required
        final_flag = True
    elif event_type in ("task_completed", "tool_usage_finished"):
        state = TaskState.completed
        final_flag = True
    else:
        state = TaskState.working
        final_flag = False

    metadata = {
        "crew_type": crew_type,
        "event_type": event_type,
        "status": status,
        "job_id": effective_job_id,
    }

    try:
        eq.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": state,
                    "message": message,
                },
                final=final_flag,
                contextId=proc_inst_id,
                taskId=task_id,
                metadata=metadata,
            )
        )
    except Exception:
        logger.exception("Failed to enqueue TaskStatusUpdateEvent", exc_info=True)


def _enqueue_artifact(
    eq: EventQueue,
    name: str,
    description: str,
    text: Any,
    proc_inst_id: str,
    task_id: str,
    *,
    exec_ctx: ExecutionContext | None = None,  # exec_ctx는 현재 사용하지 않지만 시그니처 호환용
) -> None:
    """최종 결과를 TaskArtifactUpdateEvent 로 발행한다."""
    try:
        if not isinstance(text, str):
            artifact_text = json.dumps(text, ensure_ascii=False)
        else:
            artifact_text = text
        artifact = new_text_artifact(
            name=name,
            description=description,
            text=artifact_text,
        )
        eq.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=artifact,
                lastChunk=True,
                contextId=proc_inst_id,
                taskId=task_id,
            )
        )
    except Exception:
        logger.exception("Failed to enqueue TaskArtifactUpdateEvent", exc_info=True)


def _create_subagent_event_middleware(
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    name_to_context: dict[str, dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
) -> tuple[Any | None, dict[str, Any]]:
    """Create middleware that emits task_started/task_completed around each subagent (task tool) run.

    Only used when AgentMiddleware is available; otherwise returns (None, state) and executor
    falls back to emitting subagent events after the run.
    """
    # 미들웨어가 실제로 subagent task() 호출을 감쌌는지 추적하기 위한 상태.
    # 도구 호출(log only)만 발생하고 task() 자체는 다른 계층에서 실행되는 경우,
    # 이 플래그가 False 로 남아 있어 fallback 이벤트 발행 로직을 트리거한다.
    state: dict[str, Any] = {"saw_task_call": False}

    if AgentMiddleware is None or ToolCallRequest is None:
        return None, state

    class SubagentEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            tool_name = getattr(getattr(request, "tool", None), "name", None)
            if tool_name != "task":
                return await handler(request)

            # 서브 에이전트 실행을 담당하는 task() 호출을 한 번이라도 감쌌음을 표시.
            state["saw_task_call"] = True

            call = getattr(request, "call", None) or {}
            args = call.get("args", {}) if isinstance(call, dict) else {}
            subagent_type = args.get("subagent_type") or args.get("subagent_name") or ""
            ctx = name_to_context.get(subagent_type) if subagent_type else None
            if not ctx:
                return await handler(request)

            job_id = ctx["job_id"]
            _enqueue_status(
                event_queue,
                "task_started",
                {
                    "role": ctx["role"],
                    "name": ctx["name"],
                    "goal": ctx["goal"],
                    "agent_profile": ctx["agent_profile"],
                    "agent_id": ctx.get("agent_id"),
                },
                proc_inst_id,
                task_id,
                "action",
                job_id=job_id,
            )
            try:
                result = await handler(request)
                used_tools = subagent_tool_log.get(job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"수행한_작업": 작업_텍스트},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                )
                return result
            except Exception:
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"수행한_작업": "작업 실행 중 오류가 발생했습니다."},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                )
                raise

    return SubagentEventMiddleware(), state


def _parse_block(source: str, header: str, next_headers: list[str]) -> str:
    """Extract a block that starts after header and ends before any next_headers."""
    if not source:
        return ""
    start_idx = source.find(header)
    if start_idx == -1:
        return ""
    start_idx += len(header)
    tail = source[start_idx:]
    # Find the earliest occurrence of any other header.
    end_idx = len(tail)
    for h in next_headers:
        idx = tail.find(h)
        if idx != -1 and idx < end_idx:
            end_idx = idx
    return tail[:end_idx].strip()


def parse_workitem_query(query: str) -> dict[str, Any]:
    """Parse [Description]/[Instruction]/[InputData] blocks from the work-item query."""
    text = query or ""
    # Normalise newlines for simpler parsing.
    text = text.replace("\r\n", "\n")

    desc = _parse_block(
        text,
        "[Description]",
        ["[Instruction]", "[InputData]"],
    )
    instr = _parse_block(
        text,
        "[Instruction]",
        ["[Description]", "[InputData]"],
    )
    input_raw = _parse_block(
        text,
        "[InputData]",
        ["[Description]", "[Instruction]"],
    )

    input_data: dict[str, Any] = {}
    if input_raw:
        raw = input_raw.strip()
        # Allow optional surrounding code fences or text around JSON.
        json_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        candidate = json_match.group(0) if json_match else raw
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                input_data = parsed
        except Exception:
            logger.warning("Failed to parse InputData JSON from query; using empty dict.")

    return {
        "description": desc,
        "instruction": instr,
        "input_data_raw": input_raw,
        "input_data": input_data,
    }


class HitlInterrupt(Exception):
    """Exception used to signal a HITL interrupt from inside a tool call.

    Tool implementations can return a structured payload such as:
        {\"requires_human\": true, \"question\": \"...\", \"choices\": [...], ...}
    and the instrumentation layer will raise this exception so that the
    executor can stop the run and hand control back to the caller.
    """

    def __init__(self, *, decision: dict[str, Any], tool_name: str, job_id: str) -> None:
        super().__init__(decision.get("reason") or decision.get("question") or "HITL interrupt requested.")
        self.decision = decision
        self.tool_name = tool_name
        self.job_id = job_id


def _extract_hitl_decision_from_output(output: Any) -> dict[str, Any] | None:
    """Inspect a tool output for a generic, domain-agnostic HITL signal.

    Contract (domain-independent):
    - If output is a dict and contains `requires_human` set to true, treat it as a
      HITL request. Optional keys:
        - question: str      → what to ask the human
        - choices: list[str] → optional predefined options
        - reason: str        → internal explanation (not user-facing, optional)
        - input_schema/output_schema: any → for replay/diagnostics
    """
    if not isinstance(output, dict):
        return None
    requires = output.get("requires_human")
    if requires is not True:
        return None

    question = output.get("question")
    choices = output.get("choices")
    reason = output.get("reason") or ""
    decision: dict[str, Any] = {
        "requires_human": True,
        "question": question,
        "choices": choices,
        "reason": reason,
    }
    # Pass through optional schema fields if present.
    if "input_schema" in output:
        decision["input_schema"] = output["input_schema"]
    if "output_schema" in output:
        decision["output_schema"] = output["output_schema"]
    # Include the raw output for debugging / context replay.
    decision["raw_payload"] = output
    return decision


def _json_safe(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable structures.

    - dict/list/tuple: recurse into elements
    - other non-serialisable objects: fall back to str()
    """
    try:
        json.dumps(value)
        return value
    except TypeError:
        pass

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [ _json_safe(v) for v in value ]
    # Fallback: string representation
    return str(value)


def _extract_plain_text(value: Any) -> str:
    """Extract plain text from common message/content structures without stringifying objects."""
    if isinstance(value, str):
        # 문자열이 리스트/딕셔너리 표현일 수 있으므로 한 번 파싱을 시도한다.
        text_str = value.strip()
        parsed: Any | None = None
        # JSON 파싱 시도
        try:
            parsed = json.loads(text_str)
        except Exception:
            parsed = None
        # JSON이 아니면 literal_eval 시도 (list/dict 등의 리터럴만 허용)
        if parsed is None:
            try:
                parsed = ast.literal_eval(text_str)
            except Exception:
                parsed = None

        if isinstance(parsed, (list, dict)):
            # 파싱에 성공했다면, 그 안에서 텍스트 필드만 추출한다.
            return _extract_plain_text(parsed)
        # 일반 문자열이면 그대로 반환
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return ""


def _extract_plan_block(source: str) -> str:
    """Extract the [PLAN]...[/PLAN] block from orchestrator output. Returns plain text only."""
    if not source or not isinstance(source, str):
        return ""
    text = source.strip()
    start_marker = "[PLAN]"
    end_marker = "[/PLAN]"
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        # No closing tag: take from start to end of string
        block = text[start_idx:].strip()
    else:
        block = text[start_idx:end_idx].strip()
    # Ensure we return plain text (no list/dict string representation)
    return _extract_plain_text(block) if block else ""


def _instrument_tools(
    tools: list[Any],
    *,
    todo_id: str | None,
    proc_inst_id: str | None,
    crew_type: str = "action",
    job_id_getter: callable | None = None,
    job_tool_log: dict[str, list[str]] | None = None,
    exec_ctx: ExecutionContext | None = None,
    event_queue: Any | None = None,
) -> list[Any]:
    """Wrap tools in-place so that all calls are logged into the events table.

    IMPORTANT: we do NOT change the tool type, only monkey-patch invoke/ainvoke
    on existing tool instances so that DeepAgents / LangChain can still treat
    them as normal tools.
    """
    if not tools:
        return []

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

    def _log_started(base_tool: Any, job_id: str, tool_input: Any) -> None:
        """도구 호출 시작 이벤트를 SDK EventQueue 경유로 기록."""
        if event_queue is None:
            return
        try:
            safe_input = _json_safe(tool_input)
            tool_name = getattr(base_tool, "name", base_tool.__class__.__name__)
            data = {
                "tool_name": tool_name,
                "mcp_server": getattr(base_tool, "_mcp_server", None),
                "input": safe_input,
            }
            _enqueue_status(
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
                _enqueue_status(
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
                        job_tool_log.setdefault(job_id, []).append(name)
                except Exception:
                    logger.exception("Failed to collect tool usage for job summary", exc_info=True)
        except Exception:
            logger.exception("Failed to enqueue tool_usage_finished event", exc_info=True)
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
                    job_id = _resolve_job_id()
                    _log_started(base_tool, job_id, tool_input)
                    try:
                        result = original(tool_input, *args, **kwargs)
                        decision = _log_finished(base_tool, job_id, tool_input, result, None)
                        if decision is not None:
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return result
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, tool_input, None, e)
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
                    job_id = _resolve_job_id()
                    _log_started(base_tool, job_id, tool_input)
                    try:
                        result = await original(tool_input, *args, **kwargs)
                        decision = _log_finished(base_tool, job_id, tool_input, result, None)
                        if decision is not None:
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return result
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, tool_input, None, e)
                        raise

                return wrapper

            try:
                object.__setattr__(t, "ainvoke", make_async_wrapper(t, orig_ainvoke))
            except Exception:
                logger.exception("Failed to patch tool.ainvoke for instrumentation", exc_info=True)

        setattr(t, "_processgpt_instrumented", True)

    return tools


class DeepAgentsActionExecutor(AgentExecutor):
    """Runs todolist query via create_deep_agent(subagents); emits A2A Task*Events."""

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("DeepAgents 멀티 에이전트 실행 시작")

        query = context.get_user_input()
        context_data = context.get_context_data() or {}
        row = context_data.get("row", {})
        extras = context_data.get("extras", {})

        proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id") or ""
        task_id = str(row.get("id") or "")
        tenant_id = row.get("tenant_id") or ""
        form_id = extras.get("form_id")

        logger.info("task_id=%s proc_inst_id=%s form_id=%s", task_id, proc_inst_id, form_id)

        # Prepare run_state / human_response before creating execution context.
        run_state = (extras.get("run_state") or {}) if isinstance(extras.get("run_state"), dict) else {}
        human_response_answer = extras.get("human_response_answer")

        # Create or reuse execution context for this run.
        initial_agent_path = ["root_orchestrator"]
        if isinstance(run_state, dict) and isinstance(run_state.get("agent_path"), list):
            # Reuse stored agent_path when resuming.
            initial_agent_path = list(run_state.get("agent_path")) or initial_agent_path
        if exec_ctx is None:
            base_correlation = None
            if isinstance(run_state, dict) and isinstance(run_state.get("correlation_id"), str):
                base_correlation = run_state.get("correlation_id")
            job_uuid = base_correlation or str(uuid.uuid4())
            exec_ctx = ExecutionContext(
                correlation_id=job_uuid,
                agent_path=initial_agent_path,
                task_id=task_id or None,
                proc_inst_id=proc_inst_id or None,
            )
        job_uuid = exec_ctx.correlation_id

        # Parse work-item query into structured parts for downstream logic / HITL.
        parsed_query = parse_workitem_query(query or "")
        description = parsed_query.get("description") or ""
        instruction = parsed_query.get("instruction") or ""
        input_data = parsed_query.get("input_data") or {}

        # Resume: if we have run_state and human_response, merge answer into input_data.
        if isinstance(human_response_answer, str) and human_response_answer.strip():
            pending_field = run_state.get("pending_field") or "human_response"
            input_data = dict(input_data)
            input_data[pending_field] = human_response_answer.strip()
            parsed_query["input_data"] = input_data

            # Emit a resume event for tracing via SDK EventQueue.
            try:
                _enqueue_status(
                    event_queue,
                    "task_working",
                    {
                        "resume_source": "human_response",
                        "human_answer": human_response_answer.strip(),
                        "previous_interrupt_agent_path": run_state.get("agent_path"),
                        "previous_interrupt_tool_name": run_state.get("tool_name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="resume",
                    phase="resume",
                    agent_name=None,
                )
            except Exception:
                logger.exception("Failed to enqueue resume event", exc_info=True)

        # Initialize processgpt_agent_utils context (for tools like mem0, memento, dmn_rule).
        set_context(
            task_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="action",
            users_email=extras.get("notify_user_emails", []),
        )

        all_tools: list[Any] = []
        try:
            # SafeToolLoader 는 기본 MCP 서버(claude-skills, computer-use)를 포함한
            # MCP 도구와 로컬 툴(mem0, memento, human_asked, dmn_rule)을 로드한다.
            tool_loader = SafeToolLoader(tenant_id=tenant_id or None)
            # tool_names 를 비우면 is_default=True 인 MCP 서버들을 우선 로드한다.
            all_tools = tool_loader.create_tools_from_names(tool_names=[], agent_type=None)
            # CrewAI/MCP 툴을 LangChain BaseTool로 래핑하여 tool() 데코레이터 __name__ 오류 방지
            all_tools = _ensure_langchain_tools(all_tools)
        except Exception as e:
            logger.warning(
                "MCP tools loading via SafeToolLoader failed (subagent config continues with no MCP tools): %s",
                e,
                exc_info=False,
            )

        # job_id 컨텍스트: 기본값은 오케스트레이터(플래너) 작업의 job_id이며,
        # 서브 에이전트별 도구 호출은 각 서브 에이전트 전용 job_id로 분리된다.
        current_job_id: dict[str, str | None] = {"value": None}
        subagent_tool_log: dict[str, list[str]] = {}

        agent_rows = extras.get("agents", []) or []
        subagents = build_subagent_configs(
            agent_rows,
            all_tools=all_tools,
            query=query or "",
            tenant_id=tenant_id or None,
        )
        if not subagents:
            logger.warning(
                "No subagents configured (agents=%d). Orchestrator will run without delegation.",
                len(agent_rows),
            )

        subagent_contexts: list[dict[str, Any]] = []
        # 서브 에이전트별 job_id와 메타데이터를 준비하고,
        # 각 서브 에이전트의 MCP 도구를 해당 job_id로 인스트루먼트한다.
        for idx, row in enumerate(agent_rows):
            agent_name = (
                row.get("alias")
                or row.get("username")
                or row.get("name")
                or str(row.get("id"))
            )
            agent_role = row.get("role") or ""
            agent_goal = row.get("goal") or ""
            agent_profile = row.get("agent_profile") or "/images/chat-icon.png"
            agent_id = row.get("id")

            sub_job_id = str(uuid.uuid4())
            # 서브에이전트 도구 호출은 이 에이전트 이름으로 로깅되도록 전용 ExecutionContext 사용.
            # (동일 exec_ctx를 쓰면 서브가 호출한 도구가 모두 root_orchestrator로 기록됨)
            sub_exec_ctx = ExecutionContext(
                correlation_id=exec_ctx.correlation_id,
                agent_path=list(exec_ctx.agent_path) + [agent_name],
                task_id=exec_ctx.task_id,
                proc_inst_id=exec_ctx.proc_inst_id,
            )
            if idx < len(subagents):
                sub_tools = subagents[idx].get("tools", [])
                _instrument_tools(
                    sub_tools,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    crew_type="action",
                    job_id_getter=lambda sj=sub_job_id: sj,
                    job_tool_log=subagent_tool_log,
                    exec_ctx=sub_exec_ctx,
                    event_queue=event_queue,
                )

            subagent_contexts.append(
                {
                    "job_id": sub_job_id,
                    "role": agent_role,
                    "name": agent_name,
                    "goal": agent_goal,
                    "agent_profile": agent_profile,
                    "agent_id": agent_id,
                }
            )

        name_to_context = {ctx["name"]: ctx for ctx in subagent_contexts}
        (
            subagent_event_middleware,
            subagent_middleware_state,
        ) = _create_subagent_event_middleware(
            event_queue,
            proc_inst_id,
            task_id,
            name_to_context,
            subagent_tool_log,
        )
        extra_middleware = [subagent_event_middleware] if subagent_event_middleware else []

        model_name = extras.get("model") or "openai:gpt-4o"

        # 1단계: Planner Agent 로 순수 플랜만 생성하고 planning 이벤트를 발행한다.
        base_rules = default_orchestrator_prompt()
        feedback_summary = extras.get("summarized_feedback") or ""
        form_fields = extras.get("form_fields")
        form_html = extras.get("form_html")
        form_info = None
        if form_fields or form_html:
            form_info = {"fields": form_fields, "html": form_html}

        planner_system_prompt = build_orchestrator_system_prompt(
            base_rules=base_rules,
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
            plan_text=None,
        )
        planner_agent = build_planner_agent(
            model_name=model_name,
            system_prompt=planner_system_prompt,
        )

        # Task Execution Planner용 planning 이벤트 (플랜 전용)
        planner_task_description = "\n\n".join(
            [part for part in [description, instruction] if part]
        ).strip()
        planner_goal = (
            "Your goal is to create an extremely detailed, step-by-step plan based on "
            "the tasks and tools available to each agent so that they can perform the "
            "tasks in an exemplary manner."
        )
        _enqueue_status(
            event_queue,
            "task_started",
            {
                "goal": planner_goal,
                "name": "Unknown",
                "role": "Task Execution Planner",
                "agent_profile": "/images/chat-icon.png",
                "task_description": planner_task_description,
            },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="planning",
            agent_name="root_orchestrator",
        )

        plan_text: str = ""
        try:
            planner_result = await planner_agent.ainvoke(
                {"messages": [{"role": "user", "content": query or ""}]}
            )
            planner_messages = planner_result.get("messages", [])
            planner_raw = ""
            if planner_messages:
                last_msg = planner_messages[-1]
                planner_content = (
                    last_msg.get("content")
                    if isinstance(last_msg, dict)
                    else getattr(last_msg, "content", None)
                )
                planner_raw = _extract_plain_text(planner_content)
            plan_text = _extract_plan_block(planner_raw) or _extract_plain_text(planner_raw).strip()
        except HitlInterrupt as hi:
            # A tool signalled that human input is required. Record a human_asked
            # interrupt event and return waiting_for_human so the caller can pause.
            decision = hi.decision or {}
            question = decision.get("question") or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            choices = decision.get("choices") or []
            hitl_agent_name = "root_orchestrator"
            for ctx in subagent_contexts:
                if ctx.get("job_id") == hi.job_id:
                    hitl_agent_name = ctx.get("name") or hitl_agent_name
                    break
            try:
                hitl_job_id = str(uuid.uuid4())
                _enqueue_status(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": choices,
                        "reason": decision.get("reason"),
                        "tool_call_context": {
                            "job_id": hi.job_id,
                            "tool_name": hi.tool_name,
                        },
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
            except Exception:
                logger.exception("Failed to enqueue human_asked event from tool HITL", exc_info=True)

            return {
                "status": "waiting_for_human",
                "run_state": {
                    "reason": decision.get("reason") or "Tool requested human input.",
                    "pending_field": "human_input",
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": list(exec_ctx.agent_path),
                    "tool_name": hi.tool_name,
                    "tool_call_context": {
                        "job_id": hi.job_id,
                        "tool_name": hi.tool_name,
                    },
                },
            }
        except Exception:
            logger.exception("Planner agent failed; continuing without explicit plan.", exc_info=True)

        _enqueue_status(
            event_queue,
            "task_completed",
            {
                "plans": plan_text,
            },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="planning",
            agent_name="root_orchestrator",
        )

        # 2단계: Worker Orchestrator 에게 plan_text 를 주입하여 실제 서브 에이전트 실행을 수행한다.
        system_prompt = build_orchestrator_system_prompt(
            base_rules=base_rules,
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
            plan_text=plan_text or None,
        )

        # 오케스트레이터에는 task()와 request_human_input만 부여. 스킬/MCP 도구는
        # 서브에이전트에만 두어, 루트가 직접 업무를 수행하지 않고 위임만 하도록 한다.
        orchestrator = build_orchestrator(
            model_name=model_name,
            system_prompt=system_prompt,
            subagents=subagents,
            tools=[],  # 도메인 도구 없음 → 반드시 서브에이전트 task()로 위임
            middleware=extra_middleware,
        )

        # 이 시점 이후에 발생하는 모든 도구 호출은 job_uuid와 동일한 job_id로 로깅된다.
        current_job_id["value"] = job_uuid

        try:
            result = await orchestrator.ainvoke(
                {"messages": [{"role": "user", "content": query or ""}]}
            )
        except HitlInterrupt as hi:
            # A tool signalled that human input is required during worker execution.
            # Record a human_asked interrupt event and return waiting_for_human so the caller can pause.
            decision = hi.decision or {}
            question = decision.get("question") or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            choices = decision.get("choices") or []
            hitl_agent_name = "root_orchestrator"
            for ctx in subagent_contexts:
                if ctx.get("job_id") == hi.job_id:
                    hitl_agent_name = ctx.get("name") or hitl_agent_name
                    break
            try:
                hitl_job_id = str(uuid.uuid4())
                _enqueue_status(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": choices,
                        "reason": decision.get("reason"),
                        "tool_call_context": {
                            "job_id": hi.job_id,
                            "tool_name": hi.tool_name,
                        },
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
            except Exception:
                logger.exception("Failed to enqueue human_asked event from tool HITL", exc_info=True)

            return {
                "status": "waiting_for_human",
                "run_state": {
                    "reason": decision.get("reason") or "Tool requested human input.",
                    "pending_field": "human_input",
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": list(exec_ctx.agent_path),
                    "tool_name": hi.tool_name,
                    "tool_call_context": {
                        "job_id": hi.job_id,
                        "tool_name": hi.tool_name,
                    },
                },
            }
        except Exception:
            logger.exception("Worker orchestrator failed.", exc_info=True)
            raise

        # LangGraph DeepAgents 그래프 레벨에서 request_human_input 도구 호출로 인해
        # interrupt 가 발생한 경우(__interrupt__ 페이로드)를 처리한다.
        interrupt_payload = None
        try:
            interrupt_payload = extract_interrupt_payload(result)
        except Exception:
            logger.exception("Failed to extract graph-level interrupt payload", exc_info=True)

        if interrupt_payload:
            question = (
                interrupt_payload.get("question")
                if isinstance(interrupt_payload, dict)
                else None
            ) or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            hitl_agent_name = (
                (exec_ctx.agent_path[-1] if exec_ctx.agent_path else None)
                or "root_orchestrator"
            )
            try:
                hitl_job_id = str(uuid.uuid4())
                _enqueue_status(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": [],
                        "interrupt_payload": interrupt_payload,
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue human_asked event from graph-level HITL", exc_info=True
                )

            return {
                "status": "waiting_for_human",
                "run_state": {
                    "reason": "DeepAgents graph requested human input.",
                    "pending_field": "human_input",
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": list(exec_ctx.agent_path),
                    "tool_name": "request_human_input",
                    "tool_call_context": {
                        "job_id": hitl_job_id,
                        "tool_name": "request_human_input",
                    },
                },
            }

        final_messages = result.get("messages", [])
        raw_text = ""
        if final_messages:
            last = final_messages[-1]
            content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
            raw_text = _extract_plain_text(content)

        # 서브 에이전트 task_started/task_completed는 미들웨어에서 task() 호출 시 발행된다.
        # 다만, 환경에 따라 AgentMiddleware 가 동작하지 않거나, task() 호출 계층이 미들웨어와
        # 분리되어 있을 수 있으므로 아래 조건에서 fallback 이벤트를 발행한다.
        any_subagent_invoked = any(
            subagent_tool_log.get(ctx["job_id"]) for ctx in subagent_contexts
        )
        middleware_saw_task = bool(
            (subagent_middleware_state or {}).get("saw_task_call")
        )
        if not subagent_event_middleware or not middleware_saw_task:
            if subagent_event_middleware and not middleware_saw_task and any_subagent_invoked:
                logger.warning(
                    "Subagent middleware was attached but did not observe any task() calls; "
                    "emitting subagent task_started/task_completed events as fallback."
                )
            for ctx in subagent_contexts:
                sub_job_id = ctx["job_id"]
                used_tools = subagent_tool_log.get(sub_job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                _enqueue_status(
                    event_queue,
                    "task_started",
                    {
                        "message": f"서브 에이전트 '{ctx['name']}' 실행을 시작했습니다.",
                        "role": ctx["role"],
                        "goal": ctx["goal"],
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=sub_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_started",
                    phase="reasoning",
                    agent_name=ctx["name"],
                )
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "message": f"서브 에이전트 '{ctx['name']}' 실행이 완료되었습니다.",
                        "summary": 작업_텍스트,
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=sub_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_finished",
                    phase="reasoning",
                    agent_name=ctx["name"],
                )

        form_fields = extras.get("form_fields")
        pure_form_data, wrapped_result, original_wo_form, _, _ = convert_agent_output(
            raw_text, form_id, form_fields
        )
        if pure_form_data is None:
            pure_form_data = {}

        # 폼이 없거나 defaultform인 경우, 항상 JSON 파싱 가능한 고정 포맷으로 래핑
        has_form = bool(form_fields) and form_id not in (None, "", "defaultform")
        if not has_form:
            base_text = original_wo_form or wrapped_result.get("cleaned") or ""
            # 모델이 리스트/딕셔너리 구조를 문자열로 반환한 경우에도
            # 내부 text/content 필드만 추출하여 순수 텍스트로 만든다.
            final_text = _extract_plain_text(base_text).strip()
            result_status = "SUCCESS" if final_text else "Fail"
            final_result_payload = {
                "output": {
                    "status": result_status,
                    "result": final_text,
                },
            }
            final_event_data = final_result_payload
        else:
            final_event_data = pure_form_data

        end_job = str(uuid.uuid4())
        _enqueue_status(
            event_queue,
            "task_started",
            {
                "message": "최종 결과 생성을 시작했습니다.",
            },
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="final",
            agent_name="final_result",
        )
        _enqueue_status(
            event_queue,
            "task_completed",
            {
                "message": "최종 결과 생성이 완료되었습니다.",
                "result": final_event_data,
            },
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="final",
            agent_name="final_result",
        )
        _enqueue_artifact(
            event_queue,
            name="deepagents_action_result",
            description="DeepAgents 멀티 에이전트 실행 결과",
            # wrapped_result는 dict/list 형태의 JSON-직렬화 가능한 객체여야 하며,
            # 그대로 events.data 및 todolist.draft(jsonb)에 저장된다.
            text=wrapped_result,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=exec_ctx,
        )

        logger.info("DeepAgents 멀티 에이전트 실행 완료")
        return None

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("작업 취소 요청됨")
