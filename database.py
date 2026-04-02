"""Supabase client and DB operations: polling, events, task updates, context fetches."""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from typing import Any

from dotenv import load_dotenv
from supabase import Client as SupabaseClient, create_client

logger = logging.getLogger(__name__)

load_dotenv()

_supabase: SupabaseClient | None = None

CONSUMER_ID = f"deepagents-{uuid.uuid4().hex[:8]}"


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


def get_consumer_id() -> str:
    """Consumer identifier for polling (env or hostname:pid)."""
    if os.environ.get("CONSUMER_ID"):
        return os.environ["CONSUMER_ID"]
    return f"{socket.gethostname()}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def poll_pending_todo(
    agent_orch: str,
    consumer: str | None = None,
    env: str | None = None,
    limit: int = 1,
) -> dict[str, Any] | None:
    """Claim one pending task via fetch_pending_task RPC. Returns the row or None."""
    sb = get_supabase()
    p_env = (env or os.environ.get("ENV") or "").lower()
    if p_env != "dev":
        p_env = "prod"
    resp = sb.rpc(
        "fetch_pending_task",
        {
            "p_agent_orch": agent_orch,
            "p_consumer": consumer or get_consumer_id(),
            "p_limit": limit,
            "p_env": p_env,
        },
    ).execute()
    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]
    if row.get("feedback") in ([], {}):
        row["feedback"] = None
    if row.get("output") in ([], {}):
        row["output"] = None
    if row.get("draft") in ([], {}):
        row["draft"] = None
    return row


def fetch_pending_tasks(
    agent_orch: str,
    *,
    consumer: str | None = None,
    limit: int = 5,
    env: str = "dev",
) -> list[dict[str, Any]]:
    """Call fetch_pending_task RPC to atomically claim pending work items."""
    sb = get_supabase()
    resp = sb.rpc(
        "fetch_pending_task",
        {
            "p_agent_orch": agent_orch,
            "p_consumer": consumer or CONSUMER_ID,
            "p_limit": limit,
            "p_env": env,
        },
    ).execute()
    tasks = resp.data or []
    if tasks:
        logger.info("Claimed %d pending task(s) [consumer=%s]", len(tasks), consumer or CONSUMER_ID)
    return tasks


# ---------------------------------------------------------------------------
# Events table (public.events)
# ---------------------------------------------------------------------------
# event_type_enum: task_started, task_completed, tool_usage_started, tool_usage_finished,
#                  crew_completed, human_asked, human_response, human_checked, task_working, error
# event_status: ASKED, APPROVED, REJECTED (or null)


def insert_event(
    *,
    id: str,
    job_id: str,
    todo_id: str | None = None,
    proc_inst_id: str | None = None,
    event_type: str,
    status: str | None = None,
    crew_type: str | None = None,
    data: dict[str, Any] | list[Any] | str | None = None,
) -> None:
    """Insert one row into public.events. data is sent as JSON object so DB stores jsonb."""
    sb = get_supabase()
    if data is None:
        data = {}
    if not isinstance(data, (dict, list)):
        data = {"value": str(data)}
    # Ensure correlation_id is always present for tracing; when missing and data is a dict,
    # fall back to job_id so existing callers automatically gain a minimal correlation key.
    if isinstance(data, dict) and "correlation_id" not in data:
        data.setdefault("correlation_id", job_id)
    payload = {
        "id": id,
        "job_id": job_id,
        "todo_id": todo_id,
        "proc_inst_id": proc_inst_id,
        "event_type": event_type,
        "status": status,
        "crew_type": crew_type,
        "data": data,
    }
    sb.table("events").insert(payload).execute()
    logger.info("insert_event ok event_type=%s todo_id=%s", event_type, todo_id)


def insert_events_bulk(payloads: list[dict[str, Any]]) -> None:
    """Insert multiple rows into public.events. data kept as dict/list for jsonb."""
    if not payloads:
        return
    sb = get_supabase()
    for p in payloads:
        if p.get("data") is not None and not isinstance(p.get("data"), (dict, list)):
            p["data"] = {"value": str(p["data"])}
    sb.table("events").insert(payloads).execute()
    logger.info("insert_events_bulk ok count=%d", len(payloads))


# ---------------------------------------------------------------------------
# Task status / result
# ---------------------------------------------------------------------------


def update_task_error(todo_id: str) -> None:
    """Mark todolist row as FAILED and clear consumer."""
    sb = get_supabase()
    sb.table("todolist").update({"draft_status": "FAILED", "consumer": None}).eq("id", todo_id).execute()
    logger.info("update_task_error ok todo_id=%s", todo_id)


