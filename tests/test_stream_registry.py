"""Unit tests for core/chat/stream_registry.py — chat SSE reconnect (attach) registry."""

import asyncio

import pytest

from core.chat.stream_registry import (
    MulticastStreamer,
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


async def test_multicast_streamer_forwards_to_inner_and_accumulates_tokens():
    inner = _FakeStreamer()
    await start_run("room-5")
    proxy = MulticastStreamer(inner, "room-5")

    await proxy.send_json({"type": "token", "content": "ab"})
    await proxy.send_json({"type": "token", "content": "cd"})

    assert inner.sent == [
        {"type": "token", "content": "ab"},
        {"type": "token", "content": "cd"},
    ]
    result = await subscribe("room-5")
    assert result is not None
    _, snapshot = result
    assert snapshot == "abcd"


async def test_multicast_streamer_marks_done_on_done_event():
    inner = _FakeStreamer()
    await start_run("room-6")
    proxy = MulticastStreamer(inner, "room-6")

    await proxy.send_json({"type": "done", "content": "final"})

    assert await subscribe("room-6") is None


async def test_multicast_streamer_invokes_on_event_hook_for_context_events():
    inner = _FakeStreamer()
    await start_run("room-7")
    seen: list[dict] = []

    async def _on_event(evt):
        seen.append(evt)

    proxy = MulticastStreamer(inner, "room-7", on_event=_on_event)
    await proxy.send_json({"type": "tool_start", "tool": "write_file"})
    await proxy.send_json({"type": "token", "content": "irrelevant-to-hook"})

    assert seen == [
        {"type": "tool_start", "tool": "write_file"},
        {"type": "token", "content": "irrelevant-to-hook"},
    ]
