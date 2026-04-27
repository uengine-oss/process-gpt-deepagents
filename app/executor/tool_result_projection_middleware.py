"""Tool result projection middleware: keep only necessary fields in ToolMessage content.

Goal:
- Avoid injecting full tool outputs into subsequent model calls.
- Prefer: (a) structured projection for known tools, (b) safe truncation for text blobs.

Configuration (optional):
- Rules can be provided programmatically via ``create_tool_result_projection_middleware(rules=...)``.
  Rule schema (all optional):
    {
      "max_chars": 6000,              // truncate text fallback
      "keep_keys": ["a","b"],         // keep only these keys if JSON dict
      "drop_keys": ["raw","content"], // drop these keys from JSON dict
      "list_head": 20,                // if JSON is list, keep only first N
      "redact_keys": ["token"],       // redact these keys in dicts (set to "***")
    }

Notes:
- This middleware is intentionally conservative; it never expands content, only reduces it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 6_000
_DEFAULT_LIST_HEAD = 20


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


def _truncate(text: str, *, max_chars: int) -> str:
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    return (
        head
        + "\n\n[[PROJECTED]] 원본이 커서 필요한 일부만 유지했습니다. "
        "필요한 필드/범위를 지정해 도구를 더 좁게 재호출하세요."
    )


def _maybe_parse_json(s: str) -> Any | None:
    t = (s or "").strip()
    if not t:
        return None
    if not ((t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]"))):
        return None
    try:
        return json.loads(t)
    except Exception:
        return None


def _redact_in_obj(obj: Any, *, redact_keys: set[str]) -> Any:
    if not redact_keys:
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in redact_keys:
                out[ks] = "***"
            else:
                out[ks] = _redact_in_obj(v, redact_keys=redact_keys)
        return out
    if isinstance(obj, list):
        return [_redact_in_obj(x, redact_keys=redact_keys) for x in obj]
    return obj


def _apply_rule_to_json(value: Any, *, rule: dict[str, Any]) -> Any:
    keep_keys = rule.get("keep_keys")
    drop_keys = rule.get("drop_keys")
    redact_keys = rule.get("redact_keys")
    list_head = rule.get("list_head")

    rk: set[str] = set()
    if isinstance(redact_keys, list):
        rk = {str(x) for x in redact_keys if str(x)}

    if isinstance(value, dict):
        d = dict(value)
        if isinstance(drop_keys, list):
            for k in drop_keys:
                d.pop(str(k), None)
        if isinstance(keep_keys, list) and keep_keys:
            kk = {str(x) for x in keep_keys if str(x)}
            d = {k: v for k, v in d.items() if k in kk}
        d = _redact_in_obj(d, redact_keys=rk)
        return d

    if isinstance(value, list):
        n = _DEFAULT_LIST_HEAD
        if isinstance(list_head, int) and list_head > 0:
            n = min(list_head, 500)
        head = value[:n]
        head = _redact_in_obj(head, redact_keys=rk)
        if len(value) > n:
            return {"items_head": head, "total_items": len(value), "truncated": True}
        return head

    return value


def _default_rule_for_tool(tool_name: str) -> dict[str, Any] | None:
    # Conservative built-ins for common high-volume tools.
    if tool_name == "list_skills":
        return {"list_head": 30, "max_chars": 6000, "drop_keys": ["full_content", "content", "raw"]}
    if tool_name == "find_helpful_skills":
        return {"list_head": 10, "max_chars": 6000, "drop_keys": ["full_content", "content", "raw"]}
    if tool_name == "read_skill_document":
        # Keep doc header/body head only.
        return {"max_chars": 8000}
    if tool_name.startswith("sandbox_"):
        # Prefer short heads; leave structured stdout capture to executor hook.
        return {"keep_keys": ["exit_code", "stdout", "stderr", "command"], "max_chars": 6000}
    return None


def _project_tool_message(msg: ToolMessage, *, rules: dict[str, dict[str, Any]]) -> ToolMessage:
    tool_name = str(getattr(msg, "name", "") or "").strip()
    rule = rules.get(tool_name) or _default_rule_for_tool(tool_name) or {}

    max_chars = _DEFAULT_MAX_CHARS
    if isinstance(rule.get("max_chars"), int) and int(rule["max_chars"]) > 0:
        max_chars = max(200, min(int(rule["max_chars"]), 200_000))

    flat = _flatten_tool_content(msg.content)
    parsed = _maybe_parse_json(flat)
    if parsed is not None:
        projected = _apply_rule_to_json(parsed, rule=rule)
        # keep JSON as string to stay compatible with downstream expectations
        new_text = json.dumps(projected, ensure_ascii=False)
    else:
        new_text = _truncate(flat, max_chars=max_chars)

    if len(new_text) > max_chars:
        new_text = _truncate(new_text, max_chars=max_chars)

    if new_text == flat:
        return msg

    logger.info("tool_result_projection: tool=%s chars %d -> %d", tool_name or "tool", len(flat), len(new_text))
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


def _project_result_value(value: Any, *, rules: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, ToolMessage):
        return _project_tool_message(value, rules=rules)
    # Command(update={"messages": [...]})
    try:
        from langgraph.types import Command as LGCommand
    except Exception:
        LGCommand = None  # type: ignore[misc, assignment]
    if LGCommand is not None and isinstance(value, LGCommand):
        upd = getattr(value, "update", None)
        if isinstance(upd, dict):
            msgs = upd.get("messages")
            if isinstance(msgs, list) and msgs:
                changed = False
                new_msgs: list[Any] = []
                for m in msgs:
                    if isinstance(m, ToolMessage):
                        nm = _project_tool_message(m, rules=rules)
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
                        logger.exception("tool_result_projection: failed to rebuild Command; returning original")
        return value
    return value


def create_tool_result_projection_middleware() -> Any | None:
    """Always-on middleware to project ToolMessage content to small inputs."""
    return create_tool_result_projection_middleware_with_rules(rules=None)


def create_tool_result_projection_middleware_with_rules(*, rules: dict[str, dict[str, Any]] | None) -> Any | None:
    """Projection middleware with optional per-tool rules (no env vars).

    If ``rules`` is None, only built-in defaults are applied.
    """
    try:
        from langchain.agents.middleware.types import AgentMiddleware
        from langgraph.prebuilt.tool_node import ToolCallRequest
        from langgraph.types import Command as LGCommand
    except Exception:
        return None

    effective_rules = rules if isinstance(rules, dict) else {}

    class _ToolResultProjectionMiddleware(AgentMiddleware):
        def wrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], ToolMessage | LGCommand],
        ) -> ToolMessage | LGCommand:
            out = handler(request)
            return _project_result_value(out, rules=effective_rules)

        async def awrap_tool_call(
            self,
            request: ToolCallRequest,
            handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | LGCommand]],
        ) -> ToolMessage | LGCommand:
            out = await handler(request)
            return _project_result_value(out, rules=effective_rules)

    return _ToolResultProjectionMiddleware()

