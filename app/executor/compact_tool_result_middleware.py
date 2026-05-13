"""Truncate oversized tool results for narrow-context proxy models (e.g. gpt-oss via LiteLLM).

**When it runs:** Same stack as 폴링 워크아이템 실행 — SDK chat(SSE)도 ``DeepAgentsExecutor.execute()`` 를
호출하므로 별도 분기 없이 동일하게 적용된다.

**Which model:** Resolved ``LLM_MODEL`` (또는 요청 ``metadata.model`` 이 extras에 들어간 값)의 프록시용 id가
``frentis-ai-model`` 이면 활성화된다. 그 외 모델은 변경 없음.

**환경변수:** 필수 아님. 설정하지 않으면 코드 기본값만 쓴다 — 대상 모델 id ``frentis-ai-model``,
도구 결과 상한 ``10_000`` 문자. ``LLM_COMPACT_TOOL_RESULT_MODEL_IDS`` /
``LLM_COMPACT_TOOL_RESULT_MAX_CHARS`` 는 운영에서 튜닝할 때만 선택적으로 지정하면 된다.

This middleware must be attached to **each** subagent spec's ``middleware`` list: ``create_deep_agent``
does not forward the root ``middleware=`` list into per-subagent graphs.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

_DEFAULT_COMPACT_MODEL_IDS = "frentis-ai-model"
_DEFAULT_MAX_TOOL_CHARS = 10_000


def _parse_model_id_list(raw: str | None) -> frozenset[str]:
    s = (raw or "").strip()
    if not s:
        s = _DEFAULT_COMPACT_MODEL_IDS
    parts = {p.strip().lower() for p in s.split(",") if p.strip()}
    return frozenset(parts)


def _litellm_openai_model_id(model_name: str) -> str:
    """Match :func:`app.agent.factory._openai_compatible_litellm_model_id` (substring after ``:``)."""
    s = (model_name or "").strip()
    if ":" in s:
        _, rest = s.split(":", 1)
        return (rest.strip() or s).strip().lower()
    return s.lower()


def should_compact_tool_results_for_model(model_name: str) -> bool:
    """Return True if tool outputs should be compacted for this resolved ``LLM_MODEL`` / routing name."""
    mid = _litellm_openai_model_id(model_name)
    if not mid:
        return False
    allowed = _parse_model_id_list(os.environ.get("LLM_COMPACT_TOOL_RESULT_MODEL_IDS"))
    return mid in allowed


def _max_tool_chars() -> int:
    raw = (os.environ.get("LLM_COMPACT_TOOL_RESULT_MAX_CHARS") or "").strip()
    if not raw:
        return _DEFAULT_MAX_TOOL_CHARS
    try:
        v = int(raw)
    except ValueError:
        logger.warning(
            "Invalid LLM_COMPACT_TOOL_RESULT_MAX_CHARS=%r; using default %d",
            raw,
            _DEFAULT_MAX_TOOL_CHARS,
        )
        return _DEFAULT_MAX_TOOL_CHARS
    v = max(1, min(v, 500_000))
    return v


def _flatten_content_for_measure(content: Any) -> str:
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


def _compact_tool_message(msg: ToolMessage, *, max_chars: int) -> ToolMessage:
    flat = _flatten_content_for_measure(msg.content)
    if len(flat) <= max_chars:
        return msg
    head = flat[:max_chars]
    note = (
        f"\n\n[[COMPACTED]] 원본 약 {len(flat)}자 중 앞 {max_chars}자만 유지했습니다. "
        "나머지는 동일 도구를 더 좁은 인자로 재호출하거나, 문서/파일 도구로 단건 조회하세요."
    )
    new_text = head + note
    tool_name = getattr(msg, "name", None) or "tool"
    logger.info(
        "compact_tool_result: tool=%s chars %d -> %d (max=%d)",
        tool_name,
        len(flat),
        len(new_text),
        max_chars,
    )
    if hasattr(msg, "model_copy"):
        try:
            return msg.model_copy(update={"content": new_text})
        except Exception:
            pass
    try:
        return ToolMessage(
            content=new_text,
            name=str(tool_name),
            tool_call_id=str(getattr(msg, "tool_call_id", "") or ""),
        )
    except Exception:
        return msg


def _compact_result_value(result: Any, *, max_chars: int) -> Any:
    if isinstance(result, ToolMessage):
        return _compact_tool_message(result, max_chars=max_chars)
    # Command: optional state update with messages
    try:
        from langgraph.types import Command
    except Exception:
        Command = None  # type: ignore[misc, assignment]
    if Command is not None and isinstance(result, Command):
        upd = getattr(result, "update", None)
        if isinstance(upd, dict):
            msgs = upd.get("messages")
            if isinstance(msgs, list) and msgs:
                new_msgs: list[Any] = []
                changed = False
                for m in msgs:
                    if isinstance(m, ToolMessage):
                        nm = _compact_tool_message(m, max_chars=max_chars)
                        if nm is not m:
                            changed = True
                        new_msgs.append(nm)
                    else:
                        new_msgs.append(m)
                if changed:
                    new_upd = dict(upd)
                    new_upd["messages"] = new_msgs
                    try:
                        return dataclasses.replace(result, update=new_upd)
                    except Exception:
                        logger.exception("compact_tool_result: failed to rebuild Command; returning original")
        return result
    return result


def create_compact_tool_result_middleware(*, model_name: str) -> Any | None:
    """Return middleware instance if ``model_name`` is in the compact allowlist; else ``None``."""
    if not should_compact_tool_results_for_model(model_name):
        return None
    max_chars = _max_tool_chars()

    try:
        from langchain.agents.middleware.types import AgentMiddleware
        from langgraph.prebuilt.tool_node import ToolCallRequest
        from langgraph.types import Command as LGCommand
    except Exception:
        logger.exception("compact_tool_result: imports failed; skipping middleware")
        return None

    class _CompactToolResultMiddleware(AgentMiddleware):
        def wrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], ToolMessage | LGCommand],
        ) -> ToolMessage | LGCommand:
            out = handler(request)
            return _compact_result_value(out, max_chars=max_chars)

        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | LGCommand]],
        ) -> ToolMessage | LGCommand:
            out = await handler(request)
            return _compact_result_value(out, max_chars=max_chars)

    return _CompactToolResultMiddleware()
