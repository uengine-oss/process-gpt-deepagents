"""공용 Redis 클라이언트 — 채팅 모드(inflight_registry/stream_registry)의 다중 파드 상태 공유용.

REDIS_URL 이 없으면 None 을 반환한다: 두 레지스트리 모두 이 경우 지금까지의 순수
프로세스-로컬 동작으로 폴백하므로, 로컬 개발 환경은 변경 없이 그대로 동작한다
(checkpointer.py 의 "DB_* 없으면 인메모리 폴백" 패턴과 동일한 설계).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

logger = logging.getLogger(__name__)

_client: Any | None = None
_init_attempted = False
_pod_id: str | None = None


def get_redis() -> Any | None:
    """프로세스 전역 공유 redis.asyncio.Redis 클라이언트. REDIS_URL 미설정/초기화 실패 시 None."""
    global _client, _init_attempted
    if _client is not None:
        return _client
    if _init_attempted:
        return None
    _init_attempted = True

    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        from redis.asyncio import Redis
        _client = Redis.from_url(url, decode_responses=True)
        logger.info("Redis 클라이언트 초기화 완료 (다중 파드 채팅 상태 공유 활성화)")
    except Exception:
        logger.exception("Redis 클라이언트 초기화 실패 — 프로세스 로컬 동작으로 폴백")
        _client = None
    return _client


def get_pod_id() -> str:
    """이 파드(프로세스)를 식별하는 값 — inflight 소유권 레코드 등에 사용."""
    global _pod_id
    if _pod_id is None:
        env_val = os.environ.get("POD_ID") or os.environ.get("HOSTNAME")
        _pod_id = env_val or f"{socket.gethostname()}:{os.getpid()}"
    return _pod_id
