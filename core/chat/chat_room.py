from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..storage.db import get_supabase, json_normalize, now_iso

logger = logging.getLogger(__name__)


def build_initial_chat_room_context(*, has_file: bool = False) -> dict[str, Any]:
    return json_normalize({
        "todos": [],
        "tools": [],
        "skills": [],
        "enabled": {"todos": False, "tools": True, "skills": False},
        "updatedAt": now_iso(),
        "orchestration": "deepagents",
    })


def _tool_plan_to_context_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        name = str(t.get("name") or "").strip()
        tool = str(t.get("tool") or "").strip()
        disp = t.get("displayName") or t.get("display_name") or ""
        disp = str(disp).strip() if disp else ""
        if not (tid or name or tool):
            continue
        out.append({
            "id": tid or None,
            "name": name,
            "tool": tool,
            "status": "planned",
            "displayName": disp or name or tool,
        })
    return out


def merge_chat_room_context_from_event(
    existing: Any,
    evt: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(evt, dict):
        return None
    et = str(evt.get("type") or "").strip()
    if et not in ("plan_tools", "plan_skills", "plan_todos", "tool_start", "tool_end"):
        return None

    ctx: dict[str, Any] = (
        dict(existing) if isinstance(existing, dict)
        else build_initial_chat_room_context()
    )
    ctx.setdefault("todos", [])
    ctx.setdefault("tools", [])
    ctx.setdefault("skills", [])
    ctx.setdefault("enabled", {"todos": False, "tools": True, "skills": False})
    if "orchestration" in ctx:
        ctx["orchestration"] = str(ctx.get("orchestration") or "deepagents")
    ctx["updatedAt"] = now_iso()

    def _upsert_by_id(
        lst: list[dict[str, Any]], items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for x in lst:
            if not isinstance(x, dict):
                continue
            xid = str(x.get("id") or "").strip()
            if not xid:
                continue
            if xid not in by_id:
                order.append(xid)
            by_id[xid] = dict(x)
        for x in items:
            xid = str(x.get("id") or "").strip()
            if not xid:
                continue
            if xid not in by_id:
                order.append(xid)
                by_id[xid] = dict(x)
            else:
                by_id[xid] = {**by_id[xid], **x}
        return [by_id[i] for i in order if i in by_id]

    if et == "plan_tools":
        planned = _tool_plan_to_context_tools(evt.get("tools"))
        cur = [x for x in (ctx.get("tools") or []) if isinstance(x, dict)]
        with_ids = [x for x in planned if str(x.get("id") or "").strip()]
        without_ids = [x for x in planned if not str(x.get("id") or "").strip()]
        merged = _upsert_by_id(cur, with_ids)
        merged.extend(without_ids)
        ctx["tools"] = merged
        en = dict(ctx.get("enabled") or {})
        en["tools"] = True
        ctx["enabled"] = en
        return json_normalize(ctx)

    if et == "plan_skills":
        skills = evt.get("skills")
        if isinstance(skills, list):
            cur_s = [str(s).strip() for s in (ctx.get("skills") or []) if str(s).strip()]
            new_s = [str(s).strip() for s in skills if str(s).strip()]
            ctx["skills"] = list(dict.fromkeys([*cur_s, *new_s]))
            en = dict(ctx.get("enabled") or {})
            en["skills"] = bool(ctx["skills"])
            ctx["enabled"] = en
        return json_normalize(ctx)

    if et == "plan_todos":
        todos = evt.get("todos")
        if isinstance(todos, list):
            new_todos = [x for x in todos if isinstance(x, dict)]
            cur_todos = [x for x in (ctx.get("todos") or []) if isinstance(x, dict)]
            with_ids = [x for x in new_todos if str(x.get("id") or "").strip()]
            without_ids = [x for x in new_todos if not str(x.get("id") or "").strip()]
            merged_todos = _upsert_by_id(cur_todos, with_ids)
            merged_todos.extend(without_ids)
            ctx["todos"] = merged_todos
            en = dict(ctx.get("enabled") or {})
            en["todos"] = bool(ctx["todos"])
            ctx["enabled"] = en
        return json_normalize(ctx)

    if et in ("tool_start", "tool_end"):
        tool_name = str(evt.get("tool") or "").strip()
        tool_obj = evt.get("input") if isinstance(evt.get("input"), dict) else {}
        call_id = str(tool_obj.get("id") or "").strip()
        status = "running" if et == "tool_start" else "done"
        cur = [x for x in (ctx.get("tools") or []) if isinstance(x, dict)]
        updated = False
        for x in cur:
            xid = str(x.get("id") or "").strip()
            if call_id and xid == call_id:
                x["status"] = status
                updated = True
                break
            if not call_id and tool_name and str(x.get("tool") or "").strip() == tool_name:
                x["status"] = status
                updated = True
                break
        if not updated and tool_name:
            cur.append({
                "id": call_id or None,
                "name": str(tool_obj.get("name") or "").strip() or tool_name,
                "tool": tool_name,
                "status": status,
                "displayName": str(tool_obj.get("name") or "").strip() or tool_name,
            })
        ctx["tools"] = cur
        en = dict(ctx.get("enabled") or {})
        en["tools"] = True
        ctx["enabled"] = en
        return json_normalize(ctx)

    return json_normalize(ctx)


def upsert_chat_room_start(
    *,
    tenant_id: str,
    room_id: str,
    participants: list[dict[str, Any]],
    room_name: str | None,
    primary_agent_id: str | None,
    context: dict[str, Any],
) -> None:
    tid = (tenant_id or "").strip()
    rid = (room_id or "").strip()
    if not tid or not rid:
        return
    sb = get_supabase()
    existing_resp = (
        sb.table("chat_rooms")
        .select("id,participants,name,primary_agent_id,context")
        .eq("id", rid)
        .execute()
    )
    existing: dict[str, Any] | None = None
    try:
        rows = getattr(existing_resp, "data", None) or []
        if rows and isinstance(rows[0], dict):
            existing = rows[0]
    except Exception:
        pass

    prev_participants = existing.get("participants") if isinstance(existing, dict) else None
    merged_participants: list[dict[str, Any]] = []
    seen_pids: set[str] = set()
    for p in list(prev_participants or []) + list(participants or []):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if pid and pid in seen_pids:
            continue
        merged_participants.append(p)
        if pid:
            seen_pids.add(pid)

    name_final = (room_name or "").strip() or (
        str(existing.get("name") or "").strip() if isinstance(existing, dict) else ""
    ) or None
    primary_final = (primary_agent_id or "").strip() or (
        str(existing.get("primary_agent_id") or "").strip() if isinstance(existing, dict) else ""
    ) or None
    prev_ctx = existing.get("context") if isinstance(existing, dict) else None
    if isinstance(prev_ctx, dict):
        new_ctx = context if isinstance(context, dict) else {}
        ctx_final: dict[str, Any] = {**prev_ctx, **new_ctx}
        for list_key in ("tools", "skills", "todos"):
            prev_list = prev_ctx.get(list_key)
            new_list = new_ctx.get(list_key)
            if isinstance(prev_list, list) and isinstance(new_list, list):
                if list_key == "skills":
                    prev_s = [str(s).strip() for s in prev_list if str(s).strip()]
                    new_s = [str(s).strip() for s in new_list if str(s).strip()]
                    ctx_final[list_key] = list(dict.fromkeys([*prev_s, *new_s]))
                else:
                    def _upsert_lists(prev: list, new: list) -> list:
                        by_id: dict[str, dict] = {}
                        order: list[str] = []
                        for x in prev:
                            if not isinstance(x, dict):
                                continue
                            xid = str(x.get("id") or "").strip()
                            if xid and xid not in by_id:
                                order.append(xid)
                            if xid:
                                by_id[xid] = dict(x)
                        no_id: list[dict] = []
                        for x in new:
                            if not isinstance(x, dict):
                                continue
                            xid = str(x.get("id") or "").strip()
                            if not xid:
                                no_id.append(x)
                            elif xid in by_id:
                                by_id[xid] = {**by_id[xid], **x}
                            else:
                                order.append(xid)
                                by_id[xid] = dict(x)
                        return [by_id[i] for i in order if i in by_id] + no_id
                    ctx_final[list_key] = _upsert_lists(prev_list, new_list)
    else:
        ctx_final = dict(context) if isinstance(context, dict) else build_initial_chat_room_context()
    ctx_final = json_normalize(ctx_final)

    if isinstance(existing, dict) and existing.get("id"):
        sb.table("chat_rooms").update({
            "participants": json_normalize(merged_participants),
            "name": name_final,
            "primary_agent_id": primary_final,
            "context": ctx_final,
            "tenant_id": tid,
        }).eq("id", rid).execute()
    else:
        sb.table("chat_rooms").insert(json_normalize({
            "id": rid,
            "participants": merged_participants,
            "message": None,
            "name": name_final,
            "primary_agent_id": primary_final,
            "tenant_id": tid,
            "context": ctx_final,
        })).execute()


def update_chat_room_context(
    *,
    tenant_id: str,
    room_id: str,
    evt: dict[str, Any],
    has_file: bool,
) -> None:
    tid = (tenant_id or "").strip()
    rid = (room_id or "").strip()
    if not tid or not rid:
        return
    sb = get_supabase()
    existing_resp = sb.table("chat_rooms").select("id,context").eq("id", rid).execute()
    existing: dict[str, Any] | None = None
    try:
        rows = getattr(existing_resp, "data", None) or []
        if rows and isinstance(rows[0], dict):
            existing = rows[0]
    except Exception:
        pass
    prev_ctx = existing.get("context") if isinstance(existing, dict) else None
    ctx0 = prev_ctx if isinstance(prev_ctx, dict) else build_initial_chat_room_context(has_file=has_file)
    ctx1 = merge_chat_room_context_from_event(ctx0, evt)
    if ctx1 is None:
        return
    sb.table("chat_rooms").update({"context": ctx1}).eq("id", rid).execute()


async def a_upsert_chat_room_start(**kwargs: Any) -> None:
    await asyncio.to_thread(upsert_chat_room_start, **kwargs)


async def a_update_chat_room_context(**kwargs: Any) -> None:
    await asyncio.to_thread(update_chat_room_context, **kwargs)
