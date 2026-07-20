"""core/api/chat_stream_attach.py — 진행 중인 채팅 스트림에 재접속(attach)하는 엔드포인트.

방을 나갔다가 재진입하거나 새로고침한 클라이언트가, 그 방(conversation_id)에서
아직 실행 중인 채팅 턴이 있으면 누적된 텍스트 스냅샷을 먼저 받고 이후 토큰을
실시간으로 이어받을 수 있게 한다. processgpt_agent_sdk 는 건드리지 않고,
core/chat/stream_registry.py 의 메모리 레지스트리만 사용한다.
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from core.chat.stream_registry import format_sse_message, subscribe, unsubscribe
from core.storage.db import get_supabase

logger = logging.getLogger(__name__)

_INACTIVE = JSONResponse({"active": False}, status_code=404)


def _room_tenant_id(conversation_id: str) -> str:
    try:
        sb = get_supabase()
        resp = sb.table("chat_rooms").select("tenant_id").eq("id", conversation_id).execute()
        rows = getattr(resp, "data", None) or []
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("tenant_id") or "")
    except Exception:
        logger.warning("chat_stream_attach: chat_rooms 조회 실패 | conversation_id=%s", conversation_id, exc_info=True)
    return ""


async def attach_handler(request: Request) -> JSONResponse | StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    conversation_id = str(body.get("conversation_id") or "").strip()
    tenant_id = str(body.get("tenant_id") or "").strip()
    if not conversation_id or not tenant_id:
        return JSONResponse({"active": False, "error": "conversation_id/tenant_id required"}, status_code=400)

    # 다른 테넌트의 방에 attach 하는 것을 막는다 — /chat/stream 이 받는 tenant_id와
    # 방 소유 tenant_id가 일치할 때만 재접속을 허용한다.
    room_tenant_id = _room_tenant_id(conversation_id)
    if not room_tenant_id or room_tenant_id != tenant_id:
        return _INACTIVE

    subscription = await subscribe(conversation_id)
    if subscription is None:
        return _INACTIVE
    queue, snapshot_text = subscription

    async def _stream():
        try:
            yield format_sse_message({"type": "snapshot", "content": snapshot_text})
            while True:
                item = await queue.get()
                yield format_sse_message(item)
                if isinstance(item, dict) and item.get("type") in ("done", "error"):
                    return
        finally:
            # 클라이언트 연결이 끊기면(생성기 aclose) 구독자를 레지스트리에서 제거한다.
            await unsubscribe(conversation_id, queue)

    return StreamingResponse(_stream(), media_type="text/event-stream")


routes = [Route("/chat/stream/attach", attach_handler, methods=["POST"])]
