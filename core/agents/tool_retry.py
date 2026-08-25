"""HITL interrupt 를 삼키지 않는 도구 재시도 미들웨어."""

from __future__ import annotations

from langchain.agents.middleware import ToolRetryMiddleware
from langgraph.errors import GraphBubbleUp


def _is_retryable_tool_error(exc: Exception) -> bool:
    """LangGraph 제어 흐름 예외는 "도구 실패"가 아니므로 재시도 대상에서 뺀다.

    `request_human_input`(HITL)은 내부에서 `interrupt()` 를 호출하는데, 이는 정상 반환이
    아니라 `GraphInterrupt`(= `GraphBubbleUp` 하위) 예외를 던져 그래프를 멈추는 방식이다.
    `ParentCommand`(서브그래프 → 부모 Command 전달)도 마찬가지다. 이 예외들이 재시도
    대상이 되면 도구가 한 번 더 호출되는 부작용까지 생긴다.
    """
    return not isinstance(exc, GraphBubbleUp)


class ControlFlowSafeToolRetryMiddleware(ToolRetryMiddleware):
    """`GraphInterrupt` 등 LangGraph 제어 흐름 예외를 삼키지 않는 ToolRetryMiddleware.

    상위 클래스는 `except Exception` 으로 도구 예외를 잡아 `on_failure="continue"` 일 때
    에러 ToolMessage 로 바꿔 모델에 돌려준다. `interrupt()` 가 던지는 `GraphInterrupt` 도
    Exception 이라 그대로 삼켜졌고, 그 결과 그래프가 멈추지 않고 끝까지 실행돼
    `__interrupt__` 체크포인트가 남지 않았다 → 사용자가 HITL 답변을 보내도
    `Command(resume=...)` 가 재개할 대상을 찾지 못해 빈 응답으로 턴이 끝났다.
    (`retry_on` 으로 재시도는 막히지만 `_handle_failure` 가 여전히 삼키므로,
    여기서 다시 던져 LangGraph 런타임까지 올려보낸다.)
    """

    def _handle_failure(self, tool_name, tool_call_id, exc, attempts_made):
        if isinstance(exc, GraphBubbleUp):
            raise exc
        return super()._handle_failure(tool_name, tool_call_id, exc, attempts_made)


def build_tool_retry_middleware() -> ToolRetryMiddleware:
    """MCP 도구 호출 실패(ToolException 등) 시 예외로 실행을 중단시키는 대신,
    에러 내용을 ToolMessage 로 모델에 돌려줘 스스로 인자를 보정해 재시도하게 한다.
    (odoo-mcp aggregate_records/search_records 의 domain·datetime 형식 오류 등)
    단, HITL interrupt 같은 LangGraph 제어 흐름 예외는 그대로 통과시킨다.
    """
    return ControlFlowSafeToolRetryMiddleware(
        max_retries=1,
        retry_on=_is_retryable_tool_error,
        on_failure="continue",
    )
