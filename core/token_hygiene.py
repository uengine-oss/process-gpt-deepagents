"""토큰 위생 미들웨어: 모델 호출 직전 대용량 도구 결과를 잘라낸다."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

_DEFAULT_MAX_CHARS = 12_000
_TRUNCATION_NOTICE = "\n...[도구 결과가 너무 길어 잘렸습니다 / tool result truncated]"


def _truncate_tool_message(msg: Any, max_chars: int) -> Any:
    if not isinstance(msg, ToolMessage):
        return msg
    content = msg.content
    if isinstance(content, str) and len(content) > max_chars:
        return msg.model_copy(update={"content": content[:max_chars] + _TRUNCATION_NOTICE})
    return msg


class ToolResultTruncationMiddleware(AgentMiddleware):
    """max_chars를 초과하는 ToolMessage 내용을 모델 호출 직전에 잘라낸다.

    도구 결과가 지나치게 길면 컨텍스트 윈도우를 소비하고 모델 성능이 저하된다.
    이 미들웨어는 그래프 상태를 변경하지 않고, 모델에게 전달되는 메시지 사본만 축약한다.
    """

    def __init__(self, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
        self._max_chars = max_chars

    def wrap_model_call(
        self,
        request: "ModelRequest",
        handler: "Callable[[ModelRequest], ModelResponse]",
    ) -> "ModelResponse | Any":
        truncated = [_truncate_tool_message(m, self._max_chars) for m in request.messages]
        return handler(dataclasses.replace(request, messages=truncated))

    async def awrap_model_call(
        self,
        request: "ModelRequest",
        handler: "Callable[[ModelRequest], Any]",
    ) -> Any:
        truncated = [_truncate_tool_message(m, self._max_chars) for m in request.messages]
        return await handler(dataclasses.replace(request, messages=truncated))
