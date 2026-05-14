"""HTTP 채팅에서 루트 오케스트레이터 도구 호출을 단계별로 제한한다.

목표:
- work-assistant MCP 도구만으로 해결 가능한 요청이면 MCP만으로 끝낸다(서브에이전트 위임 불필요).
- MCP만으로는 목표 달성이 어려워 다른 도구가 필요해지는 경우, 먼저 계획(write_todos/pipeline_spec)을 제출하고
  `task()`로 서브에이전트에 위임하도록 강제한다.

이 정책은 '요청 분류(하드코딩/휴리스틱)' 없이, 실제 도구 호출 패턴만으로 강제한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import ToolMessage
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]
    ToolMessage = None  # type: ignore[misc, assignment]

from app.executor.tool_calls import _tool_call_as_dict

_PLANNING_TOOL_NAMES = frozenset({"write_todos", "write_pipeline_spec"})
_HITL_TOOL_NAMES = frozenset({"request_human_input"})
_DEEPAGENTS_FILE_OR_EXEC = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
    }
)


def _tool_message_json(payload: dict[str, Any], *, tool_call_id: str, tool_name: str) -> Any:
    try:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        content = str(payload)
    if ToolMessage is not None and tool_call_id:
        return ToolMessage(content=content, name=tool_name or "tool", tool_call_id=tool_call_id)
    return content


def _is_work_assistant_mcp_tool(base_tool: Any) -> bool:
    srv = str(getattr(base_tool, "_mcp_server", "") or "").strip().lower().replace("_", "-")
    return srv == "work-assistant" or srv.endswith("work-assistant")


def _create_deep_required_root_gate_middleware(
    *,
    planning_gate: dict[str, Any],
    route_gate: dict[str, Any] | None = None,
) -> Any | None:
    """플랜 제출 전·서브에이전트 위임 전에 루트 전용 도구(스킬/파일/샌드박스 등)를 막는다."""
    if AgentMiddleware is None:
        return None

    class DeepRequiredRootGateMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            base_tool = getattr(request, "tool", None)
            tool_name = str(getattr(base_tool, "name", None) or "").strip()
            tc = _tool_call_as_dict(request)
            tool_call_id = str(tc.get("id") or "").strip()

            # HTTP chat: require explicit routing declaration first.
            route = ""
            if isinstance(route_gate, dict):
                route = str(route_gate.get("route") or "").strip()
            if not route:
                if tool_name in ("write_http_chat_route", "ask_user") or tool_name in _HITL_TOOL_NAMES:
                    return await handler(request)
                return _tool_message_json(
                    {
                        "error": "route_declaration_required",
                        "message": (
                            "HTTP 채팅에서는 먼저 `write_http_chat_route(route=...)`로 실행 라우팅을 선언해야 합니다. "
                            "필요하면 `request_human_input`으로 의도를 확인하세요."
                        ),
                    },
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )

            # Route=work_assistant_only: work-assistant MCP + 플랜 메모(write_todos 등) + HITL / 라우트 재선언.
            # (create_deep_agent 기본 write_todos는 오케스트레이션 추적용으로 WA 전용 정책과 충돌하지 않게 허용)
            if route == "work_assistant_only":
                if (
                    _is_work_assistant_mcp_tool(base_tool)
                    or tool_name in ("ask_user", "write_http_chat_route")
                    or tool_name in _PLANNING_TOOL_NAMES
                ):
                    return await handler(request)
                if tool_name in _HITL_TOOL_NAMES:
                    return await handler(request)
                return _tool_message_json(
                    {
                        "error": "work_assistant_only",
                        "message": (
                            "이 요청은 `work_assistant_only` 라우팅입니다. "
                            "work-assistant MCP, `write_todos`/`write_pipeline_spec`(플랜 메모), HITL은 사용할 수 있습니다. "
                            "`task()`·파일·샌드박스 등 그 밖 실행 도구는 허용되지 않습니다."
                        ),
                    },
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )

            # Route=ask_user_first: must ask user first (or re-declare route).
            if route == "ask_user_first":
                if tool_name in ("ask_user", "write_http_chat_route") or tool_name in _HITL_TOOL_NAMES:
                    return await handler(request)
                return _tool_message_json(
                    {
                        "error": "ask_user_first",
                        "message": (
                            "이 요청은 `ask_user_first` 라우팅입니다. "
                            "먼저 `request_human_input`으로 의도를 확인하세요."
                        ),
                    },
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )

            planned = bool(
                planning_gate.get("saw_write_todos") or planning_gate.get("saw_write_pipeline_spec")
            )
            delegated = bool(planning_gate.get("saw_task_delegation"))

            # 1) 플랜 전: 계획 제출·HITL·work-assistant MCP·task만 허용.
            if not planned:
                if (
                    tool_name in _PLANNING_TOOL_NAMES
                    or tool_name in _HITL_TOOL_NAMES
                    or tool_name == "task"
                    or _is_work_assistant_mcp_tool(base_tool)
                ):
                    return await handler(request)
                return _tool_message_json(
                    {
                        "error": "plan_submission_required",
                        "message": (
                            "MCP(work-assistant) 밖 도구를 사용하려면 먼저 "
                            "`write_todos` 또는 `write_pipeline_spec`으로 계획을 제출하세요."
                        ),
                    },
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )

            # 2) 플랜 후·위임 전: work-assistant MCP + task + 계획 보정 + HITL만.
            if not delegated:
                if (
                    tool_name in _PLANNING_TOOL_NAMES
                    or tool_name in _HITL_TOOL_NAMES
                    or tool_name == "task"
                    or _is_work_assistant_mcp_tool(base_tool)
                ):
                    return await handler(request)
                if tool_name in _DEEPAGENTS_FILE_OR_EXEC:
                    return _tool_message_json(
                        {
                            "error": "subagent_delegation_required",
                            "message": (
                                "계획은 제출되었으나 아직 `task()`로 서브에이전트에 위임되지 않았습니다. "
                                "MCP 밖 작업(파일·스킬·코드 실행 등)은 서브에이전트에서 수행하도록 `task()`를 호출하세요."
                            ),
                        },
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                # 스킬 해석·기타 MCP·샌드박스 등
                return _tool_message_json(
                    {
                        "error": "subagent_delegation_required",
                        "message": (
                            "work-assistant MCP만으로는 이 단계의 실행을 완료할 수 없습니다. "
                            "`task()`로 서브에이전트에 위임한 뒤, 필요 시 work-assistant MCP 도구를 병행하세요."
                        ),
                    },
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                )

            return await handler(request)

    return DeepRequiredRootGateMiddleware()
