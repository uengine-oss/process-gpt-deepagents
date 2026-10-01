"""오류로 끝난 채팅 턴도 chats 에 남는가.

오류를 SSE 로만 보내던 동안은 화면에만 보이고 DB 에는 아무 것도 없었다. 그래서
새로고침하거나 방에 다시 들어오면 질문만 있고 답은 오지 않은 것처럼 보였다.
여기서 지키는 것은 "오류도 assistant 메시지로 남는다" 다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor as ex  # noqa: E402
from executor import _chat_error_display_text, _persist_chat_error_message  # noqa: E402


class FakeReq:
    """ChatRequestContext.req 가 들고 있는 것 중 저장 경로가 쓰는 것만."""

    def __init__(self) -> None:
        self.conversation_id = "room-1"
        self.tenant_id = "t1"
        self.response_message_uuid = "assistant-uuid-1"


class FakeChatContext:
    task_id = "task-1"
    context_id = "room-1"

    def __init__(self) -> None:
        self.req = FakeReq()


class FakeProcessContext:
    """프로세스/워크리스트 경로 — req 가 없다."""

    task_id = "task-1"
    context_id = "inst-1"


def _capture(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def _fake_persist(req, event, text):
        calls.append((req, event, text))

    monkeypatch.setattr(ex, "persist_chat_to_db", _fake_persist)
    return calls


def test_오류도_assistant_메시지로_저장된다(monkeypatch):
    calls = _capture(monkeypatch)
    ctx = FakeChatContext()

    asyncio.run(
        _persist_chat_error_message(
            ctx,
            task_id="task-1",
            context_id="room-1",
            error_text="RuntimeError: boom",
            partial_text="",
        )
    )

    assert len(calls) == 1
    req, event, text = calls[0]
    # 프론트가 보낸 uuid 로 upsert 되도록 요청 객체를 그대로 넘긴다.
    assert req is ctx.req
    assert text == "⚠️ 오류가 발생했습니다: RuntimeError: boom"
    # 최종 아티팩트와 같은 모양이어야 저장 payload(role/본문)가 성립한다.
    assert event.last_chunk is True
    meta = dict(event.metadata)
    assert meta["role"] == "assistant"
    assert meta["isError"] is True
    assert meta["error"] == "RuntimeError: boom"


def test_부분_응답은_살려서_남긴다(monkeypatch):
    calls = _capture(monkeypatch)

    asyncio.run(
        _persist_chat_error_message(
            FakeChatContext(),
            task_id="task-1",
            context_id="room-1",
            error_text="RuntimeError: boom",
            partial_text="  여기까지는 답했다  ",
        )
    )

    _req, _event, text = calls[0]
    assert text == "여기까지는 답했다\n\n⚠️ 오류가 발생했습니다: RuntimeError: boom"


def test_프로세스_경로는_저장하지_않는다(monkeypatch):
    calls = _capture(monkeypatch)

    asyncio.run(
        _persist_chat_error_message(
            FakeProcessContext(),
            task_id="task-1",
            context_id="inst-1",
            error_text="RuntimeError: boom",
        )
    )

    assert calls == []


def test_오류_본문은_빈_메시지가_되지_않는다():
    # 예외 문구가 비어도 사용자에게는 뭔가 보여야 한다.
    assert _chat_error_display_text("") == "⚠️ 오류가 발생했습니다: 에이전트 응답 오류"
