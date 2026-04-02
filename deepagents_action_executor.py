"""DeepAgents executor: ProcessGPT A2A 래퍼 + DeepAgents orchestrator."""

from __future__ import annotations

import asyncio
import ast
import copy
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import TaskStatusUpdateEvent, TaskState, TaskArtifactUpdateEvent
from a2a.utils import new_agent_text_message, new_text_artifact

from deepagents_hitl import extract_interrupt_payload, get_hitl_tools
from skill_discovery_subagent import (
    build_skill_discovery_subagent,
    SKILL_DISCOVERY_SUBAGENT_NAME,
    synthetic_skill_discovery_agent_row,
)
from subagents import (
    build_subagent_configs,
    hinted_skill_slugs_from_query,
    parse_skills_csv,
)
from deepagents_factory import (
    build_orchestrator,
    build_orchestrator_system_prompt,
    default_orchestrator_prompt,
)
from output_utils import convert_agent_output
from runtime_config import resolve_model_routing
from database import get_latest_human_response
from deepagents_backend import tenant_skill_sources_for_middleware
from skill_volume_subagent_scope_middleware import SubagentJobScopeMiddleware
from skill_volume_tool_events import (
    SkillVolumeToolEventBinding,
    skill_volume_active_exec_ctx,
    skill_volume_active_job_id,
    skill_volume_tool_event_binding,
)
from tagging_safe_tool_loader import TaggingSafeToolLoader
from processgpt_agent_utils.utils.context_manager import set_context

logger = logging.getLogger(__name__)

# tenant_mcp 에 포함돼 있어도 연결 시도하지 않음 (샌드박스로 대체, subagents.VIRTUAL_TOOL_NAMES 와 정합)
_MCP_SERVER_KEYS_NEVER_LOAD: frozenset[str] = frozenset({"computer-use"})


def _mcp_config_without_blocked_servers(raw: Any) -> dict[str, Any]:
    """Return a copy of tenant MCP config with blocked servers removed from ``mcpServers``."""
    cfg = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        return cfg
    removed: list[str] = []
    for key in list(servers.keys()):
        norm = str(key).strip().lower().replace("_", "-")
        if norm in _MCP_SERVER_KEYS_NEVER_LOAD:
            removed.append(str(key))
            del servers[key]
    if removed:
        logger.info("tenant_mcp: 다음 MCP 서버는 로드하지 않음(코드 차단): %s", removed)
    return cfg


class MissingSubagentsConfigurationError(RuntimeError):
    """Raised when no subagents are configured; the orchestrator must not run alone."""

    friendly: str

    def __init__(self, friendly: str | None = None) -> None:
        msg = friendly or (
            "이 할 일에 서브 에이전트가 연결되어 있지 않습니다. "
            "활동에 최소 한 명의 에이전트를 지정한 뒤 다시 실행해 주세요."
        )
        self.friendly = msg
        super().__init__(msg)


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
        agent_role: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.agent_path: list[str] = list(agent_path) if agent_path else []
        # If current_agent is not provided, derive it from the path when possible.
        self.current_agent: str | None = current_agent or (
            self.agent_path[-1] if self.agent_path else None
        )
        self.task_id = task_id
        self.proc_inst_id = proc_inst_id
        _ar = (agent_role or "").strip()
        self.agent_role: str | None = _ar or None

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
        if self.agent_role:
            meta["role"] = self.agent_role
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


