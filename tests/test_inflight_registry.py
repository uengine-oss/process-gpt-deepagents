import asyncio

import pytest

from core.chat.inflight_registry import (
    clear_inflight_if_current,
    get_inflight,
    set_inflight,
)


async def _never_finishes():
    await asyncio.Event().wait()


async def test_get_inflight_returns_none_when_nothing_registered():
    assert get_inflight("room-1") is None


async def test_set_then_get_returns_registered_task_and_queue():
    task = asyncio.create_task(_never_finishes())
    sentinel_queue = object()
    try:
        set_inflight("room-2", task, sentinel_queue)
        entry = get_inflight("room-2")
        assert entry is not None
        assert entry[0] is task
        assert entry[1] is sentinel_queue
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_get_inflight_treats_done_task_as_absent():
    async def _finishes_immediately():
        return None

    task = asyncio.create_task(_finishes_immediately())
    await task
    set_inflight("room-3", task, object())
    assert get_inflight("room-3") is None


async def test_clear_inflight_if_current_only_removes_matching_task():
    old_task = asyncio.create_task(_never_finishes())
    new_task = asyncio.create_task(_never_finishes())
    try:
        set_inflight("room-4", old_task, object())
        # A newer turn supersedes the old one and registers itself.
        set_inflight("room-4", new_task, object())

        # The old task's done-callback fires late (e.g. after being cancelled) —
        # it must NOT clobber the already-registered newer task.
        clear_inflight_if_current("room-4", old_task)
        entry = get_inflight("room-4")
        assert entry is not None
        assert entry[0] is new_task

        # The current task's own cleanup does remove its entry.
        clear_inflight_if_current("room-4", new_task)
        assert get_inflight("room-4") is None
    finally:
        for t in (old_task, new_task):
            t.cancel()
        for t in (old_task, new_task):
            with pytest.raises(asyncio.CancelledError):
                await t
