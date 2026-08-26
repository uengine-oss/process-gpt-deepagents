"""SSE heartbeat 주입 테스트.

딥 에이전트 한 턴은 수 분씩 이벤트를 내보내지 않을 수 있다. 그 사이 프록시가 유휴
커넥션을 끊으면 화면이 "생각 중..." 에서 멈춘다. heartbeat 는 그 공백을 SSE 주석으로
메워야 하고, 실제 데이터 청크와 종료 시점은 그대로 통과시켜야 한다.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.api.sse_heartbeat import HEARTBEAT_CHUNK, install_sse_heartbeat, with_heartbeat


async def _collect(source, interval):
    return [chunk async for chunk in with_heartbeat(source, interval)]


@pytest.mark.asyncio
async def test_passes_chunks_through_without_delay():
    async def source():
        yield b"a"
        yield b"b"

    assert await _collect(source(), 10.0) == [b"a", b"b"]


@pytest.mark.asyncio
async def test_emits_heartbeat_while_source_is_idle():
    async def source():
        yield b"first"
        await asyncio.sleep(0.25)  # interval(0.05s) 보다 훨씬 긴 유휴 구간
        yield b"last"

    chunks = await _collect(source(), 0.05)
    assert chunks[0] == b"first"
    assert chunks[-1] == b"last"
    # 유휴 구간이 heartbeat 로 메워져야 한다.
    assert HEARTBEAT_CHUNK in chunks
    assert chunks.count(HEARTBEAT_CHUNK) >= 2


@pytest.mark.asyncio
async def test_heartbeat_is_an_sse_comment():
    # 클라이언트 파서는 'data:' 로 시작하는 줄만 처리하므로 heartbeat 는 주석이어야 한다.
    text = HEARTBEAT_CHUNK.decode()
    assert text.startswith(":")
    assert not any(line.startswith("data:") for line in text.splitlines())
    assert text.endswith("\n\n")


@pytest.mark.asyncio
async def test_stops_when_source_ends_immediately():
    async def source():
        return
        yield b""  # pragma: no cover

    assert await _collect(source(), 0.01) == []


def test_install_wraps_existing_streaming_route():
    async def endpoint(_request):
        async def body():
            yield b"data: hi\n\n"
            await asyncio.sleep(0.2)
            yield b"data: bye\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/chat/stream", endpoint, methods=["GET"])])
    assert install_sse_heartbeat(app, "/chat/stream", interval=0.05) is True

    with TestClient(app) as client:
        body = client.get("/chat/stream").text

    assert "data: hi" in body
    assert "data: bye" in body
    assert ": ping" in body


def test_install_is_noop_when_route_missing():
    app = Starlette(routes=[])
    assert install_sse_heartbeat(app, "/chat/stream") is False
