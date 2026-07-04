"""core/api/file_download.py — 샌드박스 파일을 위한 내부 단기 토큰 다운로드 엔드포인트.

Supabase 업로드 없이 워크스페이스 파일(pdf/docx 등)을 프론트에 제공한다.
register_file()로 파일을 등록하면 1시간 유효 토큰이 발급되고,
GET /api/internal/download/{token} 으로 다운로드할 수 있다.
"""
from __future__ import annotations

import mimetypes
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_TTL = 3600  # 1시간

# token → (content_bytes, filename, expires_at_monotonic)
_store: dict[str, tuple[bytes, str, float]] = {}


def register_file(content: bytes, filename: str) -> str:
    """파일을 메모리에 등록하고 다운로드 토큰을 반환한다."""
    _sweep()
    token = secrets.token_urlsafe(32)
    _store[token] = (content, filename, time.monotonic() + _TTL)
    return token


def _sweep() -> None:
    now = time.monotonic()
    expired = [k for k, (_, _, exp) in _store.items() if exp < now]
    for k in expired:
        del _store[k]


async def download_handler(request: Request) -> Response:
    token = request.path_params.get("token", "")
    entry = _store.get(token)
    if not entry:
        return JSONResponse({"error": "not found or expired"}, status_code=404)
    content, filename, expires_at = entry
    if time.monotonic() > expires_at:
        del _store[token]
        return JSONResponse({"error": "expired"}, status_code=410)
    mime, _ = mimetypes.guess_type(filename)
    return Response(
        content=content,
        media_type=mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


routes = [Route("/api/internal/download/{token}", download_handler, methods=["GET"])]
