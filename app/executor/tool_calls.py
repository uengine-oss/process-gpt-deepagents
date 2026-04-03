"""LangGraph ToolCallRequest helpers."""

from __future__ import annotations

from typing import Any


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
        import logging

        logging.getLogger(__name__).exception(
            "Failed to override tool_call.args on ToolCallRequest", exc_info=True
        )
        return request
