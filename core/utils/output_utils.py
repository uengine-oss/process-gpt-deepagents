from __future__ import annotations

import json
import re
from typing import Any


def _iter_balanced_json_objects(text: str):
    """문자열에서 균형 잡힌 최상위 {...} 블록을 찾아 파싱되는 dict 를 순서대로 yield.

    문자열 리터럴 안의 중괄호/escape 를 인지해 오탐을 줄인다.
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = text[i : j + 1]
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict):
                                yield obj
                        except Exception:
                            pass
                        break
            j += 1
        i = j + 1 if j > i else i + 1


def extract_json_text(text: str) -> str:
    """설명 문구가 섞여 있어도 폼 출력 JSON 을 최대한 뽑아낸다.

    우선순위: (1) ```json 코드펜스 안, (2) 문자열 전체가 순수 JSON,
    (3) 혼합 텍스트에서 균형 잡힌 최상위 {...} 블록 중 파싱되는 가장 큰 dict.
    모두 실패하면 원문을 반환한다.
    """
    stripped = (text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if match:
        stripped = match.group(1).strip()
    try:
        return json.dumps(json.loads(stripped), ensure_ascii=False)
    except Exception:
        pass
    # 설명 + JSON 혼합: 가장 큰(=폼 전체) top-level dict 를 선택한다.
    best = None
    best_len = -1
    for obj in _iter_balanced_json_objects(stripped):
        try:
            s = json.dumps(obj, ensure_ascii=False)
        except Exception:
            continue
        if len(s) > best_len:
            best = s
            best_len = len(s)
    if best is not None:
        return best
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
