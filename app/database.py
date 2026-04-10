"""Supabase client and DB helpers used by this service (HITL resume, agent loading, storage)."""

from __future__ import annotations

import logging
import os
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
