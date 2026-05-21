"""Supabase client and DB helpers used by this service (HITL resume, agent loading, storage)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import uuid
from typing import Any

from dotenv import load_dotenv
from supabase import Client as SupabaseClient, create_client

logger = logging.getLogger(__name__)

load_dotenv()

_supabase: SupabaseClient | None = None


def get_supabase() -> SupabaseClient:
    """Return a lazily-initialised Supabase client (singleton)."""
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_KEY_URL")
        key = os.environ.get("SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY (or SERVICE_ROLE_KEY) are required")
        _supabase = create_client(url, key)
    return _supabase


def get_tenant_mcp_config(tenant_id: str) -> dict[str, Any] | None:
    """Fetch tenants.mcp JSON for a tenant (best-effort).

    tenants 스키마:
      create table public.tenants (..., mcp jsonb null, ...)
    """
    tid = (tenant_id or "").strip()
    if not tid:
        return None
    sb = get_supabase()
    try:
        resp = sb.table("tenants").select("mcp").eq("id", tid).single().execute()
    except Exception:
        logger.exception("get_tenant_mcp_config failed tenant_id=%s", tid, exc_info=True)
        return None
    row = getattr(resp, "data", None)
    if not isinstance(row, dict):
        return None
    mcp = row.get("mcp")
    return mcp if isinstance(mcp, dict) else None


def set_task_waiting_hitl(todo_id: str, run_state: dict[str, Any]) -> None:
    """Set task to waiting-for-human: store run_state in draft and draft_status to HUMAN_ASKED."""
    sb = get_supabase()
    existing = sb.table("todolist").select("draft").eq("id", todo_id).execute()
    draft = (existing.data or [{}])[0].get("draft") if existing.data else None
    if not isinstance(draft, dict):
        draft = {}
    merged = {**draft, "run_state": run_state}
    sb.table("todolist").update(
        # Important: when waiting for human, release the consumer lock so that
        # a resumer (this process or another) can safely pick it up later.
        {"draft": merged, "draft_status": "HUMAN_ASKED", "consumer": None},
    ).eq("id", todo_id).execute()
    logger.info("set_task_waiting_hitl ok todo_id=%s", todo_id)


def get_latest_human_response(todo_id: str) -> str | None:
    """Return the latest human_response event's answer for this todo_id, or None."""
    sb = get_supabase()
    resp = (
        sb.table("events")
        .select("data")
        .eq("todo_id", todo_id)
        .eq("event_type", "human_response")
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )
    if not resp.data or len(resp.data) == 0:
        return None
    data = resp.data[0].get("data")
    if isinstance(data, dict) and "answer" in data:
        return data["answer"]
    return None


def claim_resumable_hitl_task(*, agent_orch: str, consumer: str, limit_scan: int = 25) -> dict[str, Any] | None:
    """Claim a waiting HITL task that has an approved human_response.

    This is an auxiliary resumption path and does NOT rely on fetch_pending_task RPC,
    because that RPC intentionally excludes HUMAN_ASKED.

    Policy:
    - Only consider todolist rows with status=IN_PROGRESS and draft_status=HUMAN_ASKED
      for this agent_orch.
    - Only resume when there exists at least one events row:
      event_type=human_response AND status=APPROVED for the same todo_id.
    - Claim by setting consumer=<this consumer> and draft_status=STARTED.
      (run_state remains in draft and will be used by the executor.)
    """
    agent_orch = (agent_orch or "").strip()
    consumer = (consumer or "").strip()
    if not agent_orch or not consumer:
        return None

    sb = get_supabase()
    cand_resp = (
        sb.table("todolist")
        .select("*")
        .eq("agent_orch", agent_orch)
        .eq("status", "IN_PROGRESS")
        .eq("draft_status", "HUMAN_ASKED")
        .order("updated_at", desc=False)
        .limit(int(limit_scan))
        .execute()
    )
    candidates = cand_resp.data or []
    if not candidates:
        return None

    todo_ids = [str(r.get("id")) for r in candidates if r.get("id")]
    if not todo_ids:
        return None

    # Find which candidates have an approved human_response.
    ev_resp = (
        sb.table("events")
        .select("todo_id")
        .in_("todo_id", todo_ids)
        .eq("event_type", "human_response")
        .eq("status", "APPROVED")
        .order("timestamp", desc=True)
        .limit(len(todo_ids) * 3)
        .execute()
    )
    approved_set = {str(r.get("todo_id")) for r in (ev_resp.data or []) if r.get("todo_id")}
    if not approved_set:
        return None

    # Pick the oldest waiting task that has an approved response.
    chosen_id: str | None = None
    for r in candidates:
        tid = str(r.get("id") or "")
        if tid and tid in approved_set:
            chosen_id = tid
            break
    if not chosen_id:
        return None

    # Claim it for processing. We intentionally do not require consumer to be NULL here
    # because older versions stored HUMAN_ASKED without clearing consumer.
    try:
        sb.table("todolist").update({"consumer": consumer, "draft_status": "STARTED"}).eq("id", chosen_id).eq(
            "draft_status", "HUMAN_ASKED"
        ).execute()
    except Exception:
        logger.exception("claim_resumable_hitl_task: failed to claim todo_id=%s", chosen_id, exc_info=True)
        return None

    # Return the fresh row.
    row_resp = sb.table("todolist").select("*").eq("id", chosen_id).single().execute()
    row = getattr(row_resp, "data", None) or None
    if not isinstance(row, dict):
        return None
    logger.info("claim_resumable_hitl_task ok todo_id=%s consumer=%s", chosen_id, consumer)
    return row


