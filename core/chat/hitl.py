"""HITL (Human-in-the-Loop) tool and interrupt payload utilities."""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator


def extract_interrupt_payload(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract interrupt payload from a LangGraph execution result dict."""
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    try:
        first = interrupts[0]
        return getattr(first, "value", None) or {"raw": interrupts}
    except Exception:
        return {"raw": interrupts}


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(default="", description="사용자에게 물어볼 질문 또는 요청.")
    context: str = Field(default="", description="결정을 돕기 위한 배경 설명 또는 선택지 목록(선택).")

    @model_validator(mode="before")
    @classmethod
    def _coalesce_question_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if str(out.get("question") or "").strip():
            return out
        for key in ("text", "message", "instruction", "prompt", "query", "task", "content"):
            v = out.get(key)
            if isinstance(v, str) and v.strip():
                out["question"] = v.strip()
                return out
        return out

    @model_validator(mode="after")
    def _ensure_question(self) -> AskUserArgs:
        if not (self.question or "").strip():
            object.__setattr__(
                self,
                "question",
                "추가 확인이 필요합니다. 원하시는 작업을 구체적으로 알려주세요.",
            )
        return self


def _ask_user_impl(question: str, context: str = "") -> str:
    payload = {"question": question, "context": context}
    response = interrupt(payload)
    if isinstance(response, dict):
        return response.get("answer", str(response))
    return str(response)


ask_user = StructuredTool.from_function(
    func=_ask_user_impl,
    name="ask_user",
    description=(
        "사람 개입(HITL)이 필요할 때 호출하는 도구. "
        "명시적 인간 승인, 민감 정보 확인, 모호한 요청 명확화, 선택지 제시 등 "
        "모든 사용자 입력이 필요한 상황에서 사용한다. "
        "context에 배경 설명이나 선택지를 포함할 수 있다. "
        "내부에서 LangGraph interrupt()를 발생시켜 그래프 실행을 일시 중단한다. "
        "단, GitHub 토큰·GitHub 인증 정보는 시스템이 자동으로 관리하므로 "
        "이 도구로 요청하지 마세요."
    ),
    args_schema=AskUserArgs,
    return_direct=False,
)


def get_hitl_tools() -> list[Any]:
    return [ask_user]


# ---------------------------------------------------------------------------
# HITL 상태 유틸리티
# ---------------------------------------------------------------------------

def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split()).lower()


def is_duplicate_hitl_request(
    previous_run_state: dict,
    *,
    question: str,
    pending_field: str,
) -> bool:
    """중복된 HITL 요청인지 확인한다 (같은 필드·같은 질문이 이미 pending 상태)."""
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
