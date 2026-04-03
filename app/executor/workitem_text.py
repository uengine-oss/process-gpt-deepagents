"""Work-item query parsing and text/form helpers."""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _parse_block(source: str, header: str, next_headers: list[str]) -> str:
    """Extract a block that starts after header and ends before any next_headers."""
    if not source:
        return ""
    start_idx = source.find(header)
    if start_idx == -1:
        return ""
    start_idx += len(header)
    tail = source[start_idx:]
    # Find the earliest occurrence of any other header.
    end_idx = len(tail)
    for h in next_headers:
        idx = tail.find(h)
        if idx != -1 and idx < end_idx:
            end_idx = idx
    return tail[:end_idx].strip()


def _is_korean_user_input(text: str) -> bool:
    """Return True if the text contains Korean (Hangul) characters."""
    if not (text or "").strip():
        return False
    return bool(re.search(r"[가-힣]", text))


def parse_workitem_query(query: str) -> dict[str, Any]:
    """Parse [Description]/[Instruction]/[InputData] blocks from the work-item query."""
    text = query or ""
    # Normalise newlines for simpler parsing.
    text = text.replace("\r\n", "\n")

    desc = _parse_block(
        text,
        "[Description]",
        ["[Instruction]", "[InputData]"],
    )
    instr = _parse_block(
        text,
        "[Instruction]",
        ["[Description]", "[InputData]"],
    )
    input_raw = _parse_block(
        text,
        "[InputData]",
        ["[Description]", "[Instruction]"],
    )

    input_data: dict[str, Any] = {}
    if input_raw:
        raw = input_raw.strip()
        # Allow optional surrounding code fences or text around JSON.
        json_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        candidate = json_match.group(0) if json_match else raw
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                input_data = parsed
        except Exception:
            logger.warning("Failed to parse InputData JSON from query; using empty dict.")

    return {
        "description": desc,
        "instruction": instr,
        "input_data_raw": input_raw,
        "input_data": input_data,
    }


def _extract_plain_text(value: Any) -> str:
    """Extract plain text from common message/content structures without stringifying objects."""
    if isinstance(value, str):
        # 문자열이 리스트/딕셔너리 표현일 수 있으므로 한 번 파싱을 시도한다.
        text_str = value.strip()
        parsed: Any | None = None
        # JSON 파싱 시도
        try:
            parsed = json.loads(text_str)
        except Exception:
            parsed = None
        # JSON이 아니면 literal_eval 시도 (list/dict 등의 리터럴만 허용)
        if parsed is None:
            try:
                parsed = ast.literal_eval(text_str)
            except Exception:
                parsed = None

        if isinstance(parsed, (list, dict)):
            # 파싱에 성공했다면, 그 안에서 텍스트 필드만 추출한다.
            return _extract_plain_text(parsed)
        # 일반 문자열이면 그대로 반환
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return ""


def _join_todo_contents(value: Any) -> str:
    """Join todo item `content` values into a single plain-text message."""
    parsed = value
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                try:
                    parsed = ast.literal_eval(raw)
                except Exception:
                    parsed = value

    if isinstance(parsed, dict):
        parsed = parsed.get("todos", parsed)

    if isinstance(parsed, list):
        parts: list[str] = []
        for item in parsed:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        if parts:
            return "\n".join(parts)

    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _extract_form_field_keys(form_fields: Any) -> list[str]:
    """Best-effort extract of field keys from form schema."""
    keys: list[str] = []
    if not isinstance(form_fields, list):
        return keys
    for field in form_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key") or field.get("name") or field.get("id")
        if isinstance(key, str):
            stripped = key.strip()
            if stripped and stripped not in keys:
                keys.append(stripped)
    return keys


def _build_form_event_result(
    form_id: str | None,
    form_fields: Any,
    pure_form_data: Any,
    fallback_text: str = "",
) -> dict[str, Any]:
    """Build JSON for final result event: {form_id: {field_key: value, ...}}."""
    field_keys = _extract_form_field_keys(form_fields)
    source = pure_form_data if isinstance(pure_form_data, dict) else {}
    inner: dict[str, Any] = {}

    for key in field_keys:
        value = source.get(key)
        if value is None:
            value = ""
        inner[key] = value

    # If model returned only text but schema has a single field,
    # map that text to the only field for better downstream UX.
    if fallback_text.strip() and len(field_keys) == 1 and not inner.get(field_keys[0]):
        inner[field_keys[0]] = fallback_text.strip()

    fid = (form_id or "").strip()
    if fid:
        return {fid: inner}
    return inner


def _coerce_tool_input_dict(tool_input: Any) -> dict[str, Any] | None:
    """Best-effort coercion for tool inputs that should be JSON objects."""
    if isinstance(tool_input, dict):
        return dict(tool_input)
    if isinstance(tool_input, str):
        raw = tool_input.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            return None
    return None


def _extract_plan_block(source: str) -> str:
    """Extract the [PLAN]...[/PLAN] block from orchestrator output. Returns plain text only."""
    if not source or not isinstance(source, str):
        return ""
    text = source.strip()
    start_marker = "[PLAN]"
    end_marker = "[/PLAN]"
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        # No closing tag: take from start to end of string
        block = text[start_idx:].strip()
    else:
        block = text[start_idx:end_idx].strip()
    # Ensure we return plain text (no list/dict string representation)
    return _extract_plain_text(block) if block else ""


def _strip_plan_blocks(source: str) -> str:
    """Remove any [PLAN]...[/PLAN] blocks from user-facing output."""
    if not source or not isinstance(source, str):
        return ""
    text = source
    # Remove complete blocks.
    text = re.sub(r"\[PLAN\].*?\[/PLAN\]\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    # If there's a dangling opening tag, drop everything from it onward.
    m = re.search(r"\[PLAN\]", text, flags=re.IGNORECASE)
    if m:
        text = text[: m.start()].rstrip()
    return text.strip()


def _extract_result_block(source: str) -> str:
    """Extract the [RESULT]...[/RESULT] block from output, if present."""
    if not source or not isinstance(source, str):
        return ""
    m = re.search(r"\[RESULT\](.*?)\[/RESULT\]", source, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return (m.group(1) or "").strip()
    cleaned = source.strip()
    if cleaned.upper().startswith("[RESULT]"):
        return cleaned[len("[RESULT]") :].strip()
    return ""


def _derive_root_goal(description: str, instruction: str, user_input: str) -> str:
    """Build a request-specific root goal string for depth=1 task events."""
    if isinstance(description, str) and description.strip():
        return description.strip()
    if isinstance(instruction, str) and instruction.strip():
        return instruction.strip()
    if isinstance(user_input, str) and user_input.strip():
        one_line = " ".join(user_input.strip().split())
        if len(one_line) > 200:
            return f"{one_line[:200]}..."
        return one_line
    return "요청된 작업을 수행합니다."