def get_todolist_draft_status(todo_id: str) -> str | None:
    """Fetch latest draft_status for a todolist row (best-effort)."""
    tid = (todo_id or "").strip()
    if not tid:
        return None
    sb = get_supabase()
    resp = sb.table("todolist").select("draft_status").eq("id", tid).single().execute()
    row = getattr(resp, "data", None)
    if not isinstance(row, dict):
        return None
    val = row.get("draft_status")
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def release_consumer_if_cancelled(*, todo_id: str, consumer: str | None) -> bool:
    """If the task is CANCELLED, clear consumer to avoid a stuck claim lock.

    Returns True when an update attempt was made successfully, False otherwise.
    """
    tid = (todo_id or "").strip()
    if not tid:
        return False
    sb = get_supabase()
    try:
        q = sb.table("todolist").update({"consumer": None}).eq("id", tid).eq("draft_status", "CANCELLED")
        if (consumer or "").strip():
            q = q.eq("consumer", str(consumer).strip())
        q.execute()
        return True
    except Exception:
        logger.exception("release_consumer_if_cancelled failed todo_id=%s", tid, exc_info=True)
        return False


# --- HTTP chat persistence (chat_rooms/chats/chat_attachments) ---

_CHAT_FILE_PATH_KEYS: tuple[str, ...] = (
    "fullPath",
    "full_path",
    "publicUrl",
    "public_url",
    "fileUrl",
    "file_url",
    "signedUrl",
    "signed_url",
    "url",
    "href",
    "path",
    "file_path",
    "filePath",
    "uri",
    "src",
    "location",
)


