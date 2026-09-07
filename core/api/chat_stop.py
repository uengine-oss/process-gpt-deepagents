"""core/api/chat_stop.py — 진행 중인 채팅 턴을 서버에서 실제로 중지하는 엔드포인트.

프론트의 '중지' 버튼은 지금까지 자기 쪽 `fetch` 만 abort 했다. 그러면 화면에서는
스트림이 멈추지만 **서버의 그래프 실행은 계속 돌아간다** — LLM 토큰과 도구 호출이
그대로 소비되고, 산출물 파일까지 만들어진 뒤 끝난다. 사용자 입장에선 "중지를 눌렀는데
계속 동작"으로 보인다.

이 엔드포인트는 `core/chat/inflight_registry` 가 이미 갖고 있는 취소 경로를 그대로
재사용한다(로컬 task 취소 + Redis 설정 시 소유 파드로 취소 신호 전파). 별도 취소
메커니즘을 새로 만들지 않으므로 다중 파드에서도 동일하게 동작한다.
"""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.chat.inflight_registry import get_inflight, supersede_previous_turn
from core.chat.stream_registry import mark_done
from core.storage.db import get_supabase

logger = logging.getLogger(__name__)


def _room_tenant_id(conversation_id: str) -> str:
    try:
        sb = get_supabase()
        resp = sb.table("chat_rooms").select("tenant_id").eq("id", conversation_id).execute()
        rows = getattr(resp, "data", None) or []
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("tenant_id") or "")
    except Exception:
        logger.warning(
            "chat_stop: chat_rooms 조회 실패 | conversation_id=%s", conversation_id, exc_info=True
        )
    return ""


async def stop_handler(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    conversation_id = str(body.get("conversation_id") or "").strip()
    tenant_id = str(body.get("tenant_id") or "").strip()
    if not conversation_id or not tenant_id:
        return JSONResponse(
            {"stopped": False, "error": "conversation_id/tenant_id required"}, status_code=400
        )

    # 다른 테넌트의 방을 중지시키지 못하게 한다(attach 와 동일한 소유권 검사).
    room_tenant_id = _room_tenant_id(conversation_id)
    if not room_tenant_id or room_tenant_id != tenant_id:
        return JSONResponse({"stopped": False, "reason": "forbidden"}, status_code=403)

    # HITL interrupt 로 이미 끝난(사용자 응답 대기) 턴은 in-flight 가 아니다 —
    # 그 상태에서 중지를 눌러도 취소할 실행이 없으므로 그대로 알려준다.
    if get_inflight(conversation_id) is None:
        await mark_done(conversation_id)
        logger.info("chat_stop: 진행 중인 턴 없음 | conversation_id=%s", conversation_id)
        return JSONResponse({"stopped": False, "reason": "no_active_turn"})

    # cancel=True — 사용자가 명시적으로 중지를 요청했다. 취소되면 그 턴의 SSE 에는
    # 종료 이벤트가 실려 나가고, done-callback 이 레지스트리를 정리한다.
    await supersede_previous_turn(conversation_id, cancel=True)
    await mark_done(conversation_id)
    logger.info("chat_stop: 사용자 요청으로 턴 중지 | conversation_id=%s", conversation_id)
    return JSONResponse({"stopped": True})


routes = [Route("/chat/stop", stop_handler, methods=["POST"])]
