"""인계로 받은 일의 앞선 대화.

대화창 인계로 받은 할 일은 새 방에서 시작한다. 무엇을 넘겨받았는지는 이 업무가 거쳐 온
앞선 방들의 대화에 있다. 예전에는 넘기는 쪽이 화면에 불러온 메시지를 '대화 기록.md' 로
복사해 넘겼는데, 화면에 불러온 만큼(최근 10개 남짓)만 담겨 대화 전체가 아니었고 단계마다
이름만 다른 사본이 쌓였다. 이제 받은 방 id 로 인계 체인을 거슬러 올라가 원래 대화를 읽는다.

규칙은 받은 방 화면의 '이전 대화'(process-gpt-vue3 HandoverHistory)와 같다.
  - handover_events 의 to_room_id → from_room_id 를 따라 시작 방까지 올라간다.
  - 각 방은 그 방에서 다음 방으로 인계한 시각까지만 읽는다(인계 직후의 '~님에게 인계했습니다' 포함).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

MAX_DEPTH = 20
AFTER_HANDOVER = timedelta(seconds=10)
# 모델에 싣는 앞선 대화의 최대 글자 수. 넘으면 오래된 쪽부터 줄인다.
MAX_CHARS = 60_000


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def resolve_chain(sb: Any, room_id: str, tenant_id: str) -> list[dict[str, Any]]:
    """받은 방에서 시작 방까지 거슬러 올라간 인계 고리. 시작 방 → 바로 앞 방 순서.

    각 항목: {room_id, until(인계 시각), handed_by, handed_to}
    """
    chain: list[dict[str, Any]] = []
    seen = {room_id}
    to_room = room_id
    for _ in range(MAX_DEPTH):
        rows = (
            sb.table("handover_events")
            .select("from_room_id,to_room_id,occurred_at,from_user_name,to_user_name")
            .eq("tenant_id", tenant_id)
            .eq("to_room_id", to_room)
            .order("occurred_at")
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            break
        ev = rows[0]
        from_room = ev.get("from_room_id")
        if not from_room or from_room in seen:
            break
        seen.add(from_room)
        chain.insert(
            0,
            {
                "room_id": from_room,
                "until": ev.get("occurred_at"),
                "handed_by": ev.get("from_user_name") or "",
                "handed_to": ev.get("to_user_name") or "",
            },
        )
        to_room = from_room
    return chain


def _room_messages(sb: Any, room_id: str, until: Any) -> list[dict[str, Any]]:
    rows = sb.table("chats").select("uuid,messages").eq("id", room_id).execute().data or []
    limit = _ts(until)
    limit = limit + AFTER_HANDOVER if limit else None
    out: list[tuple[str, dict[str, Any]]] = []
    for r in rows:
        m = r.get("messages") or {}
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        at = _ts(m.get("timeStamp"))
        if limit and at and at > limit:
            continue
        out.append((str(m.get("timeStamp") or ""), m))
    out.sort(key=lambda x: x[0])
    return [m for _, m in out]


def build_handover_history(sb: Any, room_id: str, tenant_id: str, max_chars: int = MAX_CHARS) -> list[dict[str, Any]]:
    """인계로 받은 방의 앞선 대화를 대화 이력(메시지 하나)으로 만든다. 인계로 받은 방이 아니면 []."""
    if not room_id or not tenant_id:
        return []
    try:
        chain = resolve_chain(sb, room_id, tenant_id)
        if not chain:
            return []
        names = {}
        ids = [c["room_id"] for c in chain]
        for r in sb.table("chat_rooms").select("id,name").in_("id", ids).execute().data or []:
            names[r.get("id")] = r.get("name") or ""
        blocks: list[str] = []
        for c in chain:
            lines = []
            for m in _room_messages(sb, c["room_id"], c["until"]):
                who = m.get("name") or m.get("email") or m.get("role") or ""
                lines.append(f"[{who}] {m.get('content', '').strip()}")
            head = f"## {names.get(c['room_id']) or c['room_id']}"
            if c["handed_to"]:
                head += f" ({c['handed_by']} → {c['handed_to']})"
            blocks.append(head + "\n" + ("\n\n".join(lines) if lines else "(대화 없음)"))
        body = "\n\n".join(blocks)
        if len(body) > max_chars:
            body = "…(앞부분 생략)\n" + body[-max_chars:]
        text = (
            "다음은 이 업무가 이 방으로 넘어오기 전에 거쳐 온 방들의 대화입니다. "
            "시작한 방부터 순서대로이며, 각 방은 다음 사람에게 넘긴 시점까지입니다. "
            "이 맥락을 바탕으로 아래 요청을 이어서 처리하세요.\n\n" + body
        )
        return [{"role": "user", "content": text}]
    except Exception:
        logger.warning("인계 앞선 대화 조회 실패 room=%s", room_id, exc_info=True)
        return []
