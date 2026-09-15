"""core/chat/event_tap.py — 채팅 이벤트를 흘려보내면서 곁가지로 한 번 더 보는 프록시.

executor.py 가 SDK 에게 받은 streamer 를 감싸, 각 이벤트를 (a) 원래 streamer 로
그대로 전달하고 (b) 콜백에 넘긴다. 콜백은 tool/plan 이벤트를 채팅방 컨텍스트로
증분 저장하는 데 쓴다(core/chat/chat_room.py).

재접속 스냅샷을 위한 기록은 여기서 하지 않는다 — SDK 가 채팅 출력 큐
(`RunRecordingQueue`)를 직접 가로채 같은 이벤트를 레지스트리에 남긴다. 여기서 또
기록하면 같은 토큰이 두 번 쌓여 스냅샷 본문이 두 배가 된다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class EventTapStreamer:
    """원래 streamer 를 감싸는 얇은 프록시."""

    def __init__(
        self,
        inner: Any,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._inner = inner
        self._on_event = on_event

    async def send_text(self, text: str) -> None:
        await self.send_json({"type": "token", "content": text})

    async def send_json(self, data: Any) -> None:
        if self._inner is not None:
            await self._inner.send_json(data)

        if self._on_event is None or not isinstance(data, dict):
            return
        try:
            await self._on_event(data)
        except Exception:
            # 곁가지 저장이 실패했다고 사용자에게 가는 스트림을 끊지 않는다.
            logger.warning("EventTapStreamer: on_event 처리 실패", exc_info=True)
