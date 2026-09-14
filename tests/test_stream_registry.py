"""Unit tests for core/chat/stream_registry.py — chat SSE reconnect (attach) registry."""

import asyncio

import pytest

from core.chat.stream_registry import (
    EventTapStreamer,
    append_token,
    broadcast,
    mark_done,
    start_run,
    subscribe,
    unsubscribe,
)


class _FakeStreamer:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, data):
        self.sent.append(data)


async def test_attach_with_no_active_run_returns_none():
    assert await subscribe("room-does-not-exist") is None


async def test_start_run_then_subscribe_returns_snapshot_and_queue():
    await start_run("room-1")
    await append_token("room-1", "hello ")
    await append_token("room-1", "world")

    result = await subscribe("room-1")
    assert result is not None
    queue, snapshot = result
    assert snapshot == "hello world"
    assert isinstance(queue, asyncio.Queue)


async def test_broadcast_fans_out_to_multiple_subscribers():
    await start_run("room-2")
    q1, _ = await subscribe("room-2")
    q2, _ = await subscribe("room-2")

    await broadcast("room-2", {"type": "token", "content": "hi"})

    assert q1.get_nowait() == {"type": "token", "content": "hi"}
    assert q2.get_nowait() == {"type": "token", "content": "hi"}


async def test_mark_done_makes_run_inactive_for_new_subscribers():
    await start_run("room-3")
    await mark_done("room-3")

    assert await subscribe("room-3") is None


async def test_unsubscribe_removes_queue_from_fanout():
    await start_run("room-4")
    q, _ = await subscribe("room-4")
    await unsubscribe("room-4", q)

    await broadcast("room-4", {"type": "token", "content": "x"})
    assert q.empty()


async def test_event_tap_streamer_forwards_to_inner():
    """토큰 누적/브로드캐스트는 SDK 어댑터(test_sdk_registry)가 맡는다.
    여기서는 프록시가 원래 streamer 로 그대로 흘려보내는지만 본다."""
    inner = _FakeStreamer()
    proxy = EventTapStreamer(inner)

    await proxy.send_json({"type": "token", "content": "ab"})
    await proxy.send_text("cd")

    assert inner.sent == [
        {"type": "token", "content": "ab"},
        {"type": "token", "content": "cd"},
    ]


async def test_event_tap_streamer_invokes_on_event_hook_for_context_events():
    inner = _FakeStreamer()
    seen: list[dict] = []

    async def _on_event(evt):
        seen.append(evt)

    proxy = EventTapStreamer(inner, on_event=_on_event)
    await proxy.send_json({"type": "tool_start", "tool": "write_file"})
    await proxy.send_json({"type": "token", "content": "irrelevant-to-hook"})

    assert seen == [
        {"type": "tool_start", "tool": "write_file"},
        {"type": "token", "content": "irrelevant-to-hook"},
    ]


async def test_event_tap_streamer_survives_failing_hook():
    """컨텍스트 저장이 실패해도 스트림이 끊기면 안 된다."""
    inner = _FakeStreamer()

    async def _boom(evt):
        raise RuntimeError("컨텍스트 저장 실패")

    proxy = EventTapStreamer(inner, on_event=_boom)
    await proxy.send_json({"type": "token", "content": "ab"})

    assert inner.sent == [{"type": "token", "content": "ab"}]
