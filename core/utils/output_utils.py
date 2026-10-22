from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

# langchain(1.3.4)/deepagents(0.6.8) 에이전트 루프가 도구 호출 없는 응답 뒤에도
# 종료로 판단하지 못하고 한 턴을 더 돌아, 이미 낸 최종 답변을 다시 요약해 내는
# 사례가 관측됐다(LiteLLM 로그로 확인: 두 번째 호출의 프롬프트에 첫 번째 답변
# 전체가 컨텍스트로 포함됨). 그 결과 마지막 assistant 메시지의 content 가
# "거의 같은 내용을 담은 text 블록 여러 개"로 남는다. 프레임워크 쪽 루프 종료
# 판정 버그이므로 여기서 근본 수정은 어렵고, 저장 직전에 마지막 블록만 남기는
# 방어 로직으로 사용자에게 중복 노출되는 것만 막는다.
_DUPLICATE_TEXT_BLOCK_SIMILARITY_THRESHOLD = 0.5


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
        fenced = match.group(1).strip()
        try:
            return json.dumps(json.loads(fenced), ensure_ascii=False)
        except Exception:
            # 코드펜스가 JSON 이 아니면(예: 폼 필드 값 안에 들어간 ``` 블록) 원문 전체에서 JSON 을 찾는다
            pass
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


def _text_blocks_look_like_full_restatement(parts: list[str]) -> bool:
    """마지막 블록이 앞선 블록들을 사실상 다시 요약/반복한 것인지 판단.

    각 블록이 그 자체로 상당한 분량(대충 하나의 완결된 답변으로 볼 만한 길이)이고,
    마지막 블록이 그 앞의 어느 블록과도 내용이 크게 겹치면(문자 단위 유사도 임계치
    이상) "같은 답을 다시 냈다"로 본다. 어느 조건 하나라도 안 맞으면(예: 첫 블록이
    아주 짧은 인사말/전환 문구, 혹은 블록들이 서로 다른 내용을 이어 쓴 정상적인 긴
    답변) 원래대로 이어붙인다 — 정상적인 다중 텍스트 블록 답변을 잘라내는 회귀를
    피하기 위함.
    """
    if len(parts) < 2:
        return False
    last = parts[-1].strip()
    if len(last) < 80:  # 너무 짧은 마지막 블록은 "반복 요약"으로 보기 어렵다
        return False
    for earlier in parts[:-1]:
        earlier = earlier.strip()
        if len(earlier) < 80:
            continue
        ratio = SequenceMatcher(None, earlier, last).ratio()
        if ratio >= _DUPLICATE_TEXT_BLOCK_SIMILARITY_THRESHOLD:
            return True
    return False


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
        if _text_blocks_look_like_full_restatement(parts):
            return parts[-1]
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
