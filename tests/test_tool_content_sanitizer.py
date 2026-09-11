"""모델 호출 직전 도구 결과의 미지원 콘텐츠 블록을 텍스트로 바꾸는지 검증한다.

배경: read_file 이 UTF-8 로 풀리지 않는 파일을 `{"type": "file"}` 블록으로 돌려주면
OpenAI 호환 API 가 `Invalid value: 'file'` 400 을 낸다. 그 메시지가 대화 이력에 남아
같은 대화방의 이후 모든 호출이 실패했다.
"""

from __future__ import annotations

import dataclasses

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.llm.tool_content_sanitizer import (
    ToolContentBlockSanitizerMiddleware,
    sanitize_tool_message,
)


@dataclasses.dataclass
class _Request:
    messages: list


def _file_read_message() -> ToolMessage:
    return ToolMessage(
        content_blocks=[{"type": "file", "base64": "7ZWc6riA", "mime_type": "text/markdown"}],
        name="read_file",
        tool_call_id="call_1",
        additional_kwargs={
            "read_file_path": "/workspace/.checkouts/room-1/skill/SKILL.md",
            "read_file_media_type": "text/markdown",
        },
    )


def _block_types(msg: ToolMessage) -> list[str]:
    return [b.get("type") if isinstance(b, dict) else "str" for b in msg.content]


def test_file_block_becomes_text_notice_with_path():
    cleaned = sanitize_tool_message(_file_read_message())

    assert _block_types(cleaned) == ["text"]
    notice = cleaned.content[0]["text"]
    assert "/workspace/.checkouts/room-1/skill/SKILL.md" in notice
    assert "text/markdown" in notice
    assert cleaned.tool_call_id == "call_1"
    assert cleaned.name == "read_file"


def test_original_message_in_state_is_not_mutated():
    original = _file_read_message()
    sanitize_tool_message(original)
    assert _block_types(original) == ["file"]


def test_text_and_image_blocks_are_kept():
    msg = ToolMessage(
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image", "base64": "iVBORw0KGgo=", "mime_type": "image/png"},
        ],
        tool_call_id="call_2",
    )
    assert sanitize_tool_message(msg) is msg


def test_plain_string_and_non_tool_messages_are_untouched():
    text_tool = ToolMessage(content="ok", tool_call_id="call_3")
    human = HumanMessage(content="이어서 해줘")
    ai = AIMessage(content="진행합니다")
    for msg in (text_tool, human, ai):
        assert sanitize_tool_message(msg) is msg


def test_middleware_passes_cleaned_copy_to_model_for_poisoned_history():
    """이미 이력에 `file` 블록이 남은 대화방도 다음 모델 호출에서는 400 없이 넘어간다."""
    history = [HumanMessage(content="스킬 고쳐줘"), _file_read_message(), HumanMessage(content="이어서 해줘")]
    seen: list = []

    def handler(request):
        seen.extend(request.messages)
        return "response"

    result = ToolContentBlockSanitizerMiddleware().wrap_model_call(_Request(messages=history), handler)

    assert result == "response"
    tool_msgs = [m for m in seen if isinstance(m, ToolMessage)]
    assert tool_msgs and _block_types(tool_msgs[0]) == ["text"]
    assert _block_types(history[1]) == ["file"], "그래프 상태의 원본 메시지는 그대로여야 한다"


async def test_async_middleware_path():
    seen: list = []

    async def handler(request):
        seen.extend(request.messages)
        return "response"

    await ToolContentBlockSanitizerMiddleware().awrap_model_call(_Request(messages=[_file_read_message()]), handler)

    assert _block_types(seen[0]) == ["text"]
