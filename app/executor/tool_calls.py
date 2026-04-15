"""LangGraph ToolCallRequest helpers and JSON-serialisable coercion for event payloads."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _json_safe(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable structures.

    - dict/list/tuple: recurse into elements
    - other non-serialisable objects: fall back to str()
    """
    # Special-case common LangChain/LangGraph objects so event payloads stay readable.
    try:
        from langchain_core.messages import BaseMessage  # type: ignore
    except Exception:  # pragma: no cover
        BaseMessage = None  # type: ignore
    try:
        from langchain_core.messages import ToolMessage  # type: ignore
    except Exception:  # pragma: no cover
        ToolMessage = None  # type: ignore
    try:
        from langgraph.types import Command  # type: ignore
    except Exception:  # pragma: no cover
        Command = None  # type: ignore

    # langgraph Command(update={...})
    if Command is not None and isinstance(value, Command):
        try:
            upd = getattr(value, "update", None)
        except Exception:
            upd = None
        return {"type": "command", "update": _json_safe(upd)}

    # langchain BaseMessage / ToolMessage
    if BaseMessage is not None and isinstance(value, BaseMessage):
        try:
            content = getattr(value, "content", None)
        except Exception:
            content = None
        out: dict[str, Any] = {"type": value.__class__.__name__}
        nm = getattr(value, "name", None)
        if isinstance(nm, str) and nm.strip():
            out["name"] = nm
        if ToolMessage is not None and isinstance(value, ToolMessage):
            tcid = getattr(value, "tool_call_id", None)
            if isinstance(tcid, str) and tcid.strip():
                out["tool_call_id"] = tcid
            status = getattr(value, "status", None)
            if isinstance(status, str) and status.strip():
                out["status"] = status
        # Prefer structured JSON if the content looks like it.
        if isinstance(content, str):
            s = content.strip()
            if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
                try:
                    parsed = json.loads(s)
                except Exception:
                    parsed = None
                out["content"] = parsed if parsed is not None else content
            else:
                out["content"] = content
        else:
            out["content"] = _json_safe(content)
        return out

    try:
        json.dumps(value)
        return value
    except TypeError:
        pass

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _tool_call_as_dict(request: Any) -> dict[str, Any]:
    """LangGraph ``ToolCallRequest.tool_call`` (또는 구형 ``call``)에서 호출 dict를 복사한다."""
    tc = getattr(request, "tool_call", None)
    if isinstance(tc, dict):
        return dict(tc)
    legacy = getattr(request, "call", None)
    if isinstance(legacy, dict):
        return dict(legacy)
    return {}


def _tool_call_args_from_request(request: Any) -> dict[str, Any]:
    tc = _tool_call_as_dict(request)
    raw = tc.get("args")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _request_with_merged_tool_args(request: Any, args: dict[str, Any]) -> Any:
    """``tool_call.args``만 갱신한 새 ``ToolCallRequest`` (override)를 반환한다."""
    tc = _tool_call_as_dict(request)
    if not tc:
        return request
    merged = {**tc, "args": dict(args)}
    override = getattr(request, "override", None)
    if not callable(override):
        return request
    try:
        return override(tool_call=merged)
    except Exception:
        logger.exception("Failed to override tool_call.args on ToolCallRequest", exc_info=True)
        return request
