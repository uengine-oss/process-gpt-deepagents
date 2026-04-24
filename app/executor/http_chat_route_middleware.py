"""HTTP chat routing middleware: force explicit route declaration before execution.

This middleware registers a tool that the root orchestrator must call early to declare
one of three execution routes:
- work_assistant_only: finish using work-assistant MCP tools only (no task()).
- needs_task: work-assistant MCP is insufficient; must delegate via task() (planning required).
- ask_user_first: user intent is ambiguous; must call request_human_input first.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Sequence

try:
    from langchain.agents.middleware.types import AgentMiddleware, AgentState
except ImportError:  # pragma: no cover
    AgentMiddleware = None  # type: ignore[misc, assignment]
    AgentState = dict  # type: ignore[misc, assignment]
from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import Command
from typing_extensions import NotRequired, TypedDict, override

logger = logging.getLogger(__name__)


ALLOWED_HTTP_CHAT_ROUTES: tuple[str, ...] = ("work_assistant_only", "needs_task", "ask_user_first")


def normalize_http_chat_route(value: str | None) -> str:
    s = (value or "").strip().lower().replace("-", "_")
    if s in ("work_assistant_only", "workassistant_only", "work_assistant"):
        return "work_assistant_only"
    if s in ("needs_task", "need_task", "task", "delegate", "delegation_required"):
        return "needs_task"
    if s in ("ask_user_first", "ask_user", "hitl", "clarify"):
        return "ask_user_first"
    return s


class HttpChatRouteAgentState(AgentState[Any]):
    http_chat_route: NotRequired[str]


WRITE_HTTP_CHAT_ROUTE_DESCRIPTION = """Declare the HTTP chat execution route.

You MUST call this tool before calling any other tools (except request_human_input),
and before calling task().

Allowed routes:
- work_assistant_only: The user request can be completed using only work-assistant MCP tools.
- needs_task: work-assistant MCP tools alone are insufficient; you will submit a plan and call task().
- ask_user_first: The user intent is ambiguous; you will call request_human_input first.
"""


def create_http_chat_route_middleware(*, route_gate: dict[str, Any]) -> Any | None:
    if AgentMiddleware is None:
        return None

    class HttpChatRouteMiddleware(AgentMiddleware[HttpChatRouteAgentState, Any, Any]):
        state_schema = HttpChatRouteAgentState  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()

            @tool(description=WRITE_HTTP_CHAT_ROUTE_DESCRIPTION)
            def write_http_chat_route(
                route: str,
                reason: str | None = None,
                tool_call_id: Annotated[str, InjectedToolCallId] = "",
            ) -> Command[Any]:
                r0 = normalize_http_chat_route(route)
                if r0 not in ALLOWED_HTTP_CHAT_ROUTES:
                    payload = {
                        "ok": False,
                        "error": "invalid_route",
                        "allowed": list(ALLOWED_HTTP_CHAT_ROUTES),
                        "received": route,
                    }
                    return Command(
                        update={
                            "messages": [
                                ToolMessage(
                                    content=json.dumps(payload, ensure_ascii=False),
                                    tool_call_id=tool_call_id,
                                    status="error",
                                )
                            ]
                        }
                    )
                # Side-channel for middleware gates.
                route_gate["route"] = r0
                if isinstance(reason, str) and reason.strip():
                    route_gate["reason"] = reason.strip()
                payload_ok = {"ok": True, "route": r0}
                return Command(
                    update={
                        "http_chat_route": r0,
                        "messages": [
                            ToolMessage(
                                content=json.dumps(payload_ok, ensure_ascii=False),
                                tool_call_id=tool_call_id,
                            )
                        ],
                    }
                )

            self.tools: Sequence[Any] = [write_http_chat_route]

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            # Ensure the orchestrator sees the route tool in-system instructions.
            # Token hygiene: once a route is already declared (or preselected by the executor),
            # do not re-inject these instructions on every model turn.
            try:
                if isinstance(route_gate, dict) and str(route_gate.get("route") or "").strip():
                    return await handler(request)
            except Exception:
                pass
            extra = (
                "\n\n## `write_http_chat_route`\n"
                "HTTP 채팅에서는 반드시 먼저 `write_http_chat_route(route=...)`로 라우팅을 선언하세요.\n"
                "- work_assistant_only: work-assistant MCP만으로 완결 (task() 금지)\n"
                "- needs_task: MCP만으로 불충분 → 계획 제출 후 task()\n"
                "- ask_user_first: 의도/대상이 모호 → request_human_input 먼저\n"
            )
            try:
                sys_msg = getattr(request, "system_message", None)
                if sys_msg is not None:
                    new_system_content = [*sys_msg.content_blocks, {"type": "text", "text": extra}]
                else:
                    new_system_content = [{"type": "text", "text": extra.lstrip()}]
                new_system_message = SystemMessage(content=new_system_content)  # type: ignore[arg-type]
                return await handler(request.override(system_message=new_system_message))
            except Exception:
                logger.exception("Failed to inject write_http_chat_route system message; continuing", exc_info=True)
                return await handler(request)

        @override
        async def aafter_model(self, state: HttpChatRouteAgentState, runtime: Any) -> dict[str, Any] | None:
            # No post-processing required.
            _ = runtime
            _ = state
            return None

    return HttpChatRouteMiddleware()

