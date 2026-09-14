"""core/chat/sdk_registry.py — SDK 레지스트리 어댑터.

SDK 가 마운트한 /chat/stream 과 이 저장소의 /chat/stream/attach · /chat/stop 이
**같은** 레지스트리를 보는지 확인한다. 어긋나면 다중 파드에서 재접속과 중지가
엉뚱한 파드를 보게 된다.
"""

import asyncio

import pytest

from core.chat import stream_registry
from core.chat.sdk_registry import (
    DeepAgentsInflightRegistry,
    DeepAgentsRunRegistry,
    install,
)
from core.chat.stream_registry import subscribe


@pytest.fixture(autouse=True)
def _clean_registry():
    stream_registry._registry.clear()
    stream_registry._locks.clear()
    yield
    stream_registry._registry.clear()
    stream_registry._locks.clear()


class TestRunRegistryAdapter:
    """SDK 가 출력 큐에서 뽑아 넘기는 이벤트가 이 저장소 레지스트리에 쌓인다."""

    async def test_토큰이_누적돼_스냅샷이_된다(self):
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "token", "content": "안녕"})
        await reg.record("room-1", {"type": "token", "content": "하세요"})

        # attach 라우트가 쓰는 함수로 확인한다 — 두 경로가 같은 저장소를 봐야 한다.
        result = await subscribe("room-1")
        assert result is not None
        _queue, snapshot = result
        assert snapshot == "안녕하세요"

    async def test_구독자에게_실시간으로_전달된다(self):
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        queue, _ = await subscribe("room-1")

        await reg.record("room-1", {"type": "tool_start", "tool": "write_file"})

        assert queue.get_nowait() == {"type": "tool_start", "tool": "write_file"}

    async def test_done_이벤트가_런을_종료시킨다(self):
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "done", "content": "끝"})

        assert await subscribe("room-1") is None
        assert await reg.is_active("room-1") is False

    async def test_error_이벤트도_런을_종료시킨다(self):
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "error", "error": "boom"})

        assert await subscribe("room-1") is None

    async def test_is_active_는_구독자를_남기지_않는다(self):
        """확인만 하려고 subscribe 를 쓰면 유령 구독자가 쌓인다."""
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")

        assert await reg.is_active("room-1") is True
        assert stream_registry._registry["room-1"].subscribers == []

    async def test_새_턴은_이전_누적_본문을_지운다(self):
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "token", "content": "이전턴"})
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "token", "content": "새턴"})

        _queue, snapshot = await subscribe("room-1")
        assert snapshot == "새턴"

    async def test_토큰이_두_번_기록되지_않는다(self):
        """SDK 어댑터와 executor 양쪽에서 기록하면 스냅샷 본문이 두 배가 된다."""
        reg = DeepAgentsRunRegistry()
        await reg.start_run("room-1")
        await reg.record("room-1", {"type": "token", "content": "가"})

        _queue, snapshot = await subscribe("room-1")
        assert snapshot == "가"


class TestInflightRegistryAdapter:
    async def test_supersede_는_아무것도_하지_않는다(self):
        """대체/이어가기(HITL) 판단은 executor 가 한다 — SDK 가 먼저 끊으면 안 된다."""
        reg = DeepAgentsInflightRegistry()
        task = asyncio.create_task(asyncio.sleep(60))
        stream_registry_task_holder = task
        try:
            from core.chat import inflight_registry

            inflight_registry.set_inflight("room-1", task, None)
            assert await reg.supersede("room-1") is False
            assert not task.cancelled()
        finally:
            stream_registry_task_holder.cancel()
            from core.chat import inflight_registry

            inflight_registry._INFLIGHT.pop("room-1", None)

    async def test_set_inflight_은_executor_등록을_덮지_않는다(self):
        """SDK 가 만든 바깥 task 로 덮으면 취소 대상이 어긋난다."""
        from core.chat import inflight_registry

        reg = DeepAgentsInflightRegistry()
        executor_task = asyncio.create_task(asyncio.sleep(60))
        sdk_task = asyncio.create_task(asyncio.sleep(60))
        try:
            inflight_registry.set_inflight("room-1", executor_task, None)
            reg.set_inflight("room-1", sdk_task)

            assert reg.get_inflight("room-1") is executor_task
        finally:
            executor_task.cancel()
            sdk_task.cancel()
            inflight_registry._INFLIGHT.pop("room-1", None)

    async def test_cancel_은_진행_중인_턴을_취소한다(self):
        from core.chat import inflight_registry

        reg = DeepAgentsInflightRegistry()
        started = asyncio.Event()

        async def long_turn():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(long_turn())
        inflight_registry.set_inflight("room-1", task, None)
        await started.wait()

        assert await reg.cancel("room-1") is True
        assert task.cancelled()

    async def test_취소할_턴이_없으면_False(self):
        reg = DeepAgentsInflightRegistry()
        assert await reg.cancel("room-없음") is False


def test_install_이_SDK_레지스트리를_교체한다():
    from processgpt_agent_sdk import chat_registry as sdk_registry

    install()
    assert isinstance(sdk_registry.get_run_registry(), DeepAgentsRunRegistry)
    assert isinstance(sdk_registry.get_inflight_registry(), DeepAgentsInflightRegistry)
