"""core/chat/sdk_registry.py — 이 저장소의 레지스트리를 SDK 에 꽂는 어댑터.

## 왜 필요한가

SDK 0.5.x 는 진행 중인 턴을 자체 레지스트리(프로세스 로컬 dict)에 기록한다.
이 저장소는 그보다 앞서 만든 Redis 기반 레지스트리를 갖고 있어서, 그대로 두면
같은 턴이 두 곳에 따로 기록된다 — SDK 가 마운트한 `/chat/stream` 은 SDK 쪽만 보고,
이 저장소의 `/chat/stream/attach` 와 `/chat/stop` 은 자기 쪽만 본다. 단일 파드에서는
양쪽 다 채워져 우연히 동작하지만, 다중 파드에서는 SDK 쪽이 파드 로컬이라 어긋난다.

여기서 SDK 의 레지스트리 인터페이스를 구현해 `core/chat/stream_registry.py` 와
`core/chat/inflight_registry.py` 로 위임한다. 이러면 기록 지점이 하나가 되고,
Redis 를 쓰는 다중 파드 동작이 SDK 라우트에도 그대로 적용된다.

## supersede 를 비워 두는 이유

새 요청이 이전 턴을 **대체**하는지 **이어가는지** 는 요청 본문을 봐야 안다.
HITL(사람 확인) 응답은 이전 턴이 남긴 interrupt 를 재개하는 것이라 취소하면 안 되는데,
그 판단(`run_state.tool_name == "request_human_input"`)은 executor 안에서만 할 수 있다.
그래서 턴 시작 시점의 정리는 SDK 에 맡기지 않고 executor 의
`supersede_previous_turn(cancel=...)` 가 계속 담당한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from processgpt_agent_sdk.chat_registry import (
    DONE_TYPES,
    ChatRunRegistry,
    InflightRegistry,
    set_inflight_registry,
    set_run_registry,
)

from core.chat import stream_registry
from core.chat.inflight_registry import get_inflight, supersede_previous_turn

logger = logging.getLogger(__name__)


class DeepAgentsRunRegistry(ChatRunRegistry):
    """SDK 의 런 레지스트리 계약을 `core/chat/stream_registry` 로 위임한다.

    SDK 는 채팅 출력 큐(`RunRecordingQueue`)를 가로채 턴이 내보내는 이벤트를
    `record()` 로 넘긴다. 그 이벤트는 예전 `MulticastStreamer` 가 보던 것과
    같은 집합이라(둘 다 `ChatStreamer.send_json` 을 거친다), 위임만 하면 기존
    attach 구독자가 지금까지와 똑같은 스트림을 받는다.
    """

    async def start_run(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        await stream_registry.start_run(conversation_id)

    async def record(self, conversation_id: str, payload: dict) -> None:
        if not conversation_id or not isinstance(payload, dict):
            return
        # append_token 은 Redis 백엔드에서 no-op 이다(XADD 가 영구 기록도 겸한다).
        # 로컬 백엔드에서만 누적 텍스트를 쌓는다.
        if payload.get("type") == "token":
            content = payload.get("content")
            if isinstance(content, str) and content:
                await stream_registry.append_token(conversation_id, content)

        await stream_registry.broadcast(conversation_id, payload)

        if payload.get("type") in DONE_TYPES:
            await stream_registry.mark_done(conversation_id)

    async def mark_done(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        await stream_registry.mark_done(conversation_id)

    async def subscribe(self, conversation_id: str):
        return await stream_registry.subscribe(conversation_id)

    async def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        await stream_registry.unsubscribe(conversation_id, q)

    async def is_active(self, conversation_id: str) -> bool:
        # subscribe 는 구독자를 등록하므로, 확인 목적이면 반드시 되돌려 놓아야 한다.
        sub = await stream_registry.subscribe(conversation_id)
        if sub is None:
            return False
        await stream_registry.unsubscribe(conversation_id, sub[0])
        return True


class DeepAgentsInflightRegistry(InflightRegistry):
    """SDK 의 실행 레지스트리 계약을 `core/chat/inflight_registry` 로 위임한다.

    턴 등록은 executor 가 `set_inflight_and_claim()` 으로 직접 한다(Redis 소유권까지
    같이 잡아야 하고, 등록 대상이 SDK 가 만든 바깥 task 가 아니라 그래프를 돌리는
    안쪽 task 라야 취소가 정확하다). 그래서 여기서는 SDK 가 넘겨주는 task 를
    기록하지 않고, 조회·취소만 기존 레지스트리로 넘긴다.
    """

    def set_inflight(self, conversation_id: str, task: asyncio.Task) -> None:
        # executor 가 자기 task 를 등록한다 — 여기서 덮어쓰면 취소 대상이 어긋난다.
        return

    def get_inflight(self, conversation_id: str) -> Any:
        entry = get_inflight(conversation_id)
        return entry[0] if entry else None

    async def supersede(self, conversation_id: str) -> bool:
        # 대체/이어가기 판단은 executor 가 한다 — 모듈 docstring 참고.
        return False

    async def cancel(self, conversation_id: str) -> bool:
        """`/chat/stop` 의 명시적 중지. 로컬/원격(Redis) 양쪽을 다룬다."""
        if not conversation_id:
            return False
        had_turn = get_inflight(conversation_id) is not None
        await supersede_previous_turn(conversation_id, cancel=True)
        return had_turn


def install() -> None:
    """프로세스 기동 시 한 번 호출해 SDK 레지스트리를 이 저장소 것으로 바꾼다."""
    set_run_registry(DeepAgentsRunRegistry())
    set_inflight_registry(DeepAgentsInflightRegistry())
    logger.info("SDK 채팅 레지스트리를 deepagents 구현으로 교체")
