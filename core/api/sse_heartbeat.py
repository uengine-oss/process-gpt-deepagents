"""SSE 스트림에 keepalive(heartbeat)를 주입한다.

배경
----
딥 에이전트 한 턴은 LLM 이 오래 생각하거나 서브에이전트가 파일을 쓰는 동안 수 분씩
아무 이벤트도 내보내지 않는다. 그 사이 SSE 커넥션은 완전히 유휴 상태가 되는데,
중간 프록시(Cloudflare 등)는 유휴 커넥션을 100초 안팎에서 끊어버린다. 그러면
브라우저는 `net::ERR_QUIC_PROTOCOL_ERROR` 로 스트림을 잃고, 백엔드는 계속 돌고 있는데
화면은 "생각 중..." 에서 영원히 멈춘다(사용자가 새로고침해 attach 로 재접속해야만
진행 상황이 갱신된다).

`/chat/stream` 은 process-gpt-agent-sdk 가 `mount_chat_sse()` 로 마운트하는 라우트라
이 저장소에서 핸들러를 직접 고칠 수 없다. 대신 마운트된 뒤 라우트를 감싸,
StreamingResponse 의 body_iterator 에 주기적인 SSE 주석(`: ping`)을 끼워 넣는다.
SSE 명세상 `:` 로 시작하는 줄은 주석이라 클라이언트 파서(data: 만 처리)는 무시하고,
프록시 입장에서는 커넥션이 살아 있는 것으로 보인다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from starlette.responses import StreamingResponse
from starlette.routing import request_response

logger = logging.getLogger(__name__)

# SSE 주석 한 줄. 클라이언트는 무시하고 프록시의 유휴 타이머만 리셋한다.
HEARTBEAT_CHUNK = b": ping\n\n"

# 기본 15초 — 흔한 프록시 유휴 한도(30~100초)보다 넉넉히 짧게 잡는다.
DEFAULT_INTERVAL_SECONDS = 15.0


async def with_heartbeat(source: AsyncIterator[bytes], interval: float) -> AsyncIterator[bytes]:
    """원본 이터레이터를 감싸, `interval` 초 동안 청크가 없으면 heartbeat 를 내보낸다."""
    iterator = source.__aiter__()
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                # 아직 다음 청크가 없다 → 커넥션만 살려 둔다.
                yield HEARTBEAT_CHUNK
                continue
            task, pending = pending, None
            try:
                chunk = task.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if pending is not None:
            pending.cancel()


def install_sse_heartbeat(app, path: str, interval: float = DEFAULT_INTERVAL_SECONDS) -> bool:
    """이미 마운트된 `path` 라우트를 heartbeat 주입 버전으로 교체한다.

    라우트를 찾지 못하면 아무것도 하지 않고 False 를 돌려준다(기동은 계속되어야 한다).
    """
    for route in app.routes:
        if getattr(route, "path", None) != path:
            continue
        original = getattr(route, "endpoint", None)
        if original is None:
            continue

        async def handler(request, _original=original):
            response = await _original(request)
            if isinstance(response, StreamingResponse):
                response.body_iterator = with_heartbeat(response.body_iterator, interval)
            return response

        route.endpoint = handler
        # Starlette Route 는 endpoint 를 request_response() 로 감싼 app 을 실제로 호출한다.
        # endpoint 만 바꾸면 반영되지 않으므로 app 도 함께 다시 만든다.
        route.app = request_response(handler)
        logger.info("SSE heartbeat 적용: path=%s interval=%.1fs", path, interval)
        return True

    logger.warning("SSE heartbeat 미적용: 라우트를 찾지 못했습니다 path=%s", path)
    return False
