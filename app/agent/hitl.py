"""LangGraph DeepAgents HITL 도구 및 interrupt 페이로드 유틸리티.

- `request_human_input`:
    LangChain `StructuredTool` 로 선언된 사람 개입(HITL) 도구.
    내부에서 `langgraph.types.interrupt` 를 호출해 그래프 실행을 중단시키고,
    payload(`{"question": ..., "context": ...}`)를 상태에 남긴다.

- `extract_interrupt_payload`:
    DeepAgents / LangGraph 실행 결과 dict 에서 `__interrupt__` 필드를 찾아
    첫 번째 interrupt 의 `.value` 를 꺼낸다.

이 모듈은 DeepAgents 그래프 내부에서 HITL 을 트리거하는 역할만 담당하고,
실제 `human_asked` / `human_response` 이벤트 발행 및 재개(resume) 로직은
호출 측(executor)에서 구현한다.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator


def extract_interrupt_payload(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """DeepAgents / LangGraph 실행 결과에서 interrupt payload 를 추출한다.

    결과 dict 에 `__interrupt__` 키가 없으면 None 을 반환한다.
    값이 리스트인 경우 첫 번째 요소의 `.value` 를 사용하고,
    예외가 발생하면 원시 interrupt 값을 `"raw"` 키로 감싼 dict 를 반환한다.
    """
    if not isinstance(result, dict):
        return None

    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None

    try:
        # LangGraph 는 보통 Interrupt 객체 리스트를 반환한다.
        first = interrupts[0]
        return getattr(first, "value", None) or {"raw": interrupts}
    except Exception:
        return {"raw": interrupts}


class RequestHumanInputArgs(BaseModel):
    """LLM 이 `question` 대신 `text`/`message` 등으로 잘못 호출하는 경우를 흡수한다."""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(default="", description="사람에게 물어볼 구체적인 질문 또는 요청.")
    context: str = Field(default="", description="결정을 돕기 위한 배경 설명(선택).")
    openui_lang: str = Field(
        default="",
        description="채팅 UI가 렌더링할 OpenUI Lang(선택).",
    )

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
    def _ensure_question(self) -> RequestHumanInputArgs:
        if not (self.question or "").strip():
            object.__setattr__(
                self,
                "question",
                "추가 확인이 필요합니다. 원하시는 작업을 구체적으로 알려주세요.",
            )
        return self


def _request_human_input_impl(question: str, context: str = "", openui_lang: str = "") -> str:
    """사람 개입(HITL)이 필요할 때 호출하는 도구.

    이 도구는 아래 상황에서 **의도적으로 호출**되어야 한다.

    - 승인/결재/법무·보안 검토 등 **명시적 인간 승인**이 필요한 작업
    - 개인정보·자격증명·권한 등 **민감 정보**가 포함되거나 노출 위험이 있는 작업
    - 요청이 **모호**해서 여러 해석이 가능하고, 잘못 진행하면 비용/리스크가 큰 작업
    - 외부에서만 알 수 있는 값(계정/환경/정책/내부 기준 등)이 필요해 도구로는 확정 불가한 작업

    단, 테넌트 컨텍스트 도구(mem0/memento/dmn_rule 등)가 구성되어 있고 “즉시 승인/민감/모호 확인”이
    아닌 단순 정보 누락에 가까우면, 가능할 때 먼저 한 번 시도해본 뒤 HITL을 요청하는 편이 좋다.

    내부에서 LangGraph `interrupt({...})` 를 발생시켜 그래프 실행을 일시 중단하고,
    외부 오케스트레이터가 `human_asked` 이벤트를 기록하도록 한다.

    Args:
        question: 사람에게 물어볼 구체적인 질문 또는 요청.
        context: 결정을 돕기 위한 배경 설명(선택).
        openui_lang: 채팅 UI가 렌더링할 OpenUI Lang(선택). 제공 시 **OpenUI Lang만** 포함하고,
            ``Card``/``CardHeader``/``Form``/``Input``/``Textarea``/``Select``/``Radio``/``Checkbox``는
            **위치 인자** 호출만 사용한다(예: ``CardHeader("제목", "부제")``; ``title=``·``name=`` 금지).
    """
    payload = {"question": question, "context": context}
    if isinstance(openui_lang, str) and openui_lang.strip():
        payload["openui_lang"] = openui_lang.strip()
    response = interrupt(payload)
    if isinstance(response, dict):
        # 재개 시에는 dict 형태로 응답이 들어올 수 있으며,
        # 그 중 `answer` 필드를 우선 사용한다.
        return response.get("answer", str(response))
    return str(response)


_REQUEST_HUMAN_INPUT_DESCRIPTION = """사람 개입(HITL)이 필요할 때 호출하는 도구.

이 도구는 아래 상황에서 **의도적으로 호출**되어야 한다.

- 승인/결재/법무·보안 검토 등 **명시적 인간 승인**이 필요한 작업
- 개인정보·자격증명·권한 등 **민감 정보**가 포함되거나 노출 위험이 있는 작업
- 요청이 **모호**해서 여러 해석이 가능하고, 잘못 진행하면 비용/리스크가 큰 작업
- 외부에서만 알 수 있는 값(계정/환경/정책/내부 기준 등)이 필요해 도구로는 확정 불가한 작업

단, 테넌트 컨텍스트 도구(mem0/memento/dmn_rule 등)가 구성되어 있고 “즉시 승인/민감/모호 확인”이
아닌 단순 정보 누락에 가까우면, 가능할 때 먼저 한 번 시도해본 뒤 HITL을 요청하는 편이 좋다.

내부에서 LangGraph `interrupt({...})` 를 발생시켜 그래프 실행을 일시 중단하고,
외부 오케스트레이터가 `human_asked` 이벤트를 기록하도록 한다.

Args:
    question: 사람에게 물어볼 구체적인 질문 또는 요청.
    context: 결정을 돕기 위한 배경 설명(선택).
    openui_lang: 채팅 UI가 렌더링할 OpenUI Lang(선택). 제공 시 **OpenUI Lang만** 포함하고,
        ``Card``/``CardHeader``/``Form``/``Input``/``Textarea``/``Select``/``Radio``/``Checkbox``는
        **위치 인자** 호출만 사용한다(예: ``CardHeader("제목", "부제")``; ``title=``·``name=`` 금지).
"""


request_human_input = StructuredTool.from_function(
    func=_request_human_input_impl,
    name="request_human_input",
    description=_REQUEST_HUMAN_INPUT_DESCRIPTION,
    args_schema=RequestHumanInputArgs,
    return_direct=False,
)


def get_hitl_tools() -> list[Any]:
    """DeepAgents 그래프에 주입할 HITL 도구 목록을 반환한다."""
    return [request_human_input]
