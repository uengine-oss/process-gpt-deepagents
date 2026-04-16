"""Normalize gpt-oss / LiteLLM channel-style tool text into OpenAI-style ``AIMessage.tool_calls``.

Some OpenAI-compatible backends return assistant ``content`` that embeds pseudo tool
invocations (channel markers, ``to=functions.<tool> code``, JSON arguments, then a call tail).

DeepAgents expects ``tool_calls`` on ``AIMessage``. Without normalization, tool routing fails
and ``task()`` can surface raw scaffolding as the subagent result.

Active only for the same model id set as :mod:`app.executor.compact_tool_result_middleware`
(``should_compact_tool_results_for_model`` — default ``frentis-ai-model``).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.tool import tool_call as lc_tool_call

from app.executor.compact_tool_result_middleware import should_compact_tool_results_for_model

logger = logging.getLogger(__name__)

_TO_FUNCTIONS_PREFIX = "to=functions."
_CALL_TAIL = "<|ca" + "ll|>"
_HEAD_RE = re.compile(
    # gpt-oss 계열은 아래처럼 변형된 헤더를 자주 냅니다.
    # - "analysis to=functions.foo code<|message|>{...}<|call|>"
    # - "commentary to=functions.foo <|constrain|>json<|message|>{...}"   (code/call 누락 가능)
    r"to=functions\.(?P<fn>[a-zA-Z0-9_-]+)\b[^\n]*?<\|mes+sage\|>",
    re.IGNORECASE,
)


def _flatten_ai_text_content(msg: AIMessage) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts: list[str] = []
        for block in c:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(c)


def _content_looks_like_oss_channel_tool_scaffold(text: str) -> bool:
    t = text or ""
    if _TO_FUNCTIONS_PREFIX in t:
        return True
    if "<|st" in t and "functions." in t:
        return True
    if "<|cha" in t and "to=functions" in t.replace(" ", ""):
        return True
    return False


def _parse_tool_blocks_from_oss_channel_text(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Return (tool_name, args_dict) pairs in document order; skips invalid JSON."""
    out: list[tuple[str, dict[str, Any]]] = []
    decoder = json.JSONDecoder()
    last_end = 0
    for m in _HEAD_RE.finditer(text):
        if m.start() < last_end:
            continue
        fn = (m.group("fn") or "").strip()
        if not fn:
            continue
        tail_from_head = text[m.end() :]
        json_blob = tail_from_head.lstrip()
        ws_before_json = len(tail_from_head) - len(json_blob)
        abs_json = m.end() + ws_before_json
        try:
            args, used = decoder.raw_decode(json_blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(args, dict):
            continue
        out.append((fn, args))
        # `<|call|>`이 아예 없거나(혹은 상위에서 ToolMessage가 잘려) tail이 소실될 수 있음.
        # 이런 경우에도 도구호출로 간주해서 실행 루프가 이어지게 하는 편이 낫다.
        after_json = text[abs_json + used :]
        after_ws = after_json.lstrip()
        ws2 = len(after_json) - len(after_ws)
        abs_after_ws = abs_json + used + ws2
        if after_ws[: len(_CALL_TAIL)].lower() == _CALL_TAIL.lower():
            last_end = abs_after_ws + len(_CALL_TAIL)
        else:
            last_end = abs_json + used
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen_last: tuple[str, str] | None = None
    for n, a in out:
        sig = (n, json.dumps(a, sort_keys=True, ensure_ascii=False))
        if seen_last == sig:
            continue
        seen_last = sig
        deduped.append((n, a))
    return deduped


def _normalize_single_aimessage(msg: AIMessage) -> AIMessage:
    existing = getattr(msg, "tool_calls", None) or []
    if existing:
        return msg
    flat = _flatten_ai_text_content(msg)
    if not flat.strip() or not _content_looks_like_oss_channel_tool_scaffold(flat):
        return msg
    pairs = _parse_tool_blocks_from_oss_channel_text(flat)
    if not pairs:
        return msg
    tcalls: list[Any] = []
    for fn, args in pairs:
        tid = f"call_{uuid.uuid4().hex[:24]}"
        tcalls.append(lc_tool_call(name=fn, args=args, id=tid))
    logger.info("oss_tool_normalize: AIMessage -> %d tool_call(s): %s", len(tcalls), [t["name"] for t in tcalls])
    try:
        return msg.model_copy(update={"tool_calls": tcalls, "content": ""})
    except Exception:
        try:
            return AIMessage(
                content="",
                tool_calls=tcalls,
                id=getattr(msg, "id", None),
                name=getattr(msg, "name", None),
            )
        except Exception:
            logger.exception("oss_tool_normalize: failed to rebuild AIMessage; returning original")
            return msg


def _normalize_model_response_payload(resp: Any) -> Any:
    try:
        from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse
    except Exception:
        ModelResponse = None  # type: ignore[misc, assignment]
        ExtendedModelResponse = None  # type: ignore[misc, assignment]

    if isinstance(resp, AIMessage):
        if ModelResponse is None:
            return _normalize_single_aimessage(resp)
        return ModelResponse(result=[_normalize_single_aimessage(resp)], structured_response=None)

    if ModelResponse is not None and isinstance(resp, ModelResponse):
        new_list: list[BaseMessage] = []
        changed = False
        for m in resp.result:
            if isinstance(m, AIMessage):
                nm = _normalize_single_aimessage(m)
                if nm is not m:
                    changed = True
                new_list.append(nm)
            else:
                new_list.append(m)
        if not changed:
            return resp
        return dataclasses.replace(resp, result=new_list)

    if ExtendedModelResponse is not None and isinstance(resp, ExtendedModelResponse):
        inner = _normalize_model_response_payload(resp.model_response)
        if inner is resp.model_response:
            return resp
        return dataclasses.replace(resp, model_response=inner)

    return resp


def create_oss_channel_tool_call_middleware(*, model_name: str) -> Any | None:
    """Return middleware that normalizes channel-style tool text for the configured OSS proxy models."""
    if not should_compact_tool_results_for_model(model_name):
        return None

    try:
        from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
    except Exception:
        logger.exception("oss_tool_normalize: AgentMiddleware import failed; skipping")
        return None

    class _OssChannelToolCallMiddleware(AgentMiddleware):
        def wrap_model_call(
            self,
            request: ModelRequest[Any],
            handler: Callable[[ModelRequest[Any]], Any],
        ) -> Any:
            raw = handler(request)
            return _normalize_model_response_payload(raw)

        async def awrap_model_call(
            self,
            request: ModelRequest[Any],
            handler: Callable[[ModelRequest[Any]], Awaitable[Any]],
        ) -> Any:
            raw = await handler(request)
            return _normalize_model_response_payload(raw)

    return _OssChannelToolCallMiddleware()