def _ensure_required_root_task_started_fields(payload: dict[str, Any], *, event_type: str) -> None:
    """Depth 1 task_started payload must always include role/goal/agent_profile."""
    if event_type != "task_started":
        return
    if payload.get("depth") != 1:
        return

    agent_name = str(payload.get("agent_name") or "").strip()

    if not payload.get("role"):
        if agent_name == "작업 실행 기획자":
            payload["role"] = "작업 실행 기획자"
        elif agent_name == "최종 결과 작성자":
            payload["role"] = "최종 결과 작성자"
        else:
            payload["role"] = agent_name or "agent"

    if not payload.get("goal"):
        if agent_name == "작업 실행 기획자":
            payload["goal"] = str(payload.get("message") or "요청된 작업을 수행합니다.")
        elif agent_name == "최종 결과 작성자":
            payload["goal"] = str(payload.get("message") or "최종 응답을 작성합니다.")
        else:
            payload["goal"] = str(payload.get("message") or "작업을 시작합니다.")

    if not payload.get("agent_profile"):
        payload["agent_profile"] = "/images/chat-icon.png"


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
    _ensure_required_root_task_started_fields(payload, event_type=event_type)

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
    # NOTE:
    # - tool_usage_finished / planning-level task_completed 는 terminal 상태로 간주하지 않는다.
    # - 최종 완료(final phase + result crew)에서만 completed/final=true 를 사용한다.
    if event_type == "human_asked":
        state = TaskState.input_required
        final_flag = True
    elif (
        event_type == "task_completed"
        and effective_phase == "final"
        and crew_type == "result"
    ):
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
    subagent_skill_log: dict[str, list[str]],
    structured_skill_handoff_by_job: dict[str, dict[str, Any]] | None = None,
    on_before_subagent_start: Any | None = None,
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
        # Captured from `스킬 탐색 도우미` output; injected into subsequent domain task() calls.
        "skill_handoff": None,
        "skill_handoff_raw": "",
        "skill_handoff_by_agent": {},
        "skill_handoff_structured": structured_skill_handoff_by_job or {},
    }

    if AgentMiddleware is None or ToolCallRequest is None:
        return None, state

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

    def _extract_skill_handoff_from_text(text: str) -> dict[str, Any] | None:
        """Extract a structured skill handoff JSON object from tool output text.

        Expected format (anywhere in text):
          [SKILL_HANDOFF_V1]
          { ... valid JSON ... }
        """
        if not isinstance(text, str) or not text.strip():
            return None
        blob = text
        m = re.search(r"\[SKILL_HANDOFF_V1\]\s*(\{[\s\S]*?\})\s*(?:$|\n\[)", blob)
        if not m:
            # Fallback: try to capture to end of text.
            m = re.search(r"\[SKILL_HANDOFF_V1\]\s*(\{[\s\S]*\})\s*$", blob)
        if not m:
            return None
        raw = (m.group(1) or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        # Minimal sanity keys; keep permissive to avoid brittle failures.
        parsed.setdefault("version", 1)
        return parsed

    def _build_skill_handoff_directive(handoff: dict[str, Any]) -> str:
        """Build a deterministic directive block to prepend to domain subagent instructions."""
        try:
            compact = json.dumps(handoff, ensure_ascii=False)
        except Exception:
            compact = "{}"
        selected = ""
        try:
            selected = str(handoff.get("selected_primary") or "").strip()
        except Exception:
            selected = ""
        primary_source = ""
        try:
            ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
            if ranked and isinstance(ranked[0], dict):
                ev = ranked[0].get("evidence") if isinstance(ranked[0].get("evidence"), dict) else {}
                primary_source = str(ev.get("source") or "").strip().lower()
        except Exception:
            primary_source = ""
        return (
            "[SKILL_HANDOFF_REQUIRED_V1]\n"
            "필수: 아래 `스킬 핸드오프`(스킬 탐색 도우미 결과)를 작업 시작 시 최우선으로 참고해야 합니다.\n"
            "- 스킬 탐색 도우미가 이미 `read_skill_document`로 문서를 조회·기록했다면, 실행 단계에서는 "
            "동일 작업을 반복하지 말고 핸드오프·탐색 결과를 그대로 사용해 샌드박스 물리화·실행으로 진행하세요.\n"
            "- 작업 계획/실행 시, 이 핸드오프에 명시된 스킬/문서/엔드포인트를 우선 사용하세요.\n"
            "- 실행이 필요하면, 아래 2가지 케이스 중 해당하는 방식으로 스킬을 샌드박스에 물리화한 뒤 진행하세요.\n"
            "  - (파일형/FS 마운트형) Tier1 볼륨(/app/skills)에 스킬이 있으면: "
            "`sandbox_copy_mounted_skill(workspace_id, tenant_id, skill_name, dest_subdir='skill')`\n"
            "  - (문서형/MCP형) Tier1에 없고 MCP(claude-skills)로만 존재하면:\n"
            "    1) 탐색 단계에서 문서 목록/본문을 확보하지 않았다면 "
            "`read_skill_document(skill_name=<name>, document_path=null)`로 목록을 받고\n"
            "    2) 핸드오프의 `documents[].path`(또는 목록에서 고른 경로들)를 `read_skill_document`로 읽어온 뒤\n"
            "    3) `sandbox_write_files(workspace_id, files_json)`로 `skill_docs/<skill_name>/<path>`에 저장하세요.\n"
            "    4) 필요하면 위 저장된 문서를 근거로 실행 스크립트를 생성해 `sandbox_write_and_run`으로 실행하세요.\n"
            + (f"- 권장 primary skill_name: {selected}\n" if selected else "")
            + (f"- 권장 primary evidence.source: {primary_source}\n" if primary_source else "")
            + "\n[SKILL_HANDOFF_V1]\n"
            + compact
            + "\n"
        )

    def _select_skill_handoff_for_domain(subagent_type: str) -> dict[str, Any] | None:
        """Pick the best available handoff for a given domain subagent."""
        if not isinstance(subagent_type, str) or not subagent_type.strip():
            return None
        try:
            by_agent = state.get("skill_handoff_by_agent")
            if isinstance(by_agent, dict):
                per = by_agent.get(subagent_type)
                if isinstance(per, dict) and per:
                    return per
        except Exception:
            pass
        h = state.get("skill_handoff")
        return h if isinstance(h, dict) and h else None

    def _missing_skill_discovery_checklist_tools(*, job_id: str) -> list[str]:
        used = set(subagent_tool_log.get(job_id, []) or [])
        required_fs = {"skill_volume_probe"}
        required_mcp = {"find_helpful_skills", "read_skill_document"}
        missing = sorted((required_fs | required_mcp) - used)
        return missing

    def _inject_skill_discovery_retry_directive(
        *,
        args: dict[str, Any],
        call: dict[str, Any],
        request: Any,
        job_id: str,
        attempt: int,
        missing: list[str],
    ) -> None:
        marker = "[SKILL_DISCOVERY_RETRY_V1]"
        used = sorted(set(subagent_tool_log.get(job_id, []) or []))
        directive = (
            f"{marker}\n"
            "필수: 직전 스킬 탐색이 체크리스트를 충족하지 못했습니다. 지금 즉시 재시도하세요.\n"
            "- 이번 재시도에서는 아래 누락 도구를 반드시 최소 1회씩 호출해야 합니다.\n"
            f"- missing_tools: {missing}\n"
            f"- used_tools_so_far: {used}\n"
            f"- attempt: {attempt}\n"
            "추가 규칙:\n"
            "- `skill_volume_probe`는 allowed_skill_names 전체를 대상으로 호출하세요.\n"
            "- `find_helpful_skills`는 tenant_id + allowed_skill_names를 포함해서 호출하세요.\n"
            "- `read_skill_document`는 최소 1개 후보 스킬에 대해 document_path=null로 목록을 받고, "
            "그 다음 실제 문서를 1개 이상 읽어(또는 핵심 레퍼런스를 확인해) 엔드포인트/사용법 근거를 확보하세요.\n"
            "- 마지막 출력에는 반드시 `[SKILL_HANDOFF_V1]` JSON을 포함하세요.\n"
        )
        if not isinstance(args, dict):
            return
        for key in ("instruction", "goal", "description", "prompt", "query", "task"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                if marker not in v:
                    args[key] = directive + "\n" + v
                break
        else:
            args["instruction"] = directive
        if isinstance(call, dict):
            call["args"] = args
            try:
                setattr(request, "call", call)
            except Exception:
                pass

    class SubagentEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            tool_name = getattr(getattr(request, "tool", None), "name", None)
            if tool_name != "task":
                return await handler(request)

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

            call = getattr(request, "call", None) or {}
            args = call.get("args", {}) if isinstance(call, dict) else {}
            subagent_type = args.get("subagent_type") or args.get("subagent_name") or ""
            ctx = name_to_context.get(subagent_type) if subagent_type else None
            if not ctx:
                return await handler(request)

            # Fail-fast: do not allow domain subagents to run before we have a valid skill handoff.
            # This prevents low-quality "invent random endpoints" behavior when skill discovery is incomplete.
            try:
                if subagent_type and subagent_type != SKILL_DISCOVERY_SUBAGENT_NAME:
                    handoff = _select_skill_handoff_for_domain(str(subagent_type))
                    if not isinstance(handoff, dict) or not handoff:
                        return {
                            "error": "skill_handoff_missing",
                            "message": (
                                "도메인 서브 에이전트를 실행하기 전에 `스킬 탐색 도우미`가 "
                                "`[SKILL_HANDOFF_V1]` JSON을 포함한 결과를 먼저 반환해야 합니다. "
                                "현재 런에서는 유효한 스킬 핸드오프가 없어 도메인 실행을 차단합니다."
                            ),
                            "requires_human": False,
                        }
            except Exception:
                logger.exception("Failed while enforcing skill_handoff_missing fail-fast", exc_info=True)

            # Inject skill handoff into domain subagent task text (if available).
            # This makes cross-subagent sharing deterministic, rather than relying on prompt-following.
            try:
                if subagent_type and subagent_type != SKILL_DISCOVERY_SUBAGENT_NAME:
                    handoff = _select_skill_handoff_for_domain(str(subagent_type))
                    if isinstance(handoff, dict) and handoff:
                        marker = "[SKILL_HANDOFF_REQUIRED_V1]"
                        directive = _build_skill_handoff_directive(handoff)
                        for key in ("instruction", "goal", "description", "prompt", "query", "task"):
                            v = args.get(key)
                            if isinstance(v, str) and v.strip():
                                if marker not in v:
                                    args[key] = directive + "\n" + v
                                break
                        else:
                            args["instruction"] = directive
                        if isinstance(call, dict):
                            call["args"] = args
                            try:
                                setattr(request, "call", call)
                            except Exception:
                                pass
            except Exception:
                logger.exception("Failed to inject skill handoff into domain subagent task args", exc_info=True)

            # Enforce mem0 usage for every subagent invocation by injecting a
            # deterministic "call mem0 first" directive into the delegated task text.
            # This is stronger than relying on subagent prompts, and avoids "mem0 often omitted".
            try:
                marker = "[MEM0_REQUIRED_V1]"
                delegated_goal_for_mem0 = _derive_subagent_goal_from_task_args(
                    args, str(ctx.get("goal") or "")
                )
                mem0_directive = (
                    f"{marker}\n"
                    "필수: 이 서브에이전트 작업은 시작 직후 `mem0`를 최소 1회 호출해야 합니다.\n"
                    "- 먼저 `mem0`를 호출해 과거 피드백/주의사항/필수 매개변수/자격증명 단서를 검색하세요.\n"
                    "- mem0 query에는 아래 두 문장을 그대로 포함하세요.\n"
                    f"  1) 작업 목표: {delegated_goal_for_mem0}\n"
                    "  2) 현재 워크아이템 전체(Description/Instruction/InputData)를 바탕으로 필요한 단서\n"
                    "- mem0 결과가 '없음'이어도 호출 자체는 반드시 수행한 뒤 다음 단계로 진행하세요.\n"
                )

                if isinstance(args, dict):
                    for key in ("instruction", "goal", "description", "prompt", "query", "task"):
                        v = args.get(key)
                        if isinstance(v, str) and v.strip():
                            if marker not in v:
                                args[key] = mem0_directive + "\n" + v
                            break
                    else:
                        # If no obvious text field exists, attach one.
                        args["instruction"] = mem0_directive
                    if isinstance(call, dict):
                        call["args"] = args
                        try:
                            setattr(request, "call", call)
                        except Exception:
                            pass
            except Exception:
                logger.exception("Failed to inject mem0 directive into subagent task args", exc_info=True)

            # 실제 매핑 가능한 subagent task()를 감싼 경우만 True.
            # unknown task()만 있었던 런에서는 fallback pair를 유지해야 한다.
            state["saw_task_call"] = True

            job_id = ctx["job_id"]
            delegated_goal = _derive_subagent_goal_from_task_args(args, str(ctx.get("goal") or ""))
            _enqueue_status(
                event_queue,
                "task_started",
                {
                    "role": ctx["role"],
                    "name": ctx["name"],
                    "goal": delegated_goal,
                    "agent_profile": ctx["agent_profile"],
                    "agent_id": ctx.get("agent_id"),
                },
                proc_inst_id,
                task_id,
                "action",
                job_id=job_id,
                exec_ctx=ctx.get("exec_ctx"),
            )
            try:
                # Skill discovery must not "finish early". Retry until checklist is satisfied.
                result = None
                if subagent_type == SKILL_DISCOVERY_SUBAGENT_NAME:
                    max_attempts = 3
                    last_raw = ""
                    last_handoff: dict[str, Any] | None = None
                    last_missing: list[str] = []
                    for attempt in range(1, max_attempts + 1):
                        result = await handler(request)
                        raw = ""
                        if isinstance(result, dict):
                            raw = _extract_plain_text(result.get("content"))
                        else:
                            raw = _extract_plain_text(getattr(result, "content", None))
                        last_raw = raw
                        handoff = _extract_skill_handoff_from_text(raw)
                        last_handoff = handoff if isinstance(handoff, dict) else None
                        missing = _missing_skill_discovery_checklist_tools(job_id=job_id)
                        last_missing = missing
                        # Require BOTH: valid handoff JSON + mandatory tool checklist.
                        structured = {}
                        try:
                            structured = (state.get("skill_handoff_structured") or {}).get(job_id) or {}
                        except Exception:
                            structured = {}
                        if not missing and (isinstance(last_handoff, dict) and last_handoff or isinstance(structured, dict) and structured):
                            chosen = (
                                last_handoff
                                if (isinstance(last_handoff, dict) and last_handoff)
                                else structured
                            )
                            state["skill_handoff"] = chosen
                            try:
                                by_agent = chosen.get("by_agent") if isinstance(chosen, dict) else None
                                if isinstance(by_agent, dict) and by_agent:
                                    state["skill_handoff_by_agent"] = by_agent
                            except Exception:
                                pass
                            state["skill_handoff_raw"] = raw[:12000] if raw else ""
                            break
                        if attempt < max_attempts:
                            _inject_skill_discovery_retry_directive(
                                args=args,
                                call=call if isinstance(call, dict) else {},
                                request=request,
                                job_id=job_id,
                                attempt=attempt + 1,
                                missing=missing or ["[SKILL_HANDOFF_V1]"],
                            )
                            continue
                        raise RuntimeError(
                            "스킬 탐색 도우미가 재시도 후에도 필수 체크리스트를 충족하지 못했습니다. "
                            f"missing={last_missing} handoff_ok={bool(last_handoff)} "
                            f"used={sorted(set(subagent_tool_log.get(job_id, []) or []))} "
                            f"raw_tail={(last_raw or '')[-800:]}"
                        )
                else:
                    result = await handler(request)

                used_tools = subagent_tool_log.get(job_id, [])
                used_skills = subagent_skill_log.get(job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                if used_skills:
                    작업_텍스트 += f" | 참고/사용한 스킬: {', '.join(sorted(set(used_skills)))}"
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": 작업_텍스트,
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
                return result
            except Exception:
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": "작업 실행 중 오류가 발생했습니다.",
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
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
    _enqueue_status(
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
        "action",
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
    _enqueue_status(
        event_queue,
        "task_completed",
        {
            "message": f"서브 에이전트 '{ctx['name']}' 실행이 완료되었습니다.",
            "summary": 작업_텍스트,
            "role": ctx.get("role"),
            "name": ctx.get("name"),
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


def _is_korean_user_input(text: str) -> bool:
    """Return True if the text contains Korean (Hangul) characters."""
    if not (text or "").strip():
        return False
    return bool(re.search(r"[가-힣]", text))


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
    - Interrupt timing (no natural-language matching in executor):
        - Default: defer — return payload to the model so it can call more tools.
        - Immediate graph interrupt only if the tool sets one of:
          ``hitl_immediate: true``, ``force_human: true``, ``defer_hitl: false``,
          ``credential_required: true``, ``sensitive: true``.
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


def _resolve_pending_field_from_decision(decision: dict[str, Any], default: str = "human_response") -> str:
    """Infer pending input field from HITL decision payload."""
    if not isinstance(decision, dict):
        return default
    for key in ("pending_field", "field", "input_field"):
        value = decision.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    schema = decision.get("input_schema")
    if isinstance(schema, dict):
        for key in ("pending_field", "field", "input_field"):
            value = schema.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split()).lower()


def _is_duplicate_hitl_request(
    previous_run_state: dict[str, Any],
    *,
    question: str,
    pending_field: str,
) -> bool:
    """Detect repeated HITL prompts for the same unresolved field."""
    if not isinstance(previous_run_state, dict):
        return False
    prev_field = str(previous_run_state.get("pending_field") or "").strip().lower()
    if not prev_field:
        return False
    if prev_field != str(pending_field or "").strip().lower():
        return False
    prev_question = _normalize_text(previous_run_state.get("last_question"))
    now_question = _normalize_text(question)
    return bool(prev_question and now_question and prev_question == now_question)


def _should_defer_hitl_interrupt(decision: dict[str, Any]) -> bool:
    """Return True to defer HITL so the agent can attempt more tool calls first.

    Policy: **human intervention is the last resort.** Tool outputs that set
    ``requires_human: true`` are deferred by default (returned to the model) so
    the run can continue with mem0, MCP, sandbox, FS, etc.

    We do **not** inspect ``question``/``reason`` text for keywords (no locale-
    specific or domain-specific string lists). Wording like “담당자에게 허가가 나면”
    or “approval required” does not change routing; only structured fields do.

    Immediate HITL (return False = do not defer) when ``raw_payload`` is a dict and
    any of these are set by the **tool implementation**:
    - ``defer_hitl`` is ``False``
    - ``force_human`` is ``True``
    - ``hitl_immediate`` is ``True``
    - ``credential_required`` is ``True`` (tool declares human must supply secret)
    - ``sensitive`` is ``True`` (tool declares sensitive / policy-bound path)
    """
    if not isinstance(decision, dict):
        return False
    raw = decision.get("raw_payload")
    if not isinstance(raw, dict):
        return True
    if raw.get("defer_hitl") is False:
        return False
    if raw.get("force_human") is True:
        return False
    if raw.get("hitl_immediate") is True:
        return False
    if raw.get("credential_required") is True:
        return False
    if raw.get("sensitive") is True:
        return False
    return True


def _classify_tenant_context_tool(base_tool: Any) -> str | None:
    """Map a loaded tool to mem0 / memento / dmn_rule bucket for HITL prerequisite policy."""
    name = str(getattr(base_tool, "name", "") or "").lower()
    server = str(getattr(base_tool, "_mcp_server", "") or "").lower().replace("_", "-")
    combined = f"{name} {server}"
    if "memento" in combined:
        return "memento"
    if "mem0" in combined:
        return "mem0"
    if "dmn" in combined:
        return "dmn_rule"
    return None


def _required_context_categories_from_tools(tools: list[Any]) -> set[str]:
    out: set[str] = set()
    for t in tools:
        cat = _classify_tenant_context_tool(t)
        if cat:
            out.add(cat)
    return out


def _record_context_tool_attempt(
    tool_obj: Any,
    context_tool_tracker: dict[str, Any] | None,
) -> None:
    if context_tool_tracker is None:
        return
    cat = _classify_tenant_context_tool(tool_obj)
    if not cat:
        return
    attempted = context_tool_tracker.get("attempted_global")
    if isinstance(attempted, set):
        attempted.add(cat)


def _missing_context_for_hitl(context_tool_tracker: dict[str, Any]) -> set[str]:
    required = context_tool_tracker.get("required_global")
    attempted = context_tool_tracker.get("attempted_global")
    if not isinstance(required, set):
        return set()
    if not isinstance(attempted, set):
        return set(required)
    return set(required) - set(attempted)




def _make_request_human_input_context_guard(
    context_tool_tracker: dict[str, Any] | None,
) -> Any:
    """Block orchestrator HITL until tenant mem0/memento/dmn_rule tools have been attempted (when configured)."""

    def _guard(base_tool: Any, tool_input: Any) -> Any | None:
        if context_tool_tracker is None:
            return None
        if str(getattr(base_tool, "name", "") or "") != "request_human_input":
            return None
        missing = _missing_context_for_hitl(context_tool_tracker)
        if not missing:
            return None
        # `request_human_input` 도구는 반환 타입이 `str`이므로,
        # 가드가 차단할 때도 반드시 문자열만 반환해야 합니다.
        payload = {
            "error": "tenant_context_tools_required_before_hitl",
            "missing_context_tool_categories": sorted(missing),
            "message": (
                "테넌트에 연결된 지식·기억·규칙 도구(mem0/memento/dmn_rule)를 아직 호출하지 않았습니다. "
                "먼저 task()로 적절한 서브에이전트에 위임하여 해당 도구들을 호출한 뒤, "
                "그래도 부족할 때만 request_human_input 을 사용하세요."
            ),
            "requires_human": False,
        }
        return json.dumps(payload, ensure_ascii=False)

    return _guard


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


def _join_todo_contents(value: Any) -> str:
    """Join todo item `content` values into a single plain-text message."""
    parsed = value
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                try:
                    parsed = ast.literal_eval(raw)
                except Exception:
                    parsed = value

    if isinstance(parsed, dict):
        parsed = parsed.get("todos", parsed)

    if isinstance(parsed, list):
        parts: list[str] = []
        for item in parsed:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return "\n".join(parts)

    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _extract_form_field_keys(form_fields: Any) -> list[str]:
    """Best-effort extract of field keys from form schema."""
    keys: list[str] = []
    if not isinstance(form_fields, list):
        return keys
    for field in form_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key") or field.get("name") or field.get("id")
        if isinstance(key, str):
            stripped = key.strip()
            if stripped and stripped not in keys:
                keys.append(stripped)
    return keys


def _build_form_event_result(
    form_id: str | None,
    form_fields: Any,
    pure_form_data: Any,
    fallback_text: str = "",
) -> dict[str, Any]:
    """Build JSON for final result event: {form_id: {field_key: value, ...}}."""
    field_keys = _extract_form_field_keys(form_fields)
    source = pure_form_data if isinstance(pure_form_data, dict) else {}
    inner: dict[str, Any] = {}

    for key in field_keys:
        value = source.get(key)
        if value is None:
            value = ""
        inner[key] = value

    # If model returned only text but schema has a single field,
    # map that text to the only field for better downstream UX.
    if fallback_text.strip() and len(field_keys) == 1 and not inner.get(field_keys[0]):
        inner[field_keys[0]] = fallback_text.strip()

    fid = (form_id or "").strip()
    if fid:
        return {fid: inner}
    return inner


def _coerce_tool_input_dict(tool_input: Any) -> dict[str, Any] | None:
    """Best-effort coercion for tool inputs that should be JSON objects."""
    if isinstance(tool_input, dict):
        return dict(tool_input)
    if isinstance(tool_input, str):
        raw = tool_input.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            return None
    return None


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


def _strip_plan_blocks(source: str) -> str:
    """Remove any [PLAN]...[/PLAN] blocks from user-facing output."""
    if not source or not isinstance(source, str):
        return ""
    text = source
    # Remove complete blocks.
    text = re.sub(r"\[PLAN\].*?\[/PLAN\]\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    # If there's a dangling opening tag, drop everything from it onward.
    m = re.search(r"\[PLAN\]", text, flags=re.IGNORECASE)
    if m:
        text = text[: m.start()].rstrip()
    return text.strip()


def _extract_result_block(source: str) -> str:
    """Extract the [RESULT]...[/RESULT] block from output, if present."""
    if not source or not isinstance(source, str):
        return ""
    m = re.search(r"\[RESULT\](.*?)\[/RESULT\]", source, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    cleaned = source.strip()
    if cleaned.upper().startswith("[RESULT]"):
        return cleaned[len("[RESULT]") :].strip()
    return ""


def _derive_root_goal(description: str, instruction: str, user_input: str) -> str:
    """Build a request-specific root goal string for depth=1 task events."""
    if isinstance(description, str) and description.strip():
        return description.strip()
    if isinstance(instruction, str) and instruction.strip():
        return instruction.strip()
    if isinstance(user_input, str) and user_input.strip():
        one_line = " ".join(user_input.strip().split())
        if len(one_line) > 200:
            return f"{one_line[:200]}..."
        return one_line
    return "요청된 작업을 수행합니다."


def _build_execution_evidence(
    *,
    raw_text: str,
    wrapped_result: dict[str, Any],
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    structured_domain_outputs_by_job: dict[str, Any] | None = None,
    structured_skill_handoff_by_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build internal evidence layer separated from user-facing final output."""
    subagents: list[dict[str, Any]] = []
    for ctx in subagent_contexts:
        job_id = str(ctx.get("job_id") or "")
        used_tools = sorted(set(subagent_tool_log.get(job_id, [])))
        used_skills = sorted(set(subagent_skill_log.get(job_id, [])))
        subagents.append(
            {
                "name": ctx.get("name"),
                "role": ctx.get("role"),
                "job_id": job_id,
                "tools_used": used_tools,
                "skills_used": used_skills,
            }
        )

    parsed = wrapped_result.get("json")
    extracted_fields: dict[str, Any] = {}
    if isinstance(parsed, dict):
        for key in ("source", "endpoint_or_series", "as_of", "value", "unit"):
            if key in parsed:
                extracted_fields[key] = parsed.get(key)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "subagent_runs": subagents,
        "model_output_cleaned": wrapped_result.get("cleaned") or raw_text or "",
        "extracted_fields": extracted_fields,
        "structured_domain_outputs_by_job": structured_domain_outputs_by_job or {},
        "structured_skill_handoff_by_job": structured_skill_handoff_by_job or {},
    }


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
    crew_type: str = "action",
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
                    job_id = _resolve_job_id()
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
                            if (
                                not would_defer
                                and context_tool_tracker is not None
                                and _missing_context_for_hitl(context_tool_tracker)
                            ):
                                logger.info(
                                    "Deferring immediate HITL until tenant context tools are attempted | tool=%s job_id=%s missing=%s",
                                    getattr(base_tool, "name", base_tool.__class__.__name__),
                                    job_id,
                                    sorted(_missing_context_for_hitl(context_tool_tracker)),
                                )
                                return _tool_node_safe_output(result, effective_input, base_tool)
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
                    job_id = _resolve_job_id()
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
                            if (
                                not would_defer
                                and context_tool_tracker is not None
                                and _missing_context_for_hitl(context_tool_tracker)
                            ):
                                logger.info(
                                    "Deferring immediate HITL until tenant context tools are attempted | tool=%s job_id=%s missing=%s",
                                    getattr(base_tool, "name", base_tool.__class__.__name__),
                                    job_id,
                                    sorted(_missing_context_for_hitl(context_tool_tracker)),
                                )
                                return _tool_node_safe_output(result, effective_input, base_tool)
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
        # SDK prepare_context() does not pass these in extras; restore from todolist.draft + events.
        draft_raw = row.get("draft")
        draft = draft_raw if isinstance(draft_raw, dict) else {}

        run_state: dict[str, Any] = {}
        if isinstance(extras.get("run_state"), dict):
            run_state = dict(extras["run_state"])
        elif isinstance(draft.get("run_state"), dict):
            run_state = dict(draft["run_state"])

        human_response_answer = extras.get("human_response_answer")
        if (
            (not isinstance(human_response_answer, str) or not human_response_answer.strip())
            and task_id
            and run_state
        ):
            try:
                hr = get_latest_human_response(str(task_id))
            except Exception:
                logger.exception("get_latest_human_response failed for todo_id=%s", task_id)
                hr = None
            if isinstance(hr, str) and hr.strip():
                human_response_answer = hr.strip()
                logger.info(
                    "HITL resume: loaded human_response from DB (todo_id=%s, len=%d)",
                    task_id,
                    len(human_response_answer),
                )

        hitl_resume: dict[str, str] | None = None

        # Create or reuse execution context for this run.
        initial_agent_path = ["작업 실행 기획자"]
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
            hitl_resume = {
                "field": str(pending_field),
                "answer": human_response_answer.strip(),
            }

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

        invoke_user_text = (query or "").strip()
        if hitl_resume and hitl_resume.get("answer"):
            invoke_user_text = (
                f"{invoke_user_text}\n\n"
                f"[HITL follow-up — field `{hitl_resume.get('field', 'human_response')}`]:\n"
                f"{hitl_resume['answer']}\n\n"
                "Use this to continue the task immediately; do not ask for the same information again."
            )

        # Initialize processgpt_agent_utils context (for tools like mem0, memento, dmn_rule).
        set_context(
            task_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="action",
            users_email=extras.get("notify_user_emails", []),
        )

        domain_agent_rows = extras.get("agents", []) or []
        all_tools: list[Any] = []
        # SafeToolLoader local tools(mem0/dmn_rule)는 user_id가 필요하다.
        # 이 시스템에서는 에이전트의 id가 곧 user_id이므로 agents[].id를 최우선 사용한다.
        first_agent_user_id = ""
        if isinstance(domain_agent_rows, list):
            for ag in domain_agent_rows:
                if isinstance(ag, dict):
                    aid = str(ag.get("id") or "").strip()
                    if aid:
                        first_agent_user_id = aid
                        break
        raw_user_id = first_agent_user_id or extras.get("user_id") or extras.get("request_user_id") or row.get("user_id") or ""
        # todolist.user_id can be CSV in some flows; mem0/dmn_rule expect a single user id.
        tool_user_id = str(raw_user_id).split(",")[0].strip() if raw_user_id else ""
        mcp_cfg_raw = extras.get("tenant_mcp")
        mcp_cfg: dict[str, Any] = _mcp_config_without_blocked_servers(mcp_cfg_raw)
        mcp_servers = mcp_cfg.get("mcpServers") or {}
        # 로드 대상 MCP 서버(지식/규칙/기억 + 스킬 검색)
        desired_mcp_servers = ("claude-skills", "mem0", "dmn-rule", "memento")
        resolved_server_keys: dict[str, list[str]] = {name: [] for name in desired_mcp_servers}
        if isinstance(mcp_servers, dict):
            for key in mcp_servers:
                raw = str(key).strip()
                normalized = raw.lower().replace("_", "-")
                # tenant 설정 키가 "user-mem0", "user_dmn_rule", "org-memento" 등으로 들어와도 인식.
                for canonical in desired_mcp_servers:
                    if normalized == canonical or normalized.endswith(canonical):
                        resolved_server_keys[canonical].append(raw)

            # SafeToolLoader 는 _get_mcp_config 에서 dict key exact match를 사용하므로,
            # alias 키만 있는 경우에도 표준 키로 조회되도록 mcp_config에 canonical alias를 주입한다.
            for canonical, found_keys in resolved_server_keys.items():
                if found_keys and canonical not in mcp_servers:
                    primary_key = found_keys[0]
                    cfg = mcp_servers.get(primary_key)
                    if isinstance(cfg, dict):
                        mcp_servers[canonical] = dict(cfg)

        # 발견된 실제 키 + canonical 키를 모두 요청해 누락 가능성을 줄인다.
        mcp_request: list[str] = []
        for canonical in desired_mcp_servers:
            for candidate in [*resolved_server_keys.get(canonical, []), canonical]:
                if candidate and candidate not in mcp_request:
                    mcp_request.append(candidate)
        logger.info(
            "테넌트 연결 도구 로드 준비 | task_id=%s tenant_id=%s mcp_servers=%s request=%s",
            task_id,
            tenant_id or "(empty)",
            list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
            mcp_request,
        )
        try:
            # tenant_mcp 병합 설정 + claude-skills 명시 요청으로 Tier 2 MCP 도구 로드.
            # TaggingSafeToolLoader 가 각 MCP 툴에 _mcp_server 를 붙여 서브 에이전트 baseline 라우팅을 가능하게 함.
            tool_loader = TaggingSafeToolLoader(
                tenant_id=tenant_id or None,
                user_id=tool_user_id or None,
                agent_name="작업 실행 기획자",
                mcp_config=mcp_cfg,
            )
            all_tools = tool_loader.create_tools_from_names(tool_names=mcp_request, agent_type=None)
            # CrewAI/MCP 툴을 LangChain BaseTool로 래핑하여 tool() 데코레이터 __name__ 오류 방지
            all_tools = _ensure_langchain_tools(all_tools)
            _kn = []
            _other = []
            for _t in all_tools:
                _nm = getattr(_t, "name", type(_t).__name__)
                (_kn if _classify_tenant_context_tool(_t) else _other).append(_nm)
            logger.info(
                "연결 도구 로드 완료 | task_id=%s total_tools=%d "
                "tenant_knowledge_tools=%s (개인 기억·RAG·비즈니스 규칙) other_tools=%s (스킬·기타 MCP 등)",
                task_id,
                len(all_tools),
                _kn,
                _other,
            )
        except Exception as e:
            logger.warning(
                "MCP tools loading via TaggingSafeToolLoader failed (subagent config continues with reduced MCP tools): %s",
                e,
                exc_info=False,
            )
            logger.info("연결 도구 로드 실패 후 계속 진행 | task_id=%s total_tools=%d", task_id, len(all_tools))

        context_tool_tracker: dict[str, Any] = {
            "required_global": _required_context_categories_from_tools(all_tools),
            "attempted_global": set(),
        }
        if context_tool_tracker["required_global"]:
            logger.info(
                "HITL 선행 조건: 다음 테넌트 지식 도구 카테고리는 최소 1회 호출 후 human 개입 허용 | task_id=%s required=%s",
                task_id,
                sorted(context_tool_tracker["required_global"]),
            )

        # 서브 에이전트별 도구 호출은 각 서브 에이전트 전용 job_id로 분리된다.
        subagent_tool_log: dict[str, list[str]] = {}
        subagent_skill_log: dict[str, list[str]] = {}
        # Structured skill discovery signals captured from tool results (no text parsing).
        structured_skill_handoff_by_job: dict[str, dict[str, Any]] = {}
        # Structured domain execution outputs captured from tool results (e.g. sandbox stdout JSON).
        # Used to backfill output forms when the orchestrator forgets to emit JSON.
        structured_domain_outputs_by_job: dict[str, dict[str, Any]] = {}

        allowed_skill_names: list[str] = []
        allowed_skill_names_by_agent: dict[str, list[str]] = {}
        seen_skill_names: set[str] = set()
        for ag in domain_agent_rows:
            if isinstance(ag, dict):
                agent_name = (
                    ag.get("alias")
                    or ag.get("username")
                    or ag.get("name")
                    or str(ag.get("id") or "")
                )
            else:
                agent_name = ""
            per_agent = []
            for sk in parse_skills_csv(ag.get("skills") if isinstance(ag, dict) else None):
                n = sk.strip()
                if n and n not in seen_skill_names:
                    seen_skill_names.add(n)
                    allowed_skill_names.append(n)
                if n:
                    per_agent.append(n)
            if agent_name and per_agent:
                # Keep order stable and unique.
                allowed_skill_names_by_agent[agent_name] = list(dict.fromkeys(per_agent))
        logger.info(
            "서브에이전트 구성 시작 | task_id=%s domain_agent_rows=%d allowed_skill_names=%d",
            task_id,
            len(domain_agent_rows),
            len(allowed_skill_names),
        )

        hinted_skill_slugs = hinted_skill_slugs_from_query(
            invoke_user_text,
            allowed_skill_names=allowed_skill_names,
        )
        find_helpful_allowlist = list(
            dict.fromkeys([*allowed_skill_names, *hinted_skill_slugs])
        )
        logger.info(
            "스킬 탐색 힌트 | task_id=%s hinted_slugs=%s find_helpful_allowlist_len=%d",
            task_id,
            hinted_skill_slugs,
            len(find_helpful_allowlist),
        )

        subagents_domain = build_subagent_configs(
            domain_agent_rows,
            all_tools=all_tools,
            query=invoke_user_text,
            tenant_id=tenant_id or None,
        )
        skill_discovery_sa = build_skill_discovery_subagent(
            all_tools=all_tools,
            query=invoke_user_text,
            tenant_id=tenant_id or None,
            allowed_skill_names=allowed_skill_names,
            allowed_skill_names_by_agent=allowed_skill_names_by_agent,
            hinted_skill_slugs=hinted_skill_slugs,
        )
        subagents = [skill_discovery_sa] + subagents_domain
        agent_rows = [synthetic_skill_discovery_agent_row()] + list(domain_agent_rows)
        logger.info(
            "서브에이전트 구성 결과 | task_id=%s built_subagents=%d (includes skill discovery)",
            task_id,
            len(subagents),
        )
        for idx, sa in enumerate(subagents):
            sa_name = sa.get("name")
            sa_tools = sa.get("tools") or []
            logger.info(
                "서브에이전트[%d] name=%s tools=%d tool_names=%s",
                idx,
                sa_name,
                len(sa_tools),
                [getattr(t, "name", type(t).__name__) for t in sa_tools],
            )
        if not domain_agent_rows or not subagents_domain:
            logger.error(
                "Subagents are required: domain_agent_rows=%d subagents_domain=%d task_id=%s",
                len(domain_agent_rows),
                len(subagents_domain),
                task_id,
            )
            raise MissingSubagentsConfigurationError()

        # 동일 런에서 스킬 탐색(0번) job_id를 고정해, 탐색 단계 관측을 실행 단계 가드와 공유한다.
        subagent_job_ids = [str(uuid.uuid4()) for _ in agent_rows]
        discovery_job_id = subagent_job_ids[0] if subagent_job_ids else ""

        def _discovery_obs_read_skill_docs_ok(handoff: dict[str, Any] | None) -> bool:
            """스킬 탐색 도우미 job의 tool_result_hook 기록으로 문서 조회가 있었는지."""
            ent = structured_skill_handoff_by_job.get(discovery_job_id) or {}
            obs = ent.get("_observations") if isinstance(ent.get("_observations"), dict) else {}
            rsd = obs.get("read_skill_document")
            if not isinstance(rsd, dict) or not rsd:
                return False
            primary = ""
            if isinstance(handoff, dict) and handoff:
                primary = str(handoff.get("selected_primary") or "").strip()
                if not primary:
                    ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
                    if ranked and isinstance(ranked[0], dict):
                        primary = str(ranked[0].get("skill_name") or "").strip()
            if primary:
                docs = rsd.get(primary)
                return isinstance(docs, list) and len(docs) > 0
            for _sk, docs in rsd.items():
                if isinstance(docs, list) and len(docs) > 0:
                    return True
            return False

        def _discovery_obs_tier1_probe_ok() -> bool:
            ent = structured_skill_handoff_by_job.get(discovery_job_id) or {}
            obs = ent.get("_observations") if isinstance(ent.get("_observations"), dict) else {}
            return bool(obs.get("skill_volume_probe"))

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

            sub_job_id = subagent_job_ids[idx]
            # 서브에이전트 도구 호출은 이 에이전트 이름으로 로깅되도록 전용 ExecutionContext 사용.
            # (동일 exec_ctx를 쓰면 서브가 호출한 도구가 모두 작업 실행 기획자로 기록됨)
            _role = (agent_role or "").strip() or None
            sub_exec_ctx = ExecutionContext(
                correlation_id=exec_ctx.correlation_id,
                agent_path=list(exec_ctx.agent_path) + [agent_name],
                task_id=exec_ctx.task_id,
                proc_inst_id=exec_ctx.proc_inst_id,
                agent_role=_role,
            )
            if idx < len(subagents):
                sub_tools = subagents[idx].get("tools", [])
                tool_input_augmenter = None
                if agent_name == SKILL_DISCOVERY_SUBAGENT_NAME:
                    # Captured per-run allowlist (crew skills + query slugs) for find_helpful_skills.
                    _find_allowed = list(find_helpful_allowlist)

                    skill_discovery_tier_state: dict[str, bool] = {"tier2_attempted": False}
                    skill_discovery_has_tier2 = any(
                        "claude-skills" in str(getattr(st, "_mcp_server", "") or "").lower()
                        for st in sub_tools
                    )

                    def _skill_discovery_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        if tool_name not in ("find_helpful_skills", "read_skill_document"):
                            return tool_input
                        merged = _coerce_tool_input_dict(tool_input)
                        if merged is None:
                            return tool_input
                        if tool_name == "find_helpful_skills":
                            # LangChain tool calls are usually shaped like:
                            # {"name": "...", "args": {...}, "id": "...", "type": "tool_call"}
                            # Ensure allowlist + tenant_id are passed inside "args" so MCP tools receive them.
                            args = merged.get("args")
                            if not isinstance(args, dict):
                                args = {}
                                merged["args"] = args

                            # Back-compat: if previous code injected at top-level, migrate it into args.
                            top_allowed = merged.get("allowed_skill_names")
                            if isinstance(top_allowed, list) and "allowed_skill_names" not in args:
                                args["allowed_skill_names"] = list(top_allowed)
                                merged.pop("allowed_skill_names", None)
                            top_tenant = merged.get("tenant_id")
                            if isinstance(top_tenant, str) and top_tenant.strip() and "tenant_id" not in args:
                                args["tenant_id"] = top_tenant
                                merged.pop("tenant_id", None)

                            if _find_allowed and "allowed_skill_names" not in args:
                                args["allowed_skill_names"] = list(_find_allowed)
                            if tenant_id and "tenant_id" not in args:
                                args["tenant_id"] = str(tenant_id)
                            return merged
                        # read_skill_document: forcing SKILL.md often fails for tenant skills.
                        # Convert to listing mode first so the subagent can choose an actual file.
                        doc_path = str(merged.get("document_path") or "").strip()
                        if doc_path in ("SKILL.md", "/SKILL.md"):
                            merged["document_path"] = None
                        return merged

                    def _skill_discovery_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        server = str(getattr(base_tool, "_mcp_server", "") or "").lower()
                        if "claude-skills" in server or tool_name in (
                            "list_skills",
                            "read_skill_document",
                            "find_helpful_skills",
                        ):
                            skill_discovery_tier_state["tier2_attempted"] = True
                            return None
                        # Tier 1 (filesystem via mounted skill volume) is allowed before Tier 2:
                        # sandbox_create + sandbox_copy_mounted_skill + sandbox_read_workspace_files are
                        # considered "materialization / inspection", not arbitrary execution.
                        if tool_name in (
                            "sandbox_create",
                            "sandbox_copy_mounted_skill",
                            "sandbox_read_workspace_files",
                            "sandbox_read_artifacts",
                        ):
                            return None
                        if tool_name.startswith("sandbox_") and skill_discovery_has_tier2:
                            if not skill_discovery_tier_state.get("tier2_attempted"):
                                return {
                                    "error": "tier2_required_before_tier3",
                                    "message": "Run claude-skills MCP discovery tools before sandbox execution.",
                                    "requires_human": False,
                                }
                        return None
                    tool_input_augmenter = _skill_discovery_input_augmenter
                    tool_call_guard = _skill_discovery_tool_call_guard
                    def _skill_discovery_tool_result_hook(
                        *,
                        base_tool: Any,
                        job_id: str,
                        tool_input: Any,
                        tool_output: Any,
                        error: Exception | None,
                    ) -> None:
                        if error is not None:
                            return
                        tname = str(getattr(base_tool, "name", "") or "")
                        raw_in = tool_input if isinstance(tool_input, dict) else _coerce_tool_input_dict(tool_input)
                        raw_in = raw_in if isinstance(raw_in, dict) else {}
                        text_out = _extract_plain_text(tool_output)
                        entry = structured_skill_handoff_by_job.setdefault(
                            job_id,
                            {
                                "version": 1,
                                "tenant_id": tenant_id or "",
                                "allowed_skill_names": list(find_helpful_allowlist),
                                "selected_primary": (find_helpful_allowlist[0] if find_helpful_allowlist else ""),
                                "ranked": [],
                                "_observations": {},
                                "required_env": {},
                            },
                        )
                        obs = entry.setdefault("_observations", {})
                        if tname == "skill_volume_probe":
                            obs["skill_volume_probe"] = text_out
                        elif tname == "find_helpful_skills":
                            obs["find_helpful_skills"] = text_out
                        elif tname == "read_skill_document":
                            sk = ""
                            args = raw_in.get("args") if isinstance(raw_in.get("args"), dict) else raw_in
                            if isinstance(args, dict):
                                sk = str(args.get("skill_name") or "").strip()
                                docp = args.get("document_path")
                            else:
                                docp = None
                            if sk:
                                docs = obs.setdefault("read_skill_document", {}).setdefault(sk, [])
                                docs.append({"document_path": docp, "text": text_out[:20000]})
                                # Opportunistically extract env var names from the doc text.
                                try:
                                    env_names = set()
                                    # Common patterns: $VAR, ${VAR}, os.environ["VAR"], export VAR=...
                                    for m in re.finditer(r"(?:\\$\\{?|os\\.environ\\[\"|os\\.environ\\['|export\\s+)([A-Z][A-Z0-9_]{2,})", text_out):
                                        env_names.add(m.group(1))
                                    if env_names:
                                        req = entry.setdefault("required_env", {})
                                        for nm in sorted(env_names):
                                            req.setdefault(nm, {"type": "text", "source": "doc"})
                                except Exception:
                                    pass
                        # Keep a minimal ranked list even if the model forgets to print handoff JSON.
                        if not entry.get("ranked"):
                            primary = str(entry.get("selected_primary") or "").strip()
                            if primary:
                                entry["ranked"] = [
                                    {
                                        "skill_name": primary,
                                        "evidence": {"source": "mcp", "notes": "Captured from tool results"},
                                        "documents": [],
                                        "endpoints": [],
                                        "sandbox_materialization": {"required": True, "suggested_calls": []},
                                    }
                                ]
                else:
                    # Domain/DB subagents: prefer centralized skill discovery, but allow fallback discovery
                    # only after Tier 1 probe is attempted (volume/FS) to avoid redundant MCP calls.
                    domain_tier_state: dict[str, bool] = {
                        "tier1_attempted": False,
                        "mounted_skill_copied": False,
                        "skill_docs_read": False,
                    }

                    def _effective_skill_docs_read() -> bool:
                        """도메인이 직접 읽었거나, 스킬 탐색 도우미가 같은 런에서 이미 기록한 경우."""
                        if domain_tier_state.get("skill_docs_read"):
                            return True
                        ho: dict[str, Any] | None = None
                        try:
                            st = subagent_event_state
                            if isinstance(st, dict):
                                ho = st.get("skill_handoff")
                        except Exception:
                            ho = None
                        if _discovery_obs_read_skill_docs_ok(ho if isinstance(ho, dict) else None):
                            domain_tier_state["skill_docs_read"] = True
                            return True
                        ent_full = structured_skill_handoff_by_job.get(discovery_job_id)
                        if _discovery_obs_read_skill_docs_ok(
                            ent_full if isinstance(ent_full, dict) else None
                        ):
                            domain_tier_state["skill_docs_read"] = True
                            return True
                        return False

                    def _effective_tier1_attempted() -> bool:
                        if domain_tier_state.get("tier1_attempted"):
                            return True
                        if _discovery_obs_tier1_probe_ok():
                            domain_tier_state["tier1_attempted"] = True
                            return True
                        return False

                    def _domain_tool_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                        """Fix common confusion: passing FS absolute paths to MCP doc readers."""
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        if tool_name != "read_skill_document":
                            return tool_input
                        merged = _coerce_tool_input_dict(tool_input)
                        if merged is None:
                            return tool_input
                        args = merged.get("args") if isinstance(merged.get("args"), dict) else merged
                        if not isinstance(args, dict):
                            return merged
                        dp = str(args.get("document_path") or "").strip()
                        if dp.startswith("/") and ("/app/skills" in dp or "/skills/" in dp):
                            # Force listing mode; the agent must pick a valid MCP document path.
                            args["document_path"] = None
                        return merged

                    def _domain_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        server = str(getattr(base_tool, "_mcp_server", "") or "").lower()

                        # Never allow domain agents to mutate the shared skill catalog.
                        if tool_name in ("delete_skill", "delete_skill_file", "update_skill_file"):
                            return {
                                "error": "skill_catalog_mutation_forbidden",
                                "message": (
                                    "도메인 서브에이전트는 스킬 카탈로그를 수정/삭제할 수 없습니다. "
                                    "필요하면 `스킬 탐색 도우미` 또는 오케스트레이터 레벨에서 처리하세요."
                                ),
                                "requires_human": False,
                            }

                        # Track Tier 1 attempt for deterministic fallback gating.
                        if tool_name == "skill_volume_probe":
                            domain_tier_state["tier1_attempted"] = True
                            return None

                        # Domain must consult skill docs (from handoff) before any execution/materialization.
                        # This enforces "read docs → materialize → run", instead of inventing endpoints/scripts.
                        if tool_name == "read_skill_document":
                            domain_tier_state["skill_docs_read"] = True
                            return None

                        # Prevent domain subagents from re-running skill discovery search.
                        # Skill discovery is centralized in `스킬 탐색 도우미`; domain should only read docs.
                        if tool_name == "find_helpful_skills":
                            return {
                                "error": "domain_skill_search_forbidden",
                                "message": (
                                    "도메인 서브에이전트는 `find_helpful_skills`로 스킬 탐색을 수행할 수 없습니다. "
                                    "`스킬 탐색 도우미`의 핸드오프를 사용하고, 필요한 경우 `read_skill_document`로 "
                                    "해당 스킬 문서를 확인한 뒤 실행 단계로 진행하세요."
                                ),
                                "requires_human": False,
                            }

                        # Require skill materialization before running arbitrary sandbox code.
                        # This prevents "ignore skill discovery, generate random API script" failures.
                        if tool_name == "sandbox_copy_mounted_skill":
                            if not _effective_skill_docs_read():
                                return {
                                    "error": "skill_docs_required_before_materialization",
                                    "message": (
                                        "스킬을 샌드박스에 복사/물리화하기 전에 스킬 문서(엔드포인트/사용법) 확인이 필요합니다. "
                                        "`스킬 탐색 도우미`가 이미 `read_skill_document`로 기록했다면 그대로 진행 가능합니다. "
                                        "그렇지 않으면 `read_skill_document(skill_name=...)`로 확인하세요."
                                    ),
                                    "requires_human": False,
                                }
                            domain_tier_state["mounted_skill_copied"] = True
                            return None
                        if tool_name in ("sandbox_write_and_run", "sandbox_run_shell") or tool_name.startswith(
                            "sandbox_write_"
                        ):
                            if not _effective_skill_docs_read():
                                return {
                                    "error": "skill_docs_required_before_sandbox",
                                    "message": (
                                        "샌드박스 실행/작성 전에 스킬 문서 확인이 필요합니다. "
                                        "`스킬 탐색 도우미`가 이미 문서를 읽고 기록했다면 그대로 진행하고, "
                                        "아니면 `read_skill_document` 후 `sandbox_copy_mounted_skill` 순으로 진행하세요."
                                    ),
                                    "requires_human": False,
                                }
                            # For FS-mount skills, require sandbox_copy_mounted_skill first.
                            # For MCP-document skills, allow sandbox_write_* materialization after docs are read.
                            if not domain_tier_state.get("mounted_skill_copied"):
                                # Best-effort: infer materialization route from injected skill handoff.
                                handoff = None
                                try:
                                    handoff = (
                                        subagent_event_state.get("skill_handoff")
                                        if isinstance(subagent_event_state, dict)
                                        else None
                                    )
                                except Exception:
                                    handoff = None
                                # If skill discovery produced per-domain handoffs, prefer this agent's slice.
                                if isinstance(handoff, dict):
                                    try:
                                        by_agent = handoff.get("by_agent")
                                        if isinstance(by_agent, dict):
                                            per = by_agent.get(agent_name)
                                            if isinstance(per, dict) and per:
                                                handoff = per
                                    except Exception:
                                        pass
                                primary_source = ""
                                if isinstance(handoff, dict):
                                    ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
                                    if ranked and isinstance(ranked[0], dict):
                                        ev = ranked[0].get("evidence") if isinstance(ranked[0].get("evidence"), dict) else {}
                                        primary_source = str(ev.get("source") or "").strip().lower()
                                if primary_source != "mcp":
                                    return {
                                        "error": "skill_materialization_required_before_sandbox",
                                        "message": (
                                            "샌드박스에서 임의 코드 실행 전에 스킬을 먼저 물리화해야 합니다. "
                                            "파일형/FS 마운트형이면 `sandbox_copy_mounted_skill(workspace_id, tenant_id, skill_name, dest_subdir=...)`로 "
                                            "스킬 파일을 복사한 뒤 진행하세요. "
                                            "문서형/MCP형이면 `read_skill_document`로 문서를 읽고 "
                                            "`sandbox_write_files`로 `skill_docs/<skill_name>/...`에 저장해 물리화한 뒤 진행하세요."
                                        ),
                                        "requires_human": False,
                                    }

                        # If the structured handoff indicates required env vars, require env_json to be supplied
                        # for sandbox execution calls (so config/credentials are injected deterministically).
                        if tool_name in ("sandbox_write_and_run", "sandbox_write_files_and_run"):
                            try:
                                handoff = subagent_event_state.get("skill_handoff") if isinstance(subagent_event_state, dict) else None
                            except Exception:
                                handoff = None
                            # If skill discovery produced per-domain handoffs, prefer this agent's slice.
                            if isinstance(handoff, dict):
                                try:
                                    by_agent = handoff.get("by_agent")
                                    if isinstance(by_agent, dict):
                                        per = by_agent.get(agent_name)
                                        if isinstance(per, dict) and per:
                                            handoff = per
                                except Exception:
                                    pass
                            req_env = {}
                            if isinstance(handoff, dict):
                                req_env = handoff.get("required_env") if isinstance(handoff.get("required_env"), dict) else {}
                            if req_env:
                                raw = tool_input if isinstance(tool_input, dict) else _coerce_tool_input_dict(tool_input)
                                raw = raw if isinstance(raw, dict) else {}
                                args = raw.get("args") if isinstance(raw.get("args"), dict) else raw
                                env_json = ""
                                if isinstance(args, dict):
                                    env_json = str(args.get("env_json") or "").strip()
                                if not env_json:
                                    return {
                                        "error": "env_injection_required",
                                        "message": (
                                            "스킬 문서에서 환경변수 요구사항이 감지되었습니다(required_env). "
                                            "샌드박스 실행 시 `env_json`으로 환경변수를 텍스트 형태로 주입해야 합니다. "
                                            "또한 필요한 경우 `sandbox_write_files_and_run`로 컨텍스트 원재료를 파일로 생성한 뒤 실행하세요."
                                        ),
                                        "requires_human": False,
                                    }

                        # Tier 2 (claude-skills MCP) is allowed only as fallback, after Tier 1 was attempted.
                        is_skill_mcp = ("claude-skills" in server) or tool_name in (
                            "list_skills",
                            "read_skill_document",
                            "find_helpful_skills",
                        )
                        # Allow domain to read skill docs anytime (handoff-driven), even without Tier1 probe.
                        if tool_name == "read_skill_document":
                            return None
                        if is_skill_mcp and not _effective_tier1_attempted():
                            return {
                                "error": "tier1_required_before_fallback_skill_discovery",
                                "message": (
                                    "스킬 탐색은 기본적으로 `스킬 탐색 도우미`가 전담합니다. "
                                    "폴백으로 직접 MCP 탐색이 필요하면, 먼저 Tier1(볼륨) 확인을 위해 "
                                    "`skill_volume_probe`를 호출하세요(스킬 탐색 도우미가 이미 호출했다면 생략 가능). "
                                    "이후 `list_skills`/`find_helpful_skills`/`read_skill_document`를 진행할 수 있습니다."
                                ),
                                "requires_human": False,
                            }

                        return None

                    tool_call_guard = _domain_tool_call_guard
                    tool_input_augmenter = _domain_tool_input_augmenter

                    def _domain_tool_result_hook(
                        *,
                        base_tool: Any,
                        job_id: str,
                        tool_input: Any,
                        tool_output: Any,
                        error: Exception | None,
                    ) -> None:
                        """Capture structured stdout from domain executions for deterministic form backfill."""
                        if error is not None:
                            return
                        tname = str(getattr(base_tool, "name", "") or "")
                        if tname not in (
                            "sandbox_run_shell",
                            "sandbox_write_and_run",
                            "sandbox_write_files_and_run",
                        ):
                            return
                        if not isinstance(tool_output, dict):
                            return
                        stdout = tool_output.get("stdout") if isinstance(tool_output.get("stdout"), str) else ""
                        stderr = tool_output.get("stderr") if isinstance(tool_output.get("stderr"), str) else ""
                        if not stdout.strip():
                            return
                        try:
                            parsed = json.loads(stdout.strip())
                        except Exception:
                            return
                        if not isinstance(parsed, dict):
                            return
                        bucket = structured_domain_outputs_by_job.setdefault(job_id, {})
                        items = bucket.setdefault(tname, [])
                        if isinstance(items, list):
                            items.append(
                                {
                                    "command": tool_output.get("command"),
                                    "exit_code": tool_output.get("exit_code"),
                                    "parsed_stdout": parsed,
                                    "stdout_head": stdout.strip()[:8000],
                                    "stderr_head": stderr.strip()[:8000],
                                }
                            )
                _instrument_tools(
                    sub_tools,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    crew_type="action",
                    job_id_getter=lambda sj=sub_job_id: sj,
                    job_tool_log=subagent_tool_log,
                    job_skill_log=subagent_skill_log,
                    tool_result_hook=(
                        _skill_discovery_tool_result_hook
                        if agent_name == SKILL_DISCOVERY_SUBAGENT_NAME
                        else _domain_tool_result_hook
                    ),
                    exec_ctx=sub_exec_ctx,
                    event_queue=event_queue,
                    tool_input_augmenter=tool_input_augmenter,
                    tool_call_guard=tool_call_guard,
                    context_tool_tracker=context_tool_tracker,
                )
                logger.info(
                    "서브에이전트 도구 인스트루먼트 완료 | task_id=%s agent=%s job_id=%s tools=%d",
                    task_id,
                    agent_name,
                    sub_job_id,
                    len(sub_tools),
                )

            subagent_contexts.append(
                {
                    "job_id": sub_job_id,
                    "role": agent_role,
                    "name": agent_name,
                    "goal": agent_goal,
                    "agent_profile": agent_profile,
                    "agent_id": agent_id,
                    "exec_ctx": sub_exec_ctx,
                }
            )

        instrumented_hitl_tools = list(get_hitl_tools())
        _instrument_tools(
            instrumented_hitl_tools,
            todo_id=task_id or None,
            proc_inst_id=proc_inst_id or None,
            crew_type="action",
            job_id_getter=lambda: job_uuid,
            job_tool_log=subagent_tool_log,
            job_skill_log=subagent_skill_log,
            exec_ctx=exec_ctx,
            event_queue=event_queue,
            tool_input_augmenter=None,
            tool_call_guard=_make_request_human_input_context_guard(context_tool_tracker),
            context_tool_tracker=context_tool_tracker,
        )

        routing_cfg = resolve_model_routing(extras, default_model="openai:gpt-4o")
        model_name = routing_cfg.model_name

        # 단일 DeepAgent 런타임(Planning + Delegation + Execution)
        base_rules = default_orchestrator_prompt()
        feedback_summary = extras.get("summarized_feedback") or ""
        form_fields = extras.get("form_fields")
        form_html = extras.get("form_html")
        form_info = None
        if form_fields or form_html:
            form_info = {"fields": form_fields, "html": form_html}

        # 사용자가 한글로 입력한 경우 plans/result를 한글로 작성하도록 지시
        user_text_for_lang = (
            invoke_user_text.strip()
            or f"{description or ''} {instruction or ''}".strip()
        )
        output_language = "korean" if _is_korean_user_input(user_text_for_lang) else None

        system_prompt = build_orchestrator_system_prompt(
            base_rules=base_rules,
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
            plan_text=None,
            output_language=output_language,
            hitl_resume=hitl_resume,
        )

        # NOTE: 루트(DeepAgent) task_started 는 서브에이전트 이벤트보다 먼저 발행되어야 한다.
        # (DB 인입 시점 기준으로 서브가 먼저 시작한 것처럼 보이는 문제 방지)
        _enqueue_status(
            event_queue,
            "task_started",
            {
                "message": "단일 DeepAgent 실행을 시작했습니다.",
                "goal": "에이전트가 목표를 수립하고 작업 계획을 작성하는 중입니다.",
                "routing_mode": routing_cfg.route_mode,
                "model": model_name,
            },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="planning",
            agent_name="작업 실행 기획자",
        )

        # task() 실행 구간에서 skill 볼륨 FS 접근의 job_id/exec_ctx 를 서브 에이전트에 맞춘다.
        # 서브에이전트 started/completed 는 task() 호출 시점 미들웨어에서 발행하고,
        # 미들웨어가 동작하지 않은 런타임만 후단 fallback 이벤트를 발행한다.
        planning_completed_emitted = False

        async def _emit_planning_completed_fallback(message: str) -> None:
            """플랜 본문을 포착하지 못한 경우에도 planning 완료 이벤트를 보장한다."""
            nonlocal planning_completed_emitted
            if planning_completed_emitted:
                return
            _enqueue_status(
                event_queue,
                "task_completed",
                {"message": message},
                proc_inst_id,
                task_id,
                "action",
                job_id=job_uuid,
                exec_ctx=exec_ctx,
                logical_event_type="agent_finished",
                phase="planning",
                agent_name="작업 실행 기획자",
            )
            planning_completed_emitted = True

        async def _before_subagent_start() -> None:
            # 플래닝 결과가 state에서 아직 감지되지 않았더라도,
            # 첫 서브에이전트 시작 전에는 planning completed를 반드시 발행한다.
            await _emit_planning_completed_fallback("플래닝이 완료되어 실행 단계로 전환합니다.")

        name_to_context = {str(c["name"]): c for c in subagent_contexts}
        subagent_event_middleware, subagent_event_state = _create_subagent_event_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            name_to_context=name_to_context,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            structured_skill_handoff_by_job=structured_skill_handoff_by_job,
            on_before_subagent_start=_before_subagent_start,
        )
        extra_middleware: list[Any] = [SubagentJobScopeMiddleware()]
        if subagent_event_middleware is not None:
            extra_middleware.append(subagent_event_middleware)

        orchestrator = build_orchestrator(
            model_name=model_name,
            system_prompt=system_prompt,
            subagents=subagents,
            tools=[],
            hitl_tools=instrumented_hitl_tools,
            middleware=extra_middleware,
            skills=tenant_skill_sources_for_middleware(tenant_id or None),
        )
        logger.info(
            "오케스트레이터 빌드 완료 | task_id=%s model=%s subagents=%d middleware=%d",
            task_id,
            model_name,
            len(subagents),
            len(extra_middleware),
        )

        name_to_sub_job = {str(c["name"]): str(c["job_id"]) for c in subagent_contexts}
        name_to_sub_ex = {str(c["name"]): c["exec_ctx"] for c in subagent_contexts}
        skill_vol_binding = SkillVolumeToolEventBinding(
            event_queue=event_queue,
            todo_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="action",
            default_job_id=job_uuid,
            root_exec_ctx=exec_ctx,
            subagent_name_to_job_id=name_to_sub_job,
            subagent_name_to_exec_ctx=name_to_sub_ex,
        )
        tok_skill_bind = skill_volume_tool_event_binding.set(skill_vol_binding)
        tok_skill_job = skill_volume_active_job_id.set(job_uuid)
        tok_skill_ex = skill_volume_active_exec_ctx.set(exec_ctx)
        try:
            logger.info("오케스트레이터 실행 시작 | task_id=%s", task_id)
            result = None

            async def _extract_plan_message_from_state(state: Any) -> str:
                """Try best-effort extraction of [PLAN] from a LangGraph state."""
                if not isinstance(state, dict):
                    return ""
                msgs = state.get("messages", []) or []
                try:
                    for msg in reversed(msgs):
                        content = (
                            msg.get("content")
                            if isinstance(msg, dict)
                            else getattr(msg, "content", None)
                        )
                        raw_text_for_plan = _extract_plain_text(content)
                        candidate = _extract_plan_block(raw_text_for_plan) or ""
                        if candidate:
                            return candidate
                except Exception:
                    logger.exception("Failed while scanning messages for [PLAN] block", exc_info=True)
                return ""

            async def _emit_planning_completed_if_ready(state: Any) -> None:
                nonlocal planning_completed_emitted
                if planning_completed_emitted:
                    return
                if not isinstance(state, dict):
                    return

                todos_state = state.get("todos")
                plan_message = ""
                try:
                    plan_message = await _extract_plan_message_from_state(state)
                except Exception:
                    logger.exception("Failed to extract plan message from state", exc_info=True)

                if not plan_message and todos_state is not None:
                    try:
                        plan_message = _join_todo_contents(todos_state)
                    except Exception:
                        plan_message = ""

                # 플랜이 아직 확정되지 않았으면 대기 (첫 관측 시점에 즉시 발행)
                if not plan_message and todos_state is None:
                    return

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
                planning_completed_emitted = True

            # 가능한 경우 스트리밍으로 플래닝 완료 시점을 조기에 포착
            stream_input = {"messages": [{"role": "user", "content": invoke_user_text}]}
            if hasattr(orchestrator, "astream"):
                async for state in orchestrator.astream(stream_input, stream_mode="values"):
                    result = state
                    await _emit_planning_completed_if_ready(state)
            else:
                result = await orchestrator.ainvoke(stream_input)
                await _emit_planning_completed_if_ready(result)

            logger.info(
                "오케스트레이터 실행 완료 | task_id=%s result_type=%s keys=%s",
                task_id,
                type(result).__name__,
                list(result.keys()) if isinstance(result, dict) else [],
            )
            # 스트리밍 경로에서 플래닝 완료 이벤트가 아직 안 나갔다면(플랜/투두가 끝까지 없던 케이스) 보정
            if not planning_completed_emitted:
                await _emit_planning_completed_if_ready(result)
            if not planning_completed_emitted:
                await _emit_planning_completed_fallback("플래닝 단계가 종료되었습니다.")
        except HitlInterrupt as hi:
            # A tool signalled that human input is required during worker execution.
            # Record a human_asked interrupt event and return waiting_for_human so the caller can pause.
            if not planning_completed_emitted:
                await _emit_planning_completed_fallback("플래닝이 완료되어 실행 중 사용자 입력이 필요해 중단되었습니다.")
            decision = hi.decision or {}
            pending_field = _resolve_pending_field_from_decision(decision)
            question = decision.get("question") or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            choices = decision.get("choices") or []
            hitl_agent_name = "작업 실행 기획자"
            interrupt_agent_path = list(exec_ctx.agent_path)
            for ctx in subagent_contexts:
                if ctx.get("job_id") == hi.job_id:
                    hitl_agent_name = ctx.get("name") or hitl_agent_name
                    sub_ctx = ctx.get("exec_ctx")
                    if sub_ctx is not None and hasattr(sub_ctx, "agent_path"):
                        interrupt_agent_path = list(sub_ctx.agent_path)
                    break
            if _is_duplicate_hitl_request(
                run_state,
                question=question,
                pending_field=pending_field,
            ):
                logger.info(
                    "Suppressing duplicate HITL question | task_id=%s field=%s",
                    task_id,
                    pending_field,
                )
                return {
                    "status": "waiting_for_human",
                    "run_state": run_state,
                }
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
                    "pending_field": pending_field,
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": interrupt_agent_path,
                    "tool_name": hi.tool_name,
                    "last_question": question,
                    "tool_call_context": {
                        "job_id": hi.job_id,
                        "tool_name": hi.tool_name,
                    },
                },
            }
        except Exception:
            if not planning_completed_emitted:
                await _emit_planning_completed_fallback("플래닝 단계가 종료되었으나 실행 중 오류가 발생했습니다.")
            logger.exception("Worker planner failed.", exc_info=True)
            raise
        finally:
            skill_volume_active_exec_ctx.reset(tok_skill_ex)
            skill_volume_active_job_id.reset(tok_skill_job)
            skill_volume_tool_event_binding.reset(tok_skill_bind)

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
            pending_field = "human_response"
            if _is_duplicate_hitl_request(
                run_state,
                question=question,
                pending_field=pending_field,
            ):
                logger.info(
                    "Suppressing duplicate graph-level HITL question | task_id=%s",
                    task_id,
                )
                return {
                    "status": "waiting_for_human",
                    "run_state": run_state,
                }
            hitl_agent_name = (
                (exec_ctx.agent_path[-1] if exec_ctx.agent_path else None)
                or "작업 실행 기획자"
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
                    "pending_field": pending_field,
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": list(exec_ctx.agent_path),
                    "tool_name": "request_human_input",
                    "last_question": question,
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

        # 미들웨어가 실제 task()를 감싸지 못한 런타임만 fallback으로 started/completed를 발행.
        fallback_emitted = False
        if not subagent_event_state.get("saw_task_call"):
            logger.info(
                "서브에이전트 이벤트 fallback 발행 | task_id=%s subagents=%d",
                task_id,
                len(subagent_contexts),
            )
            for ctx in subagent_contexts:
                _emit_subagent_task_started(
                    event_queue=event_queue,
                    proc_inst_id=proc_inst_id,
                    task_id=task_id,
                    exec_ctx=ctx.get("exec_ctx") or exec_ctx,
                    ctx=ctx,
                )
            for ctx in subagent_contexts:
                _emit_subagent_task_completed(
                    event_queue=event_queue,
                    proc_inst_id=proc_inst_id,
                    task_id=task_id,
                    exec_ctx=ctx.get("exec_ctx") or exec_ctx,
                    ctx=ctx,
                    subagent_tool_log=subagent_tool_log,
                    subagent_skill_log=subagent_skill_log,
                )
            fallback_emitted = True

        # 중요: fallback subagent 이벤트와 final 이벤트를 같은 버퍼 배치에 넣으면
        # DB 저장 시각이 동일해 UI 정렬이 뒤섞일 수 있다.
        # 짧게 양보해 별도 배치로 flush되게 하여 서브에이전트 이벤트가 항상 먼저 보이도록 한다.
        if fallback_emitted:
            await asyncio.sleep(1.2)

        form_fields = extras.get("form_fields")
        pure_form_data, wrapped_result, original_wo_form, _, _ = convert_agent_output(
            raw_text, form_id, form_fields
        )
        if pure_form_data is None:
            pure_form_data = {}
        async def _finalize_form_with_llm(
            *,
            model_name: str,
            form_id: str,
            form_fields: Any,
            evidence: dict[str, Any],
        ) -> dict[str, Any] | None:
            """Use an LLM-only finalizer to produce the exact form JSON deliverable.

            This is intentionally NOT heuristic matching: the LLM is responsible for mapping
            evidence into the required output form keys.
            """
            field_keys = _extract_form_field_keys(form_fields)
            if not field_keys:
                return None

            # Build a strict finalizer prompt: no tools, no subagents, no markdown.
            finalizer_prompt = (
                "You are a form output finalizer.\n"
                "You MUST return ONE valid JSON object and nothing else.\n"
                "Do NOT use markdown. Do NOT use code fences. Do NOT include explanations.\n\n"
                "You are given:\n"
                f"- form_id: {form_id}\n"
                f"- required field keys: {field_keys}\n\n"
                "Task:\n"
                "Populate ALL required keys with the correct values inferred from the provided EVIDENCE.\n"
                "If you cannot infer a value with high confidence, still fill it with the best available "
                "value from evidence; do not leave fields blank.\n"
                "Return ONLY the JSON object using exactly the required keys.\n"
            )

            # LLM-only runtime: no tools, no HITL tools, no subagents.
            finalizer = build_orchestrator(
                model_name=model_name,
                system_prompt=finalizer_prompt,
                subagents=[],
                tools=[],
                hitl_tools=[],
                middleware=[],
                skills=None,
            )
            msg = (
                "EVIDENCE (JSON):\n"
                + json.dumps(evidence, ensure_ascii=False)
                + "\n\n"
                "Return ONLY the final form JSON now."
            )
            out_state = await finalizer.ainvoke({"messages": [{"role": "user", "content": msg}]})
            # Extract the last message content
            text = ""
            if isinstance(out_state, dict):
                msgs = out_state.get("messages") or []
                if msgs:
                    last = msgs[-1]
                    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
                    text = _extract_plain_text(content)
            parsed = None
            try:
                parsed = json.loads((text or "").strip())
            except Exception:
                parsed = None
            return parsed if isinstance(parsed, dict) else None

        # 폼이 없거나 defaultform인 경우, 항상 JSON 파싱 가능한 고정 포맷으로 래핑
        has_form = bool(form_fields) and form_id not in (None, "", "defaultform")
        if not has_form:
            base_text = original_wo_form or wrapped_result.get("cleaned") or ""
            # 모델이 리스트/딕셔너리 구조를 문자열로 반환한 경우에도
            # 내부 text/content 필드만 추출하여 순수 텍스트로 만든다.
            final_text = _extract_plain_text(base_text).strip()
            # 최종 사용자 출력에는 PLAN 블록을 절대 포함하지 않는다.
            final_text = _strip_plan_blocks(final_text)
            result_block = _extract_result_block(final_text)
            if result_block:
                final_text = result_block
            result_status = "SUCCESS" if final_text else "Fail"
            final_result_payload = {
                "output": {
                    "status": result_status,
                    "result": final_text,
                },
            }
        else:
            # For form outputs, the deliverable MUST be produced by an LLM, not heuristics.
            # If the orchestrator didn't emit a complete form JSON, run a tool-free finalizer LLM pass
            # using captured execution evidence (including structured subagent tool outputs).
            field_keys = _extract_form_field_keys(form_fields)
            needs_finalize = False
            if not isinstance(pure_form_data, dict):
                needs_finalize = True
                pure_form_data = {}
            else:
                missing = [k for k in field_keys if pure_form_data.get(k) in (None, "", [])]
                if field_keys and missing:
                    needs_finalize = True

            if needs_finalize:
                evidence = {
                    "form_id": form_id,
                    "form_fields": form_fields,
                    "orchestrator_last_message": raw_text,
                    "subagent_tool_outputs": structured_domain_outputs_by_job,
                    "skill_handoff": structured_skill_handoff_by_job,
                }
                finalized = await _finalize_form_with_llm(
                    model_name=model_name,
                    form_id=str(form_id),
                    form_fields=form_fields,
                    evidence=evidence,
                )
                if isinstance(finalized, dict):
                    pure_form_data = finalized

            # Soft validation only: keep empty values but record which keys are missing.
            remaining_missing = [k for k in field_keys if pure_form_data.get(k) in (None, "", [])]
            if remaining_missing:
                pure_form_data = {k: (pure_form_data.get(k) or "") for k in field_keys}
                wrapped_result.setdefault("final_output_warnings", [])
                if isinstance(wrapped_result.get("final_output_warnings"), list):
                    wrapped_result["final_output_warnings"].append(
                        {
                            "type": "missing_form_fields",
                            "missing_keys": remaining_missing,
                        }
                    )
            # UI/consumer-friendly contract:
            # - Always use `result` key for display extraction
            # - Carry `form_id` as separate metadata for identification
            final_result_payload = {
                "form_id": str(form_id or ""),
                "result": pure_form_data,
            }

        # 이벤트 테이블: result 키에는 최종 도메인 결과만 저장한다.
        # - no-form: 평문 문자열
        # - form: {"form_id": "...", "result": { 필드키: 값, ... } }
        if not has_form:
            final_event_result = final_text
        else:
            _wrapped = _build_form_event_result(
                form_id=form_id,
                form_fields=form_fields,
                pure_form_data=pure_form_data,
                fallback_text=(original_wo_form or wrapped_result.get("cleaned") or ""),
            )
            fid = str(form_id or "").strip()
            inner = _wrapped.get(fid) if (fid and isinstance(_wrapped, dict)) else _wrapped
            final_event_result = {
                "form_id": fid,
                "result": inner if isinstance(inner, dict) else {},
            }

        execution_evidence = _build_execution_evidence(
            raw_text=raw_text,
            wrapped_result=wrapped_result,
            subagent_contexts=subagent_contexts,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            structured_domain_outputs_by_job=structured_domain_outputs_by_job,
            structured_skill_handoff_by_job=structured_skill_handoff_by_job,
        )
        # 2-layer output:
        # - final_output: user-facing final deliverable
        # - execution_evidence: internal/verifiable execution details
        wrapped_result["final_output"] = final_result_payload
        wrapped_result["execution_evidence"] = execution_evidence
        has_execution_evidence = any(
            bool((run.get("tools_used") or []))
            for run in (execution_evidence.get("subagent_runs") or [])
        )
        if not has_execution_evidence:
            final_result_payload = {
                "output": {
                    "status": "Fail",
                    "result": "실행 증거가 부족하여 결과를 확정할 수 없습니다. 자동 탐색/실행 단계를 재시도해 주세요.",
                }
            }
            wrapped_result["final_output"] = final_result_payload
            if not has_form:
                final_event_result = final_result_payload["output"]["result"]
            else:
                final_event_result = {
                    "status": "Fail",
                    "reason": "missing_execution_evidence",
                    "result": final_event_result,
                }

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
            agent_name="최종 결과 작성자",
        )
        _enqueue_status(
            event_queue,
            "task_completed",
            # NOTE: For crew_type='result' we want the event payload itself to be the deliverable.
            # - no-form: keep wrapper with message + result string
            # - form: {"form_id": "...", "result": {...}} for UI extraction
            (
                final_event_result
                if has_form
                else {"message": "최종 결과 생성이 완료되었습니다.", "result": final_event_result}
            ),
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="final",
            agent_name="최종 결과 작성자",
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