def _json_normalize(obj: Any) -> Any:
    """Best-effort JSONB-safe normalization (never raises)."""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        logger.exception("JSON-normalize failed (ignored)", exc_info=True)
        return obj


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_file_name(file_obj: dict[str, Any]) -> str | None:
    for k in ("fileName", "name", "filename", "file_name"):
        v = file_obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _extract_file_path(file_obj: dict[str, Any]) -> str | None:
    for k in _CHAT_FILE_PATH_KEYS:
        v = file_obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def build_chat_attachment_rows(
    *,
    tenant_id: str,
    chat_room_id: str,
    user_name: str,
    file: dict[str, Any] | None,
    files: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Convert request file payloads into `chat_attachments` insert rows."""
    tid = (tenant_id or "").strip()
    rid = (chat_room_id or "").strip()
    if not tid or not rid:
        return []

    out: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    if isinstance(file, dict):
        items.append(file)
    if isinstance(files, list):
        items.extend([x for x in files if isinstance(x, dict)])

    # Dedup by (file_path, file_name) best-effort.
    seen: set[str] = set()
    for f in items:
        fname = _extract_file_name(f)
        fpath = _extract_file_path(f)
        key = f"{fpath or ''}||{fname or ''}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": str(uuid.uuid4()),
                "file_name": fname,
                "file_path": fpath,
                "chat_room_id": rid,
                "user_name": (user_name or "").strip() or None,
                "tenant_id": tid,
            }
        )
    return _json_normalize(out)


def _merge_participants(existing: Any, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base: list[dict[str, Any]] = []
    if isinstance(existing, list):
        for x in existing:
            if isinstance(x, dict):
                base.append(dict(x))

    def _pid(p: dict[str, Any]) -> str:
        return str(p.get("id") or "").strip()

    by_id: dict[str, dict[str, Any]] = {_pid(p): p for p in base if _pid(p)}
    for p in incoming:
        pid = _pid(p)
        if pid and pid in by_id:
            by_id[pid] = {**by_id[pid], **p}
        elif pid:
            by_id[pid] = dict(p)
        else:
            base.append(dict(p))

    # Stable ordering: existing order first, then new ones by insertion.
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for p in base:
        pid = _pid(p)
        if pid and pid in by_id and pid not in seen_ids:
            out.append(by_id[pid])
            seen_ids.add(pid)
        elif not pid:
            out.append(p)
    for pid, p in by_id.items():
        if pid and pid not in seen_ids:
            out.append(p)
            seen_ids.add(pid)
    return out


def build_initial_chat_room_context(*, has_file: bool) -> dict[str, Any]:
    # Follow the observed frontend shape.
    return _json_normalize(
        {
            "todos": [],
            "tools": [],
            "skills": [],
            # NOTE: keep the established production keys only.
            "enabled": {"todos": False, "tools": True, "skills": False},
            "updatedAt": _now_iso(),
            "orchestration": "deepagents",
        }
    )


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
        disp = t.get("displayName") if isinstance(t.get("displayName"), str) else None
        if disp is None and isinstance(t.get("display_name"), str):
            disp = t.get("display_name")
        disp = disp.strip() if isinstance(disp, str) and disp.strip() else ""
        if not (tid or name or tool):
            continue
        out.append(
            {
                "id": tid or None,
                "name": name,
                "tool": tool,
                "status": "planned",
                "displayName": disp or name or tool,
            }
        )
    return out


def merge_chat_room_context_from_event(existing: Any, evt: dict[str, Any]) -> dict[str, Any] | None:
    """Pure merge: take current `context` and one SSE event -> new context (or None if irrelevant)."""
    if not isinstance(evt, dict):
        return None
    et = str(evt.get("type") or "").strip()
    if et not in ("plan_tools", "plan_skills", "plan_todos", "tool_start", "tool_end"):
        return None

    ctx: dict[str, Any] = dict(existing) if isinstance(existing, dict) else build_initial_chat_room_context(has_file=False)
    ctx.setdefault("todos", [])
    ctx.setdefault("tools", [])
    ctx.setdefault("skills", [])
    ctx.setdefault("enabled", {"todos": False, "tools": True, "skills": False})
    # Keep orchestration if present; do not force-inject for legacy rows.
    if "orchestration" in ctx:
        ctx["orchestration"] = str(ctx.get("orchestration") or "deepagents")
    ctx["updatedAt"] = _now_iso()

    # Upsert helpers
    def upsert_list_by_id(lst: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        cur_tools = ctx.get("tools") if isinstance(ctx.get("tools"), list) else []
        cur_tools2 = [x for x in cur_tools if isinstance(x, dict)]
        # Prefer id-based upsert; if missing ids, append.
        with_ids = [x for x in planned if str(x.get("id") or "").strip()]
        without_ids = [x for x in planned if not str(x.get("id") or "").strip()]
        merged = upsert_list_by_id(cur_tools2, with_ids)
        merged.extend(without_ids)
        ctx["tools"] = merged
        en = ctx.get("enabled") if isinstance(ctx.get("enabled"), dict) else {}
        en = dict(en)
        en["tools"] = True
        ctx["enabled"] = en
        return _json_normalize(ctx)

    if et == "plan_skills":
        skills = evt.get("skills")
        if isinstance(skills, list):
            cur = ctx.get("skills") if isinstance(ctx.get("skills"), list) else []
            cur_s = [str(s).strip() for s in cur if isinstance(s, str) and str(s).strip()]
            new_s = [str(s).strip() for s in skills if isinstance(s, str) and str(s).strip()]
            ctx["skills"] = list(dict.fromkeys([*cur_s, *new_s]))
            en = ctx.get("enabled") if isinstance(ctx.get("enabled"), dict) else {}
            en = dict(en)
            en["skills"] = bool(ctx["skills"])
            ctx["enabled"] = en
        return _json_normalize(ctx)

    if et == "plan_todos":
        todos = evt.get("todos")
        if isinstance(todos, list):
            ctx["todos"] = [x for x in todos if isinstance(x, dict)]
            en = ctx.get("enabled") if isinstance(ctx.get("enabled"), dict) else {}
            en = dict(en)
            en["todos"] = bool(ctx["todos"])
            ctx["enabled"] = en
        return _json_normalize(ctx)

    # tool_start/tool_end: best-effort map to tool status via tool id if present.
    if et in ("tool_start", "tool_end"):
        tool_name = str(evt.get("tool") or "").strip()
        tool_obj = evt.get("input") if isinstance(evt.get("input"), dict) else {}
        call_id = str(tool_obj.get("id") or "").strip()
        status = "running" if et == "tool_start" else "done"
        cur_tools = ctx.get("tools") if isinstance(ctx.get("tools"), list) else []
        cur_tools2 = [x for x in cur_tools if isinstance(x, dict)]
        updated = False
        for x in cur_tools2:
            xid = str(x.get("id") or "").strip()
            if call_id and xid == call_id:
                x["status"] = status
                updated = True
                break
            # Fallback: match on tool name
            if not call_id and tool_name and str(x.get("tool") or "").strip() == tool_name:
                x["status"] = status
                updated = True
                break
        if not updated and tool_name:
            cur_tools2.append(
                {
                    "id": call_id or None,
                    "name": str(tool_obj.get("name") or "").strip() or tool_name,
                    "tool": tool_name,
                    "status": status,
                    "displayName": str(tool_obj.get("name") or "").strip() or tool_name,
                }
            )
        ctx["tools"] = cur_tools2
        en = ctx.get("enabled") if isinstance(ctx.get("enabled"), dict) else {}
        en = dict(en)
        en["tools"] = True
        ctx["enabled"] = en
        return _json_normalize(ctx)

    return _json_normalize(ctx)


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
    existing_resp = sb.table("chat_rooms").select("id,participants,name,primary_agent_id,context").eq("id", rid).execute()
    existing = None
    try:
        rows = getattr(existing_resp, "data", None) or []
        if rows and isinstance(rows, list) and isinstance(rows[0], dict):
            existing = rows[0]
    except Exception:
        existing = None

    merged_participants = _merge_participants(existing.get("participants") if isinstance(existing, dict) else None, participants)
    name_final = (room_name or "").strip() or (str(existing.get("name") or "").strip() if isinstance(existing, dict) else "") or None
    primary_final = (primary_agent_id or "").strip() or (
        str(existing.get("primary_agent_id") or "").strip() if isinstance(existing, dict) else ""
    ) or None
    prev_ctx = existing.get("context") if isinstance(existing, dict) else None
    ctx_final: dict[str, Any]
    if isinstance(prev_ctx, dict):
        ctx_final = {**prev_ctx, **(context if isinstance(context, dict) else {})}
    else:
        ctx_final = dict(context) if isinstance(context, dict) else build_initial_chat_room_context(has_file=False)
    ctx_final = _json_normalize(ctx_final)

    if isinstance(existing, dict) and existing.get("id"):
        sb.table("chat_rooms").update(
            {
                "participants": _json_normalize(merged_participants),
                "name": name_final,
                "primary_agent_id": primary_final,
                "context": ctx_final,
                "tenant_id": tid,
            }
        ).eq("id", rid).execute()
    else:
        sb.table("chat_rooms").insert(
            _json_normalize(
                {
                    "id": rid,
                    "participants": merged_participants,
                    "message": None,
                    "name": name_final,
                    "primary_agent_id": primary_final,
                    "tenant_id": tid,
                    "context": ctx_final,
                }
            )
        ).execute()


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
    existing = None
    try:
        rows = getattr(existing_resp, "data", None) or []
        if rows and isinstance(rows, list) and isinstance(rows[0], dict):
            existing = rows[0]
    except Exception:
        existing = None
    prev_ctx = existing.get("context") if isinstance(existing, dict) else None
    ctx0 = prev_ctx if isinstance(prev_ctx, dict) else build_initial_chat_room_context(has_file=has_file)
    ctx1 = merge_chat_room_context_from_event(ctx0, evt)
    if ctx1 is None:
        return
    sb.table("chat_rooms").update({"context": ctx1}).eq("id", rid).execute()


def update_chat_room_last_message(
    *,
    tenant_id: str,
    room_id: str,
    message: dict[str, Any] | None,
) -> None:
    tid = (tenant_id or "").strip()
    rid = (room_id or "").strip()
    if not tid or not rid:
        return
    if not isinstance(message, dict):
        return
    sb = get_supabase()
    sb.table("chat_rooms").update({"message": _json_normalize(message)}).eq("id", rid).execute()


def insert_chat_message(
    *,
    tenant_id: str,
    room_id: str,
    thread_id: str | None,
    message_obj: dict[str, Any],
    message_uuid: str | None = None,
) -> str:
    tid = (tenant_id or "").strip()
    rid = (room_id or "").strip()
    if not tid or not rid:
        return ""
    sb = get_supabase()
    uid = (message_uuid or "").strip() or str(uuid.uuid4())
    row = _json_normalize(
        {
            "uuid": uid,
            "id": rid,
            "messages": message_obj,
            "tenant_id": tid,
            "thread_id": (thread_id or "").strip() or None,
        }
    )
    sb.table("chats").insert(row).execute()
    return uid


def update_chat_message(
    *,
    tenant_id: str,
    message_uuid: str,
    message_obj: dict[str, Any],
) -> None:
    """Update an existing `chats` row's `messages` JSONB by uuid (best-effort)."""
    tid = (tenant_id or "").strip()
    mid = (message_uuid or "").strip()
    if not tid or not mid:
        return
    if not isinstance(message_obj, dict):
        return
    sb = get_supabase()
    sb.table("chats").update({"messages": _json_normalize(message_obj), "tenant_id": tid}).eq("uuid", mid).execute()


def insert_chat_attachments(
    *,
    tenant_id: str,
    room_id: str,
    user_name: str,
    file: dict[str, Any] | None,
    files: list[dict[str, Any]] | None,
) -> list[str]:
    rows = build_chat_attachment_rows(
        tenant_id=tenant_id,
        chat_room_id=room_id,
        user_name=user_name,
        file=file,
        files=files,
    )
    if not rows:
        return []
    sb = get_supabase()
    sb.table("chat_attachments").insert(rows).execute()
    ids: list[str] = []
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            ids.append(r["id"])
    return ids


async def a_upsert_chat_room_start(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(upsert_chat_room_start, **kwargs)


async def a_update_chat_room_context(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(update_chat_room_context, **kwargs)


async def a_update_chat_room_last_message(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(update_chat_room_last_message, **kwargs)


async def a_insert_chat_message(**kwargs: Any) -> str:
    import asyncio

    return await asyncio.to_thread(insert_chat_message, **kwargs)


async def a_update_chat_message(**kwargs: Any) -> None:
    import asyncio

    await asyncio.to_thread(update_chat_message, **kwargs)


async def a_insert_chat_attachments(**kwargs: Any) -> list[str]:
    import asyncio

    return await asyncio.to_thread(insert_chat_attachments, **kwargs)


# --- Public Storage (Supabase Storage ``files`` bucket + public URLs) ---


def public_files_bucket_name() -> str:
    return (os.environ.get("SUPABASE_PUBLIC_FILES_BUCKET") or "files").strip() or "files"


def is_public_storage_upload_enabled() -> bool:
    raw = (os.environ.get("DISABLE_SUPABASE_STORAGE_UPLOAD") or "").strip().lower()
    return raw not in ("1", "true", "yes")


def _sanitize_storage_path_segment(s: str, *, max_len: int = 96) -> str:
    t = (s or "").strip().replace("/", "_").replace("\\", "_")
    t = re.sub(r"[^a-zA-Z0-9._-]+", "_", t).strip("._-") or "x"
    return t[:max_len]


def build_deliverable_object_path(
    *,
    filename: str,
    tenant_id: str | None,
    task_id: str | None,
    proc_inst_id: str | None,
) -> str:
    """Stable, unique object key: ``{tenant}/{proc}/{task}/{uuid}_{basename}``."""
    base = os.path.basename((filename or "").strip()) or "artifact.bin"
    safe_name = _sanitize_storage_path_segment(base, max_len=180)
    uid = uuid.uuid4().hex[:16]
    t = _sanitize_storage_path_segment(tenant_id or "no-tenant", max_len=64)
    tid = _sanitize_storage_path_segment(task_id or "no-task", max_len=64)
    pid = _sanitize_storage_path_segment(proc_inst_id or "no-proc", max_len=64)
    return f"{t}/{pid}/{tid}/{uid}_{safe_name}"


def upload_bytes_to_public_files_bucket(
    *,
    object_path: str,
    data: bytes,
    content_type: str | None = None,
) -> str:
    """Upload object and return its **public** URL. Raises on API failure."""
    bucket = public_files_bucket_name()
    sb = get_supabase()
    opts: dict[str, Any] = {"upsert": "true"}
    if content_type:
        opts["content-type"] = content_type
    else:
        opts["content-type"] = "application/octet-stream"
    sb.storage.from_(bucket).upload(
        object_path,
        data,
        file_options=opts,
    )
    url = sb.storage.from_(bucket).get_public_url(object_path)
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("storage get_public_url returned empty")
    return url.strip()


def try_upload_bytes_to_public_files(
    *,
    object_path: str,
    data: bytes,
    content_type: str | None = None,
) -> str | None:
    """Best-effort upload; logs and returns ``None`` on failure."""
    if not is_public_storage_upload_enabled():
        return None
    try:
        return upload_bytes_to_public_files_bucket(
            object_path=object_path,
            data=data,
            content_type=content_type,
        )
    except Exception:
        logger.exception(
            "Supabase public storage upload failed | bucket=%s path=%s",
            public_files_bucket_name(),
            object_path,
        )
        return None


def get_proc_def_definition(tenant_id: str, proc_def_id: str) -> dict[str, Any] | None:
    """Fetch the proc_def.definition dictionary for given tenant_id and proc_def_id."""
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    sb = get_supabase()
    resp = (
        sb.table("proc_def")
        .select("definition")
        .eq("tenant_id", tid)
        .eq("id", pdid)
        .limit(1)
        .execute()
    )
    rows = getattr(resp, "data", None)
    if not isinstance(rows, list) or not rows:
        return None
    row0 = rows[0]
    if isinstance(row0, dict):
        return row0.get("definition")
    return None


def get_adhoc_todolist_by_root_proc_inst(
    *,
    tenant_id: str,
    root_proc_inst_id: str,
) -> list[dict[str, Any]]:
    """Query todolist table rows with matching tenant_id, root_proc_inst_id, and adhoc=True."""
    tid = (tenant_id or "").strip()
    rpid = (root_proc_inst_id or "").strip()
    if not (tid and rpid):
        return []
    sb = get_supabase()
    resp = (
        sb.table("todolist")
        .select("*")
        .eq("tenant_id", tid)
        .eq("root_proc_inst_id", rpid)
        .eq("adhoc", True)
        .execute()
    )
    return resp.data or []


def _extract_activity_tools_skills_from_definition(
    definition: Any,
    *,
    activity_id: str,
) -> tuple[list[str], list[str]]:
    """Best-effort extraction of tools/skills from proc_def_version.definition JSON.

    New spec shape (expected):
      definition.activities = [ { "id": "<activity_id>", "tools": [...], "skills": [...] }, ... ]
    """
    aid = (activity_id or "").strip()
    if not aid:
        return ([], [])
    if not isinstance(definition, dict):
        return ([], [])
    activities = definition.get("activities")
    if not isinstance(activities, list) or not activities:
        return ([], [])

    target: dict[str, Any] | None = None
    for act in activities:
        if not isinstance(act, dict):
            continue
        for key in ("id", "activity_id", "activityId", "key"):
            if str(act.get(key) or "").strip() == aid:
                target = act
                break
        if target is not None:
            break
    if target is None:
        return ([], [])

    def _as_str_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        if isinstance(value, list):
            out: list[str] = []
            for x in value:
                s = str(x or "").strip()
                if s:
                    out.append(s)
            return out
        return []

    tools = list(dict.fromkeys(_as_str_list(target.get("tools"))))
    skills = list(dict.fromkeys(_as_str_list(target.get("skills"))))
    return (tools, skills)


def get_form_def_fields_json(form_id: str) -> list[dict[str, Any]] | None:
    """Fetch fields_json from the form_def table for the given form ID."""
    fid = (form_id or "").strip()
    if not fid:
        return None
    sb = get_supabase()
    try:
        resp = sb.table("form_def").select("fields_json").eq("id", fid).single().execute()
    except Exception:
        logger.exception("get_form_def_fields_json failed form_id=%s", fid, exc_info=True)
        return None
    row = getattr(resp, "data", None)
    if not isinstance(row, dict):
        return None
    fields = row.get("fields_json")
    if isinstance(fields, list):
        return fields
    return None


def _load_proc_def_activity_capabilities(
    *,
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
) -> tuple[list[str], list[str]]:
    """Fetch proc_def.definition and extract activity.tools/skills (best-effort)."""
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    aid = (activity_id or "").strip()
    if not (tid and pdid and aid):
        return ([], [])
    try:
        definition = get_proc_def_definition(tid, pdid)
    except Exception:
        logger.exception(
            "proc_def lookup failed | tenant_id=%s proc_def_id=%s activity_id=%s",
            tid,
            pdid,
            aid,
            exc_info=True,
        )
        return ([], [])
    return _extract_activity_tools_skills_from_definition(definition, activity_id=aid)



