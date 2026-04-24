"""HITL decision extraction, deduplication, deferral, and context guards."""

from __future__ import annotations

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


def _classify_tenant_context_tool(base_tool: Any) -> str | None:
    """Map a loaded tool to mem0 / memento / dmn_rule bucket (logging, preflight)."""
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
