"""도구 결과 콘텐츠 위생: 모델 API 가 받지 못하는 블록을 모델 호출 직전 텍스트 안내로 바꾼다.

배경: `read_file` 은 파일 앞부분이 UTF-8 로 풀리지 않으면 내용을 base64 로 받아
`{"type": "file", ...}` 블록을 담은 ToolMessage 를 만든다. OpenAI 호환 채팅 API 는
도구 결과에서 이 블록을 받지 않아 `Invalid value: 'file'` 400 으로 턴 전체가 실패한다.
그 ToolMessage 는 대화 이력(체크포인트)에 남으므로, 같은 대화방에서는 이후 모든
모델 호출이 첫 요청부터 같은 400 으로 실패해 방을 영영 쓸 수 없게 된다.

이 미들웨어는 그래프 상태를 바꾸지 않고, 모델에 넘기는 메시지 사본에서만 허용되지 않은
블록을 "읽을 수 없는 파일이었다"는 텍스트로 바꾼다. 그래서 새 턴뿐 아니라 이미 이력이
오염된 대화방도 다음 턴부터 다시 동작한다.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

# 도구 결과에서 모델 API 가 받는 블록. 이미지는 비전 모델이 쓰므로 그대로 둔다.
_ALLOWED_BLOCK_TYPES = frozenset({"text", "image", "image_url"})


def _unreadable_notice(msg: ToolMessage, block: dict) -> str:
    extra = msg.additional_kwargs or {}
    path = extra.get("read_file_path") or "(경로 미상)"
    mime = block.get("mime_type") or extra.get("read_file_media_type") or "unknown"
    tool = msg.name or "tool"
    return (
        f"[{tool}: '{path}' 는 텍스트로 읽을 수 없는 내용({mime})이라 결과에 넣지 않았습니다. "
        "텍스트 파일이어야 한다면 동시에 편집하다 깨졌을 수 있습니다. 같은 파일을 여러 도구 호출로 "
        "동시에 고치지 말고, 원본을 다시 체크아웃하거나 셸로 인코딩을 확인한 뒤 진행하세요.]"
    )


def sanitize_tool_message(msg: Any) -> Any:
    """허용되지 않은 콘텐츠 블록을 텍스트 블록으로 바꾼 사본을 돌려준다(바꿀 게 없으면 원본)."""
    if not isinstance(msg, ToolMessage) or not isinstance(msg.content, list):
        return msg
    changed = False
    blocks: list = []
    for block in msg.content:
        if isinstance(block, dict) and block.get("type") not in _ALLOWED_BLOCK_TYPES:
            blocks.append({"type": "text", "text": _unreadable_notice(msg, block)})
            changed = True
        else:
            blocks.append(block)
    if not changed:
        return msg
    return msg.model_copy(update={"content": blocks})


class ToolContentBlockSanitizerMiddleware(AgentMiddleware):
    """모델 호출 직전, 도구 결과의 비텍스트·미지원 블록(예: `file`)을 텍스트 안내로 바꾼다."""

    def wrap_model_call(
        self,
        request: "ModelRequest",
        handler: "Callable[[ModelRequest], ModelResponse]",
    ) -> "ModelResponse | Any":
        messages = [sanitize_tool_message(m) for m in request.messages]
        return handler(dataclasses.replace(request, messages=messages))

    async def awrap_model_call(
        self,
        request: "ModelRequest",
        handler: "Callable[[ModelRequest], Any]",
    ) -> Any:
        messages = [sanitize_tool_message(m) for m in request.messages]
        return await handler(dataclasses.replace(request, messages=messages))
