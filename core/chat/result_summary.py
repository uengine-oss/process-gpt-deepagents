"""업무(폼) 결과에서 사람이 읽을 요약을 떼어 낸다.

딥 에이전트는 폼 값과 함께 최상위 `_summary` 키에 2~3문장 요약을 쓴다(input_builder 의 응답 형식 안내).
요약은 폼 값이 아니다 — todolist(draft/output)와 프로세스 변수에 들어가지 않게 여기서 떼어 내고,
상태 이벤트(task_completed 의 agent_summary)로만 화면에 보낸다. 화면은 결과 폼 위 안내 상자로 보여 준다.
"""
from __future__ import annotations

import json

from core.utils.output_utils import extract_json_text

SUMMARY_KEY = "_summary"
MAX_SUMMARY_CHARS = 600


def split_result_summary(text: str) -> tuple[str, str]:
    """(요약을 뺀 결과 텍스트, 요약). 요약이 없거나 JSON 이 아니면 원문 그대로와 빈 요약."""
    raw = str(text or "")
    try:
        obj = json.loads(extract_json_text(raw))
    except (ValueError, TypeError):
        return raw, ""
    if not isinstance(obj, dict) or SUMMARY_KEY not in obj:
        return raw, ""
    summary = obj.pop(SUMMARY_KEY)
    summary = " ".join(str(summary).split()) if isinstance(summary, str) else ""
    return json.dumps(obj, ensure_ascii=False), summary[:MAX_SUMMARY_CHARS]
