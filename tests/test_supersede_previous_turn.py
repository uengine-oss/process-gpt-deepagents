import asyncio

from core.chat.inflight_registry import get_inflight, set_inflight, supersede_previous_turn


class _FakeChatEventQueue:
    """Mimics processgpt_agent_sdk.chat_mode.ChatEventQueue's private out_queue."""

    def __init__(self):
        self._out_queue: "asyncio.Queue" = asyncio.Queue()


async def test_supersede_cancels_a_real_running_loop_within_timeout():
    stopped = asyncio.Event()

    async def _long_running_turn():
        try:
            # Simulates an in-progress graph.astream() loop that would otherwise
            # run far longer than any reasonable request should wait.
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.set()
            raise

    prev_task = asyncio.create_task(_long_running_turn())
    prev_queue = _FakeChatEventQueue()
    # Let the task actually start running before we try to supersede it.
    await asyncio.sleep(0)

    set_inflight("room-cancel-test", prev_task, prev_queue)

    await asyncio.wait_for(supersede_previous_turn("room-cancel-test"), timeout=5)

    assert prev_task.cancelled() or (prev_task.done() and stopped.is_set())
    assert stopped.is_set()
    assert get_inflight("room-cancel-test") is None

    # The old stream must get a synthetic close event so it doesn't hang forever
    # for clients that never abort their own connection.
    event = prev_queue._out_queue.get_nowait()
    assert event["data"]["type"] == "error"


async def test_supersede_is_a_noop_when_nothing_is_inflight():
    # Must not raise even when there's nothing registered for this room.
    await asyncio.wait_for(supersede_previous_turn("room-with-nothing-inflight"), timeout=1)
