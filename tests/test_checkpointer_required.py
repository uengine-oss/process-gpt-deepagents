"""지속 체크포인터가 설정됐는데 초기화에 실패하면 메모리 저장으로 조용히 넘어가지 않는다.

스펙: infra/process-gpt/openspec/changes/workitem-resume-default/specs/deepagents_workitem-checkpoint-resume  RE-4.1–RE-4.3

2026-10-06 실측(E2-H): InMemory 로 폴백된 워커(replicas 5)에서는 다른 파드가 사람
답을 집는 순간 체크포인트가 없어, 답이 반영되지 않고 같은 질문을 다시 했다(5/5).
이전 코드는 초기화 실패를 로그 한 줄로 남기고 InMemory 로 넘어갔다.
"""

from __future__ import annotations

import asyncio

import pytest

from core.storage import checkpointer as ckpt


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    ckpt._STATE_BY_LOOP.clear()
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    yield
    ckpt._STATE_BY_LOOP.clear()


def _configure_db(monkeypatch):
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_NAME", "postgres")
    monkeypatch.setenv("DB_PORT", "1")


def _failing_setup(monkeypatch, attempts: list):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async def _boom(self):
        attempts.append(1)
        raise ConnectionError("postgres unreachable")

    monkeypatch.setattr(AsyncPostgresSaver, "setup", _boom)


def test_re_4_3_without_db_settings_memory_is_used():
    saver = asyncio.run(ckpt.get_workitem_checkpointer())
    assert saver is ckpt.get_shared_memory_checkpointer()
    # 기동 점검도 통과한다(로컬 개발)
    asyncio.run(ckpt.ensure_checkpointer_ready())


def test_re_4_2_configured_but_failing_raises_instead_of_memory(monkeypatch):
    _configure_db(monkeypatch)
    attempts: list = []
    _failing_setup(monkeypatch, attempts)

    async def _run():
        with pytest.raises(ckpt.CheckpointerUnavailable):
            await ckpt.get_workitem_checkpointer()

    asyncio.run(_run())
    assert attempts == [1]


def test_re_4_2_failure_is_not_permanent(monkeypatch):
    """일시 장애 뒤에는 다시 시도한다 — 이전 코드는 한 번 실패하면 프로세스가 끝날 때까지 InMemory 였다."""
    _configure_db(monkeypatch)
    attempts: list = []
    _failing_setup(monkeypatch, attempts)
    monkeypatch.setattr(ckpt, "INIT_RETRY_SECONDS", 0.0)

    async def _run():
        for _ in range(2):
            with pytest.raises(ckpt.CheckpointerUnavailable):
                await ckpt.get_workitem_checkpointer()

    asyncio.run(_run())
    assert attempts == [1, 1]


def test_re_4_1_boot_fails_when_configured_store_is_unreachable(monkeypatch):
    _configure_db(monkeypatch)
    _failing_setup(monkeypatch, [])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(ckpt.ensure_checkpointer_ready())
    assert exc.value.code not in (0, None)
