"""채팅 턴의 진행 중인 SSE 토큰 스트림을 conversation_id(=chat_room_id) 기준으로
프로세스 메모리에 등록해두고, 재접속(attach)한 클라이언트가 누적 텍스트 스냅샷을
받은 뒤 이후 토큰을 실시간으로 이어받을 수 있게 하는 pub/sub 레지스트리.

processgpt_agent_sdk 는 요청마다 새 asyncio.Queue/실행 task 를 만들 뿐 이런 재접속
개념이 없다. 이 모듈은 SDK 소스를 건드리지 않고, executor.py 가 받은 streamer 를
감싸는 방식으로 그 위에 얹는다 — 단일 uvicorn 워커 배포를 전제로 한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_GRACE_PERIOD_SECONDS = 30.0
_MAX_RUN_AGE_SECONDS = 30 * 60  # 완료 신호 없이 방치된 run 에 대한 TTL 안전장치
_DONE_TYPES = ("done", "error")


@dataclass
class ActiveRun:
    conversation_id: str
    accumulated_text: str = ""
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    done: bool = False
    created_at: float = field(default_factory=time.monotonic)
    done_at: float | None = None


_registry: dict[str, ActiveRun] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(conversation_id: str) -> asyncio.Lock:
    # 동기 함수(await 지점 없음) — asyncio 협조적 스케줄링 하에서 이 함수 실행 도중에는
    # 다른 코루틴이 끼어들 수 없으므로 dict 조회+생성이 원자적으로 처리된다.
    return _locks.setdefault(conversation_id, asyncio.Lock())


def _sweep_locked() -> None:
    now = time.monotonic()
    stale: list[str] = []
    for cid, run in _registry.items():
        if run.done and run.done_at is not None and (now - run.done_at) > _GRACE_PERIOD_SECONDS:
            stale.append(cid)
        elif not run.done and (now - run.created_at) > _MAX_RUN_AGE_SECONDS:
            stale.append(cid)
    for cid in stale:
        _registry.pop(cid, None)
        _locks.pop(cid, None)


async def start_run(conversation_id: str) -> ActiveRun:
    """새 채팅 턴 시작 시 호출. 같은 conversation_id 에 남아있던 이전 run은 교체된다."""
    if not conversation_id:
        raise ValueError("conversation_id is required")
    async with _lock_for(conversation_id):
        _sweep_locked()
        run = ActiveRun(conversation_id=conversation_id)
        _registry[conversation_id] = run
        return run


async def append_token(conversation_id: str, text: str) -> None:
    if not conversation_id or not text:
        return
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None or run.done:
            return
        run.accumulated_text += text


async def broadcast(conversation_id: str, payload: dict) -> None:
    if not conversation_id:
        return
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        for q in list(run.subscribers):
            q.put_nowait(payload)


async def subscribe(conversation_id: str) -> tuple[asyncio.Queue, str] | None:
    """활성 run에 재접속. 없거나 이미 종료됐으면 None(활성 스트림 없음)을 반환한다.

    반환되는 두 번째 값은 지금까지 누적된 텍스트 스냅샷이다.
    """
    if not conversation_id:
        return None
    async with _lock_for(conversation_id):
        _sweep_locked()
        run = _registry.get(conversation_id)
        if run is None or run.done:
            return None
        q: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(q)
        return q, run.accumulated_text


async def unsubscribe(conversation_id: str, q: asyncio.Queue) -> None:
    if not conversation_id:
        return
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        try:
            run.subscribers.remove(q)
        except ValueError:
            pass


async def mark_done(conversation_id: str) -> None:
    if not conversation_id:
        return
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        run.done = True
        run.done_at = time.monotonic()


class MulticastStreamer:
    """executor.py 가 받은 원래 streamer(SDK ChatStreamer)를 감싸는 프록시.

    각 send_json 호출을 (a) 원래 streamer로 그대로 전달 (b) 토큰 텍스트는 레지스트리에
    누적 (c) 현재 attach 구독자들에게 broadcast (d) done/error 타입이면 run 종료 표시,
    순서로 처리한다. on_event 콜백은 tool/plan 이벤트를 채팅방 컨텍스트 증분 저장 등
    호출자(executor.py)가 원하는 후속 처리로 넘기기 위한 훅이다.
    """

    def __init__(
        self,
        inner: Any,
        conversation_id: str,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._inner = inner
        self._conversation_id = conversation_id
        self._on_event = on_event

    async def send_text(self, text: str) -> None:
        await self.send_json({"type": "token", "content": text})

    async def send_json(self, data: Any) -> None:
        if self._inner is not None:
            await self._inner.send_json(data)

        cid = self._conversation_id
        if not cid or not isinstance(data, dict):
            return

        try:
            if data.get("type") == "token":
                content = data.get("content")
                if isinstance(content, str) and content:
                    await append_token(cid, content)

            await broadcast(cid, data)

            if data.get("type") in _DONE_TYPES:
                await mark_done(cid)

            if self._on_event is not None:
                await self._on_event(data)
        except Exception:
            logger.warning("stream_registry: send_json 후처리 실패 | conversation_id=%s", cid, exc_info=True)


def format_sse_message(payload: dict) -> bytes:
    """processgpt_agent_sdk.chat_mode 의 SSE 포맷(event: message)과 동일한 형식을
    SDK 소스에 의존하지 않고 재현한다."""
    data = json.dumps(payload or {}, ensure_ascii=False)
    lines = data.splitlines() or [""]
    data_block = "".join(f"data: {ln}\n" for ln in lines)
    return f"event: message\n{data_block}\n".encode("utf-8")
