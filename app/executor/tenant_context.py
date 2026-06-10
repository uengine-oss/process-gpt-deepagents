"""Tenant MCP filtering and one-shot context tool preflight."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
from typing import Any

from .hitl_utils import _classify_tenant_context_tool

logger = logging.getLogger(__name__)

_MCP_SERVER_KEYS_NEVER_LOAD: frozenset[str] = frozenset({"computer-use", "claude-skills"})


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


def _truncate_text(val: Any, *, max_chars: int = 2200) -> str:
    """Best-effort truncation for prompt injection."""
    if val is None:
        return ""
    try:
        s = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        s = str(val)
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    head = s[: max_chars // 2]
    tail = s[-max_chars // 3 :]
    return head + "\n…[truncated]…\n" + tail


async def _ainvoke_tool_best_effort(tool: Any, tool_input: dict[str, Any]) -> Any:
    """Invoke a tool with best-effort async compatibility."""
    ainvoke = getattr(tool, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(tool_input)
    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        return await asyncio.to_thread(invoke, tool_input)
    run = getattr(tool, "run", None)
    if callable(run):
        return await asyncio.to_thread(run, tool_input)
    raise TypeError(f"Tool {getattr(tool, 'name', type(tool).__name__)} is not invokable")


async def _prefetch_tenant_context_once(
    *,
    tools: list[Any],
    description: str,
    instruction: str,
    input_data: dict[str, Any],
    tenant_id: str,
    context_tool_tracker: dict[str, Any] | None,
) -> dict[str, Any]:
    """One-shot preflight for tenant context tools (mem0/memento/dmn_rule)."""
    enabled = str(os.environ.get("ENABLE_TENANT_CONTEXT_PREFLIGHT", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if not enabled:
        return {}

    by_cat: dict[str, Any] = {}
    for t in tools or []:
        cat = _classify_tenant_context_tool(t)
        if cat and cat not in by_cat:
            by_cat[cat] = t
    if not by_cat:
        return {}

    try:
        keys = sorted(list((input_data or {}).keys()))
    except Exception:
        keys = []
    compact_hint = {
        "tenant_id": tenant_id,
        "description": (description or "").strip(),
        "instruction": (instruction or "").strip(),
        "input_data_keys": keys[:24],
    }
    base_query = (
        "현재 작업을 성공/안전하게 수행하기 위한 선행 컨텍스트(과거 피드백/주의사항/필수 파라미터/정책/관련 문서)를 찾아주세요.\n"
        + _truncate_text(compact_hint, max_chars=900)
    )

    results: dict[str, Any] = {}
    timeout_s = float(os.environ.get("TENANT_CONTEXT_PREFLIGHT_TIMEOUT_SEC", "10") or "10")

    async def _call(cat: str, tool_obj: Any) -> None:
        tool_name = getattr(tool_obj, "name", type(tool_obj).__name__)
        payload: dict[str, Any]
        if cat == "dmn_rule":
            payload = {"query": base_query, "context": _truncate_text(compact_hint, max_chars=700)}
        else:
            payload = {"query": base_query}
        try:
            out = await asyncio.wait_for(_ainvoke_tool_best_effort(tool_obj, payload), timeout=timeout_s)
            results[cat] = {"tool": tool_name, "output": out}
            if context_tool_tracker is not None:
                attempted = context_tool_tracker.get("attempted_global")
                if isinstance(attempted, set):
                    attempted.add(cat)
        except Exception as e:
            results[cat] = {"tool": tool_name, "error": f"{type(e).__name__}: {e}"}

    await asyncio.gather(*[_call(cat, t) for cat, t in by_cat.items()])
    return results
