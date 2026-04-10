"""포트 8888에서 FastAPI(헬스 + /chat)를 백그라운드 스레드로 기동.

채팅 요청은 기본으로 터미널 로그에 보이도록(uvicorn access log 포함) 동작한다.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import threading
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from deepagents_executor import DeepAgentsExecutor

logger = logging.getLogger(__name__)


def _port_available(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError as e:
        if getattr(e, "errno", None) in (errno.EADDRINUSE, 48):
            return False
        raise
    finally:
        sock.close()


def start_http_server(
    executor: "DeepAgentsExecutor",
    *,
    host: str = "0.0.0.0",
    port: int = 8888,
) -> threading.Thread | None:
    """uvicorn으로 ``/health``, ``/chat``, ``/chat/stream`` 을 제공합니다.

    포트가 이미 사용 중이면 경고만 남기고 ``None`` 을 반환합니다.
    """
    if not _port_available(host, port):
        logger.warning("HTTP API port in use; skipping (host=%s port=%d)", host, port)
        return None

    from app.http_chat import create_chat_app

    app = create_chat_app(executor)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        # 요청 라인(POST /chat 등)도 항상 보이게 한다.
        access_log=True,
        # handler 쪽 logger.info가 보이도록 info로 둔다.
        log_level="info",
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        try:
            asyncio.run(server.serve())
        except OSError as e:
            logger.warning("HTTP API serve failed: %s", e)

    thread = threading.Thread(target=_run, name="deepagents-http-api", daemon=True)
    thread.start()
    logger.info("HTTP API started http://%s:%d (/health /chat /chat/stream)", host, port)
    return thread