def save_task_result(todo_id: str, payload: Any, final: bool = False) -> None:
    """Write agent result into todolist (draft / draft_status) as proper JSON.

    - If payload is a dict/list, it is sent as-is so that Supabase stores it
      into the jsonb `draft` column as a JSON object/array.
    - Other types are stored as-is; callers that need JSON should pass a
      dict/list payload.
    """
    sb = get_supabase()
    draft = payload
    draft_status = "COMPLETED" if final else "STARTED"
    sb.table("todolist").update({"draft": draft, "draft_status": draft_status}).eq("id", todo_id).execute()
    logger.info("save_task_result ok todo_id=%s final=%s", todo_id, final)


def update_task_draft(
    task_id: str | int,
    tenant_id: str,
    draft: str,
    draft_status: str,
) -> None:
    """Write the agent result back into the todolist row."""
    sb = get_supabase()
    sb.table("todolist").update(
        {"draft": draft, "draft_status": draft_status},
    ).eq("id", task_id).eq("tenant_id", tenant_id).execute()
    logger.info("Updated task %s → draft_status=%s", task_id, draft_status)


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


# ---------------------------------------------------------------------------
# Context preparation (for executor extras)
# ---------------------------------------------------------------------------


def fetch_form_def(tool_val: str, tenant_id: str) -> tuple[str, list[dict[str, Any]], str | None]:
    """Return (form_id, form_fields, form_html)."""
    form_id = (tool_val or "").replace("formHandler:", "", 1)
    sb = get_supabase()
    resp = (
        sb.table("form_def")
        .select("fields_json, html")
        .eq("id", form_id)
        .eq("tenant_id", tenant_id or "")
        .execute()
    )
    data = resp.data or []
    if not data:
        return (
            form_id or "freeform",
            [{"key": "freeform", "type": "textarea", "text": "자유형식 입력", "placeholder": "원하는 내용을 자유롭게 입력해주세요."}],
            None,
        )
    row = data[0]
    fields = row.get("fields_json") or [{"key": "freeform", "type": "textarea", "text": "자유형식 입력"}]
    return (form_id or "freeform", fields, row.get("html"))


def fetch_users_grouped(user_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (agents, users) from users table for given ids (is_agent split)."""
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return ([], [])
    sb = get_supabase()
    resp = sb.table("users").select("*").in_("id", ids).execute()
    rows = resp.data or []
    agents, users = [], []
    for r in rows:
        if r.get("is_agent") is True:
            agents.append(r)
        else:
            users.append(r)
    return (agents, users)


def fetch_agents_by_user_ids(user_ids: list[str], tenant_id: str) -> list[dict[str, Any]]:
    """Return agent rows from users table for given user ids and tenant (is_agent=true)."""
    ids = [u.strip() for u in (user_ids or []) if u and u.strip()]
    if not ids or not tenant_id:
        return []
    sb = get_supabase()
    resp = (
        sb.table("users")
        .select("*")
        .in_("id", ids)
        .eq("tenant_id", tenant_id)
        .eq("is_agent", True)
        .execute()
    )
    rows = resp.data or []
    logger.info("fetch_agents_by_user_ids: %d agent(s) for tenant=%s", len(rows), tenant_id)
    return rows


def fetch_email_users_by_proc_inst_id(proc_inst_id: str) -> str:
    """Comma-separated emails for human users in this proc_inst."""
    if not proc_inst_id:
        return ""
    sb = get_supabase()
    tl = sb.table("todolist").select("user_id").eq("proc_inst_id", proc_inst_id).execute()
    ids_set = set()
    for row in (tl.data or []):
        for uid in (row.get("user_id") or "").split(","):
            u = uid.strip()
            if u:
                ids_set.add(u)
    if not ids_set:
        return ""
    ur = (
        sb.table("users")
        .select("id, email, is_agent")
        .in_("id", list(ids_set))
        .eq("is_agent", False)
        .execute()
    )
    emails = [u.get("email", "").strip() for u in (ur.data or []) if u.get("email")]
    return ",".join(sorted(set(emails)))


def fetch_tenant_mcp(tenant_id: str) -> dict[str, Any] | None:
    """Return tenant MCP config or None."""
    if not tenant_id:
        return None
    sb = get_supabase()
    resp = sb.table("tenants").select("mcp").eq("id", tenant_id).single().execute()
    if not getattr(resp, "data", None):
        return None
    return resp.data.get("mcp")


def fetch_proc_inst_sources(proc_inst_id: str) -> list[dict[str, Any]]:
    """Return proc_inst_source rows for the given proc_inst_id."""
    if not proc_inst_id:
        return []
    sb = get_supabase()
    resp = sb.table("proc_inst_source").select("*").eq("proc_inst_id", proc_inst_id).execute()
    return resp.data or []
