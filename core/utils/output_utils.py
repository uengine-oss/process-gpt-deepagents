from __future__ import annotations

import json
import re
from typing import Any


def extract_json_text(text: str) -> str:
    """마크다운 코드 블록에서 JSON을 추출하거나 원본 텍스트를 반환한다."""
    stripped = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        return json.dumps(json.loads(stripped), ensure_ascii=False)
    except Exception:
        return stripped


def _last_assistant_message(messages: list[Any]) -> Any | None:
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role is not None and str(role).lower() in ("assistant", "ai"):
            return m
    return None


def _extract_plain_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)


def _is_oss_garbage(text: str) -> bool:
    """OpenAI 대리 모델이 남기는 텍스트 스캐폴딩 패턴을 감지한다."""
    return text.lstrip().startswith("<|") and "to=functions." in text


def extract_final_text(final_state: dict[str, Any], streamed_fallback: str = "") -> str:
    """그래프 최종 상태에서 assistant 응답 텍스트를 추출한다.

    OSS 모델 garbage 패턴을 필터링하고, 추출 실패 시 streamed_fallback으로 대체한다.
    """
    messages_list = final_state.get("messages") or []
    if not messages_list:
        return streamed_fallback

    last_asst = _last_assistant_message(messages_list)
    if last_asst is None:
        return streamed_fallback

    content = (
        last_asst.get("content") if isinstance(last_asst, dict)
        else getattr(last_asst, "content", None)
    )
    raw_text = _extract_plain_text(content)
    if _is_oss_garbage(raw_text):
        raw_text = ""
    return raw_text or streamed_fallback
