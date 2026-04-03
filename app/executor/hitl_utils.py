"""HITL decision extraction, deduplication, deferral, and context guards."""

from __future__ import annotations

import json
from typing import Any


def _extract_hitl_decision_from_output(output: Any) -> dict[str, Any] | None:
    """Inspect a tool output for a generic, domain-agnostic HITL signal."""
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
    if "input_schema" in output:
        decision["input_schema"] = output["input_schema"]
    if "output_schema" in output:
        decision["output_schema"] = output["output_schema"]
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
    """Return True to defer HITL so the agent can attempt more tool calls first."""
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


def _make_request_human_input_context_guard(
    context_tool_tracker: dict[str, Any] | None,
) -> Any:
    """Block orchestrator HITL until tenant mem0/memento/dmn_rule tools have been attempted (when configured)."""

    def _guard(base_tool: Any, tool_input: Any) -> Any | None:
        del tool_input
        if context_tool_tracker is None:
            return None
        if str(getattr(base_tool, "name", "") or "") != "request_human_input":
            return None
        missing = _missing_context_for_hitl(context_tool_tracker)
        if not missing:
            return None
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
