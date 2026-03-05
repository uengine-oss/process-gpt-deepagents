"""LangGraph DeepAgents HITL 도구 및 interrupt 페이로드 유틸리티.

- `request_human_input`:
    LangChain `@tool` 로 선언된 사람 개입(HITL) 도구.
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

from langchain.tools import tool
from langgraph.types import interrupt


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


@tool
def request_human_input(question: str, context: str = "") -> str:
    """사람 개입(HITL)이 필요할 때 호출하는 도구.

    현재 작업이 사람의 검토/승인/추가 정보를 필요로 하는 시점에 이 도구를 호출한다.
    내부에서 LangGraph `interrupt({...})` 를 발생시켜 그래프 실행을 일시 중단하고,
    외부 오케스트레이터가 `human_asked` 이벤트를 기록하도록 한다.

    Args:
        question: 사람에게 물어볼 구체적인 질문 또는 요청.
        context: 결정을 돕기 위한 배경 설명(선택).
    """
    response = interrupt({"question": question, "context": context})
    if isinstance(response, dict):
        # 재개 시에는 dict 형태로 응답이 들어올 수 있으며,
        # 그 중 `answer` 필드를 우선 사용한다.
        return response.get("answer", str(response))
    return str(response)


def get_hitl_tools() -> list[Any]:
    """DeepAgents 그래프에 주입할 HITL 도구 목록을 반환한다."""
    return [request_human_input]

