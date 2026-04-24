"""Always-on tool result hygiene to reduce repeated input tokens.

Why this exists:
- Some tool outputs are extremely large (e.g., find_helpful_skills full content, list_skills catalog dumps,
  sandbox stdout JSON). Even when prompt caching exists, these large blobs can be re-injected into later
  model calls and explode input tokens.

This middleware truncates only known high-risk tool results and leaves everything else unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)


_DEFAULT_MAX_CHARS = 12_000
_SANDBOX_TOOLS_PREFIX = "sandbox_"
_HIGH_RISK_TOOLS: set[str] = {
    "find_helpful_skills",
    "list_skills",
    "read_skill_document",
}


def _flatten_tool_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _truncate_text(text: str, *, max_chars: int) -> str:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    return (
        head
        + "\n\n[[TRUNCATED_FOR_TOKEN_HYGIENE]] 원본이 너무 커서 앞부분만 유지했습니다. "
        "필요하면 더 좁은 인자로 재호출하거나(문서/필드 단건), 스크립트 출력 자체를 축소하세요."
    )


def _compact_tool_message_if_needed(msg: ToolMessage, *, max_chars: int) -> ToolMessage:
    tool_name = str(getattr(msg, "name", "") or "")
    is_high_risk = tool_name in _HIGH_RISK_TOOLS or tool_name.startswith(_SANDBOX_TOOLS_PREFIX)
    if not is_high_risk:
        return msg
    flat = _flatten_tool_content(msg.content)
    new_text = _truncate_text(flat, max_chars=max_chars)
    if new_text == flat:
        return msg
    logger.info("token_hygiene: tool=%s chars %d -> %d", tool_name, len(flat), len(new_text))
    if hasattr(msg, "model_copy"):
        try:
            return msg.model_copy(update={"content": new_text})
        except Exception:
            pass
    try:
        return ToolMessage(
            content=new_text,
            name=tool_name or "tool",
            tool_call_id=str(getattr(msg, "tool_call_id", "") or ""),
        )
    except Exception:
        return msg


def create_token_hygiene_tool_result_middleware(*, max_chars: int = _DEFAULT_MAX_CHARS) -> Any | None:
    try:
        from langchain.agents.middleware.types import AgentMiddleware
        from langgraph.prebuilt.tool_node import ToolCallRequest
        from langgraph.types import Command as LGCommand
    except Exception:
        return None

    max_chars = max(1000, int(max_chars))

    def _compact_result_value(value: Any) -> Any:
        if isinstance(value, ToolMessage):
            return _compact_tool_message_if_needed(value, max_chars=max_chars)
        # Command(update={"messages": [...]})
        if isinstance(value, LGCommand):
            upd = getattr(value, "update", None)
            if isinstance(upd, dict):
                msgs = upd.get("messages")
                if isinstance(msgs, list) and msgs:
                    changed = False
                    new_msgs: list[Any] = []
                    for m in msgs:
                        if isinstance(m, ToolMessage):
                            nm = _compact_tool_message_if_needed(m, max_chars=max_chars)
                            if nm is not m:
                                changed = True
                            new_msgs.append(nm)
                        else:
                            new_msgs.append(m)
                    if changed:
                        new_upd = dict(upd)
                        new_upd["messages"] = new_msgs
                        try:
                            return dataclasses.replace(value, update=new_upd)
                        except Exception:
                            return value
        return value

    class _TokenHygieneToolResultMiddleware(AgentMiddleware):
        def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]
            out = handler(request)
            return _compact_result_value(out)

        async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:  # type: ignore[override]
            out = await handler(request)
            return _compact_result_value(out)

    return _TokenHygieneToolResultMiddleware()

