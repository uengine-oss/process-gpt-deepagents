"""채팅 턴의 진행 중인 SSE 토큰 스트림을 conversation_id(=chat_room_id) 기준으로
등록해두고, 재접속(attach)한 클라이언트가 누적 텍스트 스냅샷을 받은 뒤 이후
토큰을 실시간으로 이어받을 수 있게 하는 pub/sub 레지스트리.

processgpt_agent_sdk 는 요청마다 새 asyncio.Queue/실행 task 를 만들 뿐 이런 재접속
개념이 없다. 이 모듈은 SDK 소스를 건드리지 않고, executor.py 가 받은 streamer 를
감싸는 방식으로 그 위에 얹는다.

REDIS_URL 이 설정돼 있으면 Redis Streams(`chat:stream:{cid}`)를 백엔드로 써서
어느 파드가 send_json 을 호출했든, 어느 파드가 attach 요청을 받았든 정상 동작한다
(다중 파드 배포 지원). REDIS_URL 이 없으면 기존과 동일한 프로세스 로컬 dict 로
폴백한다(단일 uvicorn 워커 전제, 로컬 개발 환경 변경 없음).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.storage.redis_client import get_redis

logger = logging.getLogger(__name__)

_GRACE_PERIOD_SECONDS = 30.0
_MAX_RUN_AGE_SECONDS = 30 * 60  # 완료 신호 없이 방치된 run 에 대한 TTL 안전장치
_DONE_TYPES = ("done", "error")


# ------------------------------------------------------------------
# 로컬 폴백(REDIS_URL 미설정 시) — 기존 프로세스-로컬 구현 그대로.
# ------------------------------------------------------------------

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


async def _local_start_run(conversation_id: str) -> None:
    async with _lock_for(conversation_id):
        _sweep_locked()
        _registry[conversation_id] = ActiveRun(conversation_id=conversation_id)


async def _local_append_token(conversation_id: str, text: str) -> None:
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None or run.done:
            return
        run.accumulated_text += text


async def _local_broadcast(conversation_id: str, payload: dict) -> None:
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        for q in list(run.subscribers):
            q.put_nowait(payload)


async def _local_mark_done(conversation_id: str) -> None:
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        run.done = True
        run.done_at = time.monotonic()


async def _local_subscribe(conversation_id: str) -> tuple[asyncio.Queue, str] | None:
    async with _lock_for(conversation_id):
        _sweep_locked()
        run = _registry.get(conversation_id)
        if run is None or run.done:
            return None
        q: asyncio.Queue = asyncio.Queue()
        run.subscribers.append(q)
        return q, run.accumulated_text


async def _local_unsubscribe(conversation_id: str, q: asyncio.Queue) -> None:
    async with _lock_for(conversation_id):
        run = _registry.get(conversation_id)
        if run is None:
            return
        try:
            run.subscribers.remove(q)
        except ValueError:
            pass


# ------------------------------------------------------------------
# Redis Streams 백엔드(REDIS_URL 설정 시) — 다중 파드 공유.
# ------------------------------------------------------------------

def _stream_key(conversation_id: str) -> str:
    return f"chat:stream:{conversation_id}"


def _done_key(conversation_id: str) -> str:
    return f"chat:stream:{conversation_id}:done"


# subscribe() 로 만든 로컬 asyncio.Queue 를 Redis 스트림에 이어붙이는 브리지 task.
# unsubscribe() 에서 취소할 수 있도록 큐 identity 로 보관한다.
_bridge_tasks: dict[int, asyncio.Task] = {}


async def _redis_start_run(conversation_id: str) -> None:
    """새 턴 시작 — 로컬 폴백의 `_registry[cid] = ActiveRun(...)`(완전 교체)과 동일하게,
    같은 conversation_id 에 남아있던 이전 run의 스트림을 지우고 새로 시작한다.
    지우지 않으면 이전 턴들의 누적 텍스트가 스냅샷에 계속 붙어 남는다."""
    r = get_redis()
    key = _stream_key(conversation_id)
    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.delete(_done_key(conversation_id))
            pipe.xadd(key, {"data": json.dumps({"type": "_start"})})
            pipe.expire(key, _MAX_RUN_AGE_SECONDS)
            await pipe.execute()
    except Exception:
        logger.warning("stream_registry(redis): start_run 실패 | cid=%s", conversation_id, exc_info=True)


async def _redis_broadcast(conversation_id: str, payload: dict) -> None:
    """Redis Stream 은 XADD 한 번이 "영구 기록"과 "실시간 구독자에게 전달"을 동시에 한다
    (append-only log 이므로 append_token 과 broadcast 를 분리할 수 없음). 그래서 실제
    XADD 는 여기(broadcast)에서만 하고, _redis_append_token 은 no-op 이다 — send_json 이
    token 타입에 대해 append_token 다음 broadcast 를 호출해도 기록이 중복되지 않는다."""
    r = get_redis()
    try:
        await r.xadd(_stream_key(conversation_id), {"data": json.dumps(payload, ensure_ascii=False)})
    except Exception:
        logger.warning("stream_registry(redis): broadcast 실패 | cid=%s", conversation_id, exc_info=True)


async def _redis_mark_done(conversation_id: str) -> None:
    r = get_redis()
    grace = int(_GRACE_PERIOD_SECONDS)
    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.set(_done_key(conversation_id), "1", ex=grace)
            pipe.expire(_stream_key(conversation_id), grace)
            await pipe.execute()
    except Exception:
        logger.warning("stream_registry(redis): mark_done 실패 | cid=%s", conversation_id, exc_info=True)


async def _bridge_redis_to_queue(conversation_id: str, last_id: str, queue: asyncio.Queue) -> None:
    r = get_redis()
    key = _stream_key(conversation_id)
    try:
        while True:
            resp = await r.xread({key: last_id}, block=5000, count=50)
            if not resp:
                continue
            for _key, entries in resp:
                for entry_id, fields in entries:
                    last_id = entry_id
                    raw = fields.get("data") if isinstance(fields, dict) else None
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(payload, dict) or payload.get("type") == "_start":
                        continue
                    queue.put_nowait(payload)
                    if payload.get("type") in _DONE_TYPES:
                        return
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("stream_registry(redis): bridge 종료(오류) | cid=%s", conversation_id, exc_info=True)


async def _redis_subscribe(conversation_id: str) -> tuple[asyncio.Queue, str] | None:
    r = get_redis()
    try:
        if await r.exists(_done_key(conversation_id)):
            return None
        entries = await r.xrange(_stream_key(conversation_id), "-", "+")
    except Exception:
        logger.warning("stream_registry(redis): subscribe 조회 실패 | cid=%s", conversation_id, exc_info=True)
        return None
    if not entries:
        return None

    snapshot_parts: list[str] = []
    last_id = entries[-1][0]
    for _entry_id, fields in entries:
        raw = fields.get("data") if isinstance(fields, dict) else None
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "token":
            content = payload.get("content")
            if isinstance(content, str):
                snapshot_parts.append(content)

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_bridge_redis_to_queue(conversation_id, last_id, queue))
    _bridge_tasks[id(queue)] = task
    return queue, "".join(snapshot_parts)


async def _redis_unsubscribe(_conversation_id: str, q: asyncio.Queue) -> None:
    task = _bridge_tasks.pop(id(q), None)
    if task is not None:
        task.cancel()


# ------------------------------------------------------------------
# 공개 API — 호출부(executor.py, core/api/chat_stream_attach.py)는 백엔드와 무관하게
# 이 함수들만 사용한다.
# ------------------------------------------------------------------

async def start_run(conversation_id: str) -> None:
    """새 채팅 턴 시작 시 호출. 같은 conversation_id 에 남아있던 이전 run은 교체된다."""
    if not conversation_id:
        raise ValueError("conversation_id is required")
    if get_redis() is not None:
        await _redis_start_run(conversation_id)
    else:
        await _local_start_run(conversation_id)


async def append_token(conversation_id: str, text: str) -> None:
    if not conversation_id or not text:
        return
    if get_redis() is not None:
        return  # 로컬 전용 no-op: Redis 는 broadcast()의 XADD 가 영구 기록도 겸한다.
    await _local_append_token(conversation_id, text)


async def broadcast(conversation_id: str, payload: dict) -> None:
    if not conversation_id:
        return
    if get_redis() is not None:
        await _redis_broadcast(conversation_id, payload)
    else:
        await _local_broadcast(conversation_id, payload)


async def mark_done(conversation_id: str) -> None:
    if not conversation_id:
        return
    if get_redis() is not None:
        await _redis_mark_done(conversation_id)
    else:
        await _local_mark_done(conversation_id)


async def subscribe(conversation_id: str) -> tuple[asyncio.Queue, str] | None:
    """활성 run에 재접속. 없거나 이미 종료됐으면 None(활성 스트림 없음)을 반환한다.

    반환되는 두 번째 값은 지금까지 누적된 텍스트 스냅샷이다.
    """
    if not conversation_id:
        return None
    if get_redis() is not None:
        return await _redis_subscribe(conversation_id)
    return await _local_subscribe(conversation_id)


async def unsubscribe(conversation_id: str, q: asyncio.Queue) -> None:
    if not conversation_id:
        return
    if get_redis() is not None:
        await _redis_unsubscribe(conversation_id, q)
    else:
        await _local_unsubscribe(conversation_id, q)


class MulticastStreamer:
    """executor.py 가 받은 원래 streamer(SDK ChatStreamer)를 감싸는 프록시.

    각 send_json 호출을 (a) 원래 streamer로 그대로 전달 (b) 레지스트리(로컬 또는 Redis)에
    이벤트 기록/브로드캐스트 (c) done/error 타입이면 run 종료 표시, 순서로 처리한다.
    on_event 콜백은 tool/plan 이벤트를 채팅방 컨텍스트 증분 저장 등 호출자(executor.py)가
    원하는 후속 처리로 넘기기 위한 훅이다.
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
