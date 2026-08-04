"""core/api/file_download.py — 샌드박스 파일을 위한 내부 단기 토큰 다운로드 엔드포인트.

Supabase 업로드 없이 워크스페이스 파일(pdf/docx 등)을 프론트에 제공한다.
register_file()로 파일을 등록하면 1시간 유효 토큰이 발급되고,
GET /api/internal/download/{token} 으로 다운로드할 수 있다.

REDIS_URL 이 설정돼 있으면 Redis에 저장해, 토큰을 발급한 파드와 다른 파드가
다운로드 요청을 받아도 정상 서빙되게 한다(다중 파드 배포 지원). 공용 Redis
클라이언트(core/storage/redis_client.py)가 decode_responses=True라 raw bytes를
그대로 못 넣으므로 base64로 인코딩해 JSON으로 저장한다. REDIS_URL 이 없으면
기존과 동일한 프로세스 로컬 dict 로 폴백한다.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.storage.redis_client import get_redis

logger = logging.getLogger(__name__)

_TTL = 3600  # 1시간

# 로컬 폴백(REDIS_URL 미설정 시): token → (content_bytes, filename, expires_at_monotonic)
_store: dict[str, tuple[bytes, str, float]] = {}


def _redis_key(token: str) -> str:
    return f"download:{token}"


def _sweep() -> None:
    now = time.monotonic()
    expired = [k for k, (_, _, exp) in _store.items() if exp < now]
    for k in expired:
        del _store[k]


async def register_file(content: bytes, filename: str) -> str:
    """파일을 등록하고 다운로드 토큰을 반환한다."""
    token = secrets.token_urlsafe(32)
    r = get_redis()
    if r is not None:
        try:
            payload = json.dumps({
                "filename": filename,
                "content_b64": base64.b64encode(content).decode("ascii"),
            })
            await r.set(_redis_key(token), payload, ex=_TTL)
            return token
        except Exception:
            logger.warning("file_download(redis): register_file 실패 — 로컬 폴백", exc_info=True)

    _sweep()
    _store[token] = (content, filename, time.monotonic() + _TTL)
    return token


async def _lookup(token: str) -> tuple[bytes, str] | None:
    r = get_redis()
    if r is not None:
        try:
            raw = await r.get(_redis_key(token))
        except Exception:
            logger.warning("file_download(redis): 조회 실패", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            obj = json.loads(raw)
            return base64.b64decode(obj["content_b64"]), obj["filename"]
        except Exception:
            logger.warning("file_download(redis): 페이로드 파싱 실패", exc_info=True)
            return None

    _sweep()
    entry = _store.get(token)
    if not entry:
        return None
    content, filename, expires_at = entry
    if time.monotonic() > expires_at:
        del _store[token]
        return None
    return content, filename


async def download_handler(request: Request) -> Response:
    token = request.path_params.get("token", "")
    result = await _lookup(token)
    if result is None:
        return JSONResponse({"error": "not found or expired"}, status_code=404)
    content, filename = result
    mime, _ = mimetypes.guess_type(filename)
    return Response(
        content=content,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


routes = [Route("/api/internal/download/{token}", download_handler, methods=["GET"])]
