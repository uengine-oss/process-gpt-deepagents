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

from core.api.tenant_auth import request_tenant_id, tenant_guard
from core.chat.stream_registry import format_sse_message, subscribe, unsubscribe
from core.storage.db import get_supabase

logger = logging.getLogger(__name__)

# 활성 스트림 없음은 클라이언트 관점에서 정상 상태(재접속할 대상이 없을 뿐)이므로
# 200으로 응답한다. 404로 표현하면 매 메시지 전송마다(첫 attach 시도는 항상 "아직
# 스트림 없음") 브라우저 네트워크 탭에 실패 요청으로 기록되어 불필요한 노이즈가 된다.
_INACTIVE = JSONResponse({"active": False}, status_code=200)


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


@tenant_guard
async def attach_handler(request: Request) -> JSONResponse | StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    conversation_id = str(body.get("conversation_id") or "").strip()
    # tenant_id 는 요청 본문이 아니라 검증된 토큰에서 온다 — 본문 값만 믿으면
    # 방 ID 와 테넌트 ID 만 아는 외부인이 남의 스트림에 붙을 수 있다.
    tenant_id = request_tenant_id(request)
    if not conversation_id:
        return JSONResponse({"active": False, "error": "conversation_id required"}, status_code=400)

    # 다른 테넌트의 방에 attach 하는 것을 막는다 — 요청자가 속한 tenant_id와
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
