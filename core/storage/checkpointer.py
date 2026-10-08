"""LangGraph Postgres 체크포인터 + thread id."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


@dataclass
class _LoopCheckpointerState:
    pool: Any | None = None
    saver: Any | None = None
    init_lock: asyncio.Lock | None = None
    #: 마지막 초기화 실패 시각(monotonic). 영구 플래그가 아니다 — 일시 장애 뒤
    #: INIT_RETRY_SECONDS 가 지나면 다시 시도한다.
    failed_at: float | None = None


#: 초기화 실패 뒤 다시 시도하기까지의 간격. 그 사이 요청은 바로 실패한다 — 매 요청이
#: 접속 타임아웃(30초)을 다시 기다리지 않게.
INIT_RETRY_SECONDS = 30.0


class CheckpointerUnavailable(RuntimeError):
    """지속 체크포인터가 설정됐는데 쓸 수 없다.

    메모리 저장으로 대체하면 다른 파드가 사람 답을 집을 때 체크포인트가 없어 같은
    질문을 반복하고(2026-10-06 E2-H 5/5), 크래시 재점유도 처음부터 다시 한다.
    조용히 넘어가지 않고 작업을 실패로 끝내 화면에 알린다.
    """


# Lock은 루프에 묶이므로 checkpointer 상태도 루프별로 둔다.
_STATE_BY_LOOP: dict[int, _LoopCheckpointerState] = {}


def _loop_key() -> int:
    return id(asyncio.get_running_loop())


def _get_state_for_current_loop() -> _LoopCheckpointerState:
    key = _loop_key()
    st = _STATE_BY_LOOP.get(key)
    if st is None:
        st = _LoopCheckpointerState()
        _STATE_BY_LOOP[key] = st
    if st.init_lock is None:
        st.init_lock = asyncio.Lock()
    return st


# 프로세스 전역 공유 인메모리 체크포인터.
# Postgres(DB_*) 가 없을 때 HITL interrupt/resume 가 같은 서버 프로세스 안에서
# 요청 간에 유지되도록 단일 InMemorySaver 인스턴스를 공유한다.
# (uvicorn 단일 워커 가정 — 다중 워커/재시작 시 유실되므로 운영은 DB_* 권장.)
_SHARED_MEMORY_SAVER: Any | None = None


def get_shared_memory_checkpointer() -> Any:
    """프로세스 전역 단일 InMemorySaver 를 반환한다 (요청 간 interrupt 체크포인트 유지)."""
    global _SHARED_MEMORY_SAVER
    if _SHARED_MEMORY_SAVER is None:
        try:
            from langgraph.checkpoint.memory import InMemorySaver
            _SHARED_MEMORY_SAVER = InMemorySaver()
        except Exception:
            from langgraph.checkpoint.memory import MemorySaver
            _SHARED_MEMORY_SAVER = MemorySaver()
        logger.info("공유 InMemory 체크포인터 생성 (Postgres 미설정 — 프로세스 내 HITL resume 사용)")
    return _SHARED_MEMORY_SAVER


def resolve_checkpoint_thread_id(*, is_chat: bool, row: Mapping[str, Any]) -> str:
    """채팅이면 conv 스레드, 아니면 todo 스레드."""
    if is_chat:
        for key in ("id", "proc_inst_id", "root_proc_inst_id"):
            v = row.get(key)
            if v is not None:
                s = str(v).strip()
                if s:
                    return s
        return ""
    tid = row.get("id")
    return str(tid).strip() if tid is not None else ""


def checkpoint_postgres_uri() -> str:
    """DB_* → postgresql:// DSN."""
    host = (os.environ.get("DB_HOST") or "").strip()
    name = (os.environ.get("DB_NAME") or "").strip()
    if not host or not name:
        return ""
    user = (os.environ.get("DB_USER") or "postgres").strip() or "postgres"
    raw_pw = os.environ.get("DB_PASSWORD")
    password = "" if raw_pw is None else str(raw_pw)
    port = (os.environ.get("DB_PORT") or "5432").strip() or "5432"
    user_q = quote_plus(user)
    if password:
        auth = f"{user_q}:{quote_plus(password)}"
    else:
        auth = user_q
    return f"postgresql://{auth}@{host}:{port}/{name}"


async def get_async_postgres_checkpointer() -> Any | None:
    """DB_* 있으면 AsyncPostgresSaver, 없으면 None."""
    st = _get_state_for_current_loop()

    uri = checkpoint_postgres_uri()
    if not uri:
        return None
    if st.saver is not None:
        return st.saver
    if _recently_failed(st):
        return None

    assert st.init_lock is not None
    async with st.init_lock:
        if st.saver is not None:
            return st.saver
        if _recently_failed(st):
            return None
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except Exception:
            logger.exception("Postgres checkpointer imports failed")
            st.failed_at = time.monotonic()
            return None

        pool = None
        try:
            pool = AsyncConnectionPool(
                conninfo=uri,
                kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
                min_size=1,
                max_size=10,
                open=False,
                timeout=30.0,
            )
            await pool.open()
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
        except Exception:
            logger.exception("AsyncPostgresSaver init/setup failed; HITL graph resume disabled")
            st.failed_at = time.monotonic()
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    logger.exception("checkpoint pool close after failed init", exc_info=True)
            return None

        st.pool = pool
        st.saver = saver
        st.failed_at = None
        logger.info("AsyncPostgresSaver ready for LangGraph checkpointing")
        return st.saver


def _recently_failed(st: _LoopCheckpointerState) -> bool:
    return st.failed_at is not None and (time.monotonic() - st.failed_at) < INIT_RETRY_SECONDS


def persistent_checkpointer_configured() -> bool:
    """지속 저장소(Postgres) 접속 정보 DB_* 가 설정됐는가."""
    return bool(checkpoint_postgres_uri())


async def get_workitem_checkpointer() -> Any:
    """실행에 쓸 체크포인터. DB_* 가 있으면 Postgres 만, 없으면(로컬 개발) 공유 메모리.

    DB_* 가 있는데 Postgres 를 쓸 수 없으면 메모리로 대체하지 않고
    CheckpointerUnavailable 을 던진다.
    """
    saver = await get_async_postgres_checkpointer()
    if saver is not None:
        return saver
    if persistent_checkpointer_configured():
        raise CheckpointerUnavailable(
            "체크포인트 저장소(Postgres)에 연결할 수 없어 작업을 진행하지 않았습니다. "
            "메모리 저장으로 진행하면 재개·사람 답변이 반영되지 않습니다."
        )
    logger.warning("DB_* 미설정 — 프로세스 메모리 체크포인터 사용(재시작·다른 파드에서 재개 불가)")
    return get_shared_memory_checkpointer()


async def ensure_checkpointer_ready() -> None:
    """기동 점검. DB_* 가 설정됐는데 체크포인터를 만들 수 없으면 기동을 실패시킨다."""
    if not persistent_checkpointer_configured():
        logger.warning("DB_* 미설정 — 메모리 체크포인터로 기동한다. 운영에서는 DB_* 를 설정할 것")
        return
    if await get_async_postgres_checkpointer() is None:
        logger.error("DB_* 가 설정됐지만 Postgres 체크포인터를 초기화하지 못했다. 기동을 중단한다")
        raise SystemExit(1)
    logger.info("Postgres 체크포인터 기동 점검 통과")
