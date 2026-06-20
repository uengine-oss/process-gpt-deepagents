"""LangGraph Postgres 체크포인터 + thread id."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


@dataclass
class _LoopCheckpointerState:
    pool: Any | None = None
    saver: Any | None = None
    init_lock: asyncio.Lock | None = None
    init_failed: bool = False


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
    if st.init_failed:
        return None
    if st.saver is not None:
        return st.saver

    assert st.init_lock is not None
    async with st.init_lock:
        if st.saver is not None:
            return st.saver
        if st.init_failed:
            return None
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except Exception:
            logger.exception("Postgres checkpointer imports failed")
            st.init_failed = True
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
            st.init_failed = True
            if pool is not None:
                try:
                    await pool.close()
                except Exception:
                    logger.exception("checkpoint pool close after failed init", exc_info=True)
            return None

        st.pool = pool
        st.saver = saver
        logger.info("AsyncPostgresSaver ready for LangGraph checkpointing")
        return st.saver
