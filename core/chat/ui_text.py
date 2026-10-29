"""채팅에 남기는 고정 안내 문구 — 사용자가 쓴 말(한국어/영어)에 맞춘다.

에이전트(LLM)의 답은 스킬 지시대로 사용자 언어를 따르지만, executor 가 직접 덮어쓰는
완료·보류 안내와 5단계 todo 는 고정 문구라 영어로 묻는 사용자에게도 한국어로 나갔다.
언어는 이번 요청 원문(HITL 답변 턴이면 원 요청)으로 정한다: 한글이 없고 영문자가 있으면 영어, 그 밖에는 한국어(기본).
"""
from __future__ import annotations

import re

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")

TEXT: dict[str, dict[str, str]] = {
    "process_created": {
        "ko": "프로세스를 생성했어요. 확인 후 저장 버튼을 눌러주세요.",
        "en": "I've created the process. Review it, then press Save.",
    },
    "skill_created": {
        "ko": "스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.",
        "en": "I've created the skill. Review it, then press Save.",
    },
    "validation_hold": {
        "ko": "프로세스 정의가 검증을 통과하지 못해 결과 전달을 보류했어요.",
        "en": "The process definition didn't pass validation, so I'm holding the result back.",
    },
    "validation_hold_tail": {
        "ko": "어떻게 고칠지 알려주시면 이어서 수정할게요.",
        "en": "Tell me how to fix it and I'll continue from there.",
    },
    "integration_hold": {
        "ko": "프로세스 산출물을 통합하는 중 문제가 생겨 결과 전달을 보류했어요.",
        "en": "Something went wrong while putting the process files together, so I'm holding the result back.",
    },
    "retry_tail": {
        "ko": "다시 시도하거나 수정할 부분을 알려주세요.",
        "en": "Try again, or tell me what to change.",
    },
    # 에이전트 입력 끝에 붙이는 언어 지시(영어일 때만 쓴다)
    "reply_language_rule": {
        "ko": "",
        "en": "[Language]\nThe user wrote in English. Reply in English — headings, tables, questions and summaries too.",
    },
    "finalize_error": {
        "ko": "프로세스 마무리 처리 중 오류가 발생했어요. 잠시 후 다시 시도하거나 수정할 부분을 알려주세요.",
        "en": "An error occurred while finishing the process. Try again in a moment, or tell me what to change.",
    },
}

DONE_TODOS: dict[str, list[str]] = {
    "ko": [
        "1. 프로세스 초안 설계 & JSON 생성",
        "2. 스킬·에이전트·DMN 선택 & 생성",
        "3. 입력 폼 & 참조정보 생성",
        "4. 프로세스 검증 & 자동 보정",
        "5. 결과 통합 & 완료",
    ],
    "en": [
        "1. Draft the process & generate JSON",
        "2. Pick & create skills, agents, DMN",
        "3. Generate input forms & reference info",
        "4. Validate & auto-fix the process",
        "5. Assemble & finish",
    ],
}

# 이미 저장 안내가 들어 있는지(에이전트가 스스로 썼는지) — 두 언어 모두
_SAVE_MARKERS = ("확인 후 저장", "press Save")


# 프론트가 사용자 글 뒤에 붙이는 시스템 블록(한국어 지시문 포함) — 언어 판단에서 뺀다
_SYSTEM_BLOCK = re.compile(r"\n?\[(?:ExecutionPolicy|InputData|OpenUIForm)\]")


def reply_lang(text: str) -> str:
    """요청 원문의 언어 — 한글이 없고 영문자가 3자 이상이면 'en', 아니면 'ko'.
    사용자 글 뒤에 붙는 [ExecutionPolicy] 등 시스템 블록 앞까지만 본다."""
    t = _SYSTEM_BLOCK.split(str(text or ""), maxsplit=1)[0]
    if _HANGUL.search(t):
        return "ko"
    return "en" if len(_LATIN.findall(t)) >= 3 else "ko"


def t(key: str, lang: str) -> str:
    entry = TEXT[key]
    return entry.get(lang) or entry["ko"]


def done_todos(lang: str) -> list[dict]:
    return [{"content": c, "status": "completed"} for c in DONE_TODOS.get(lang) or DONE_TODOS["ko"]]


def asks_to_save(text: str) -> bool:
    s = str(text or "")
    return any(m in s for m in _SAVE_MARKERS)
