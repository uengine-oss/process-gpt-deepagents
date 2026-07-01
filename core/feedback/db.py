"""core/feedback/db.py — 피드백 처리용 DB 접근 함수.

Supabase 클라이언트는 core.storage.db.get_supabase()를 재사용한다.
"""

from __future__ import annotations

import logging
from typing import Any

from core.storage.db import get_supabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 피드백 작업 조회 / 상태 업데이트
# ---------------------------------------------------------------------------

async def fetch_feedback_task(limit: int = 1) -> dict[str, Any] | None:
    """DONE 상태이면서 feedback이 있는 작업을 원자적으로 가져온다."""
    try:
        sb = get_supabase()
        resp = sb.rpc("agent_feedback_task", {"p_limit": limit}).execute()
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception:
        logger.warning("피드백 작업 조회 실패", exc_info=True)
        return None


async def update_feedback_status(todo_id: str, status: str) -> bool:
    try:
        sb = get_supabase()
        sb.table("todolist").update({"feedback_status": status}).eq("id", todo_id).execute()
        return True
    except Exception:
        logger.warning("피드백 상태 업데이트 실패: todo_id=%s", todo_id, exc_info=True)
        return False


def fetch_child_activity_todo(root_proc_inst_id: str, activity_id: str) -> dict | None:
    """root_proc_inst_id + activity_id로 child activity todo를 조회한다.

    반환: {"id": str, "proc_inst_id": str | None, "form_id": str | None} 또는 None.
    form_id는 todolist.tool 컬럼의 "formHandler:<id>" 값에서 파싱된다.
    """
    rpid = (root_proc_inst_id or "").strip()
    aid = (activity_id or "").strip()
    if not (rpid and aid):
        return None
    try:
        sb = get_supabase()
        resp = (
            sb.table("todolist")
            .select("id, proc_inst_id, tool")
            .eq("root_proc_inst_id", rpid)
            .eq("activity_id", aid)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        row = rows[0]
        tool_val = str(row.get("tool") or "")
        form_id = tool_val.replace("formHandler:", "", 1) if tool_val.startswith("formHandler:") else None
        return {"id": row["id"], "proc_inst_id": row.get("proc_inst_id"), "form_id": form_id}
    except Exception:
        logger.warning(
            "child activity todo 조회 실패: root_proc_inst_id=%s activity_id=%s",
            rpid, aid, exc_info=True,
        )
        return None


async def fetch_events_by_todo_id(todo_id: str) -> list[dict[str, Any]]:
    try:
        sb = get_supabase()
        resp = (
            sb.table("events")
            .select("*")
            .eq("todo_id", todo_id)
            .order("timestamp", desc=False)
            .execute()
        )
        return resp.data or []
    except Exception:
        logger.warning("이벤트 로그 조회 실패: todo_id=%s", todo_id, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# 에이전트 정보 조회
# ---------------------------------------------------------------------------

def get_agent_by_id(agent_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    resp = sb.table("users").select("*").eq("id", agent_id).execute()
    if resp.data and resp.data[0].get("is_agent") and resp.data[0].get("agent_type") == "agent":
        agent = resp.data[0]
        agent["name"] = agent.get("username", "")
        return agent
    return None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """agent 여부와 무관하게 users 테이블에서 사용자를 조회한다 (피드백 작성자 조회용)."""
    if not user_id:
        return None
    try:
        sb = get_supabase()
        resp = sb.table("users").select("*").eq("id", user_id).execute()
        return resp.data[0] if resp.data else None
    except Exception:
        logger.warning("사용자 조회 실패: user_id=%s", user_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 스킬 동기화 (users.skills + tenants.skills + agent_skills)
# ---------------------------------------------------------------------------

def _parse_comma_skills(text: str | None) -> list[str]:
    if not text:
        return []
    return [s.strip() for s in text.split(",") if s.strip()]


def update_agent_and_tenant_skills(agent_id: str, skill_name: str, operation: str) -> None:
    sb = get_supabase()
    op = (operation or "").upper()

    resp = sb.table("users").select("id, tenant_id, skills").eq("id", agent_id).single().execute()
    user = resp.data if resp.data else None
    if not user:
        return

    tenant_id = user.get("tenant_id")
    user_skills = _parse_comma_skills(user.get("skills"))

    if op == "CREATE" and skill_name not in user_skills:
        user_skills.append(skill_name)
    elif op == "DELETE":
        user_skills = [s for s in user_skills if s != skill_name]

    new_text = ",".join(sorted(set(user_skills))) if user_skills else None
    sb.table("users").update({"skills": new_text}).eq("id", agent_id).execute()

    if not tenant_id:
        return

    t_resp = sb.table("tenants").select("id, skills").eq("id", tenant_id).single().execute()
    tenant = t_resp.data if t_resp.data else None
    if not tenant:
        return

    t_skills: list[str] = list(tenant.get("skills") or [])
    if op == "CREATE" and skill_name not in t_skills:
        t_skills.append(skill_name)
    elif op == "DELETE":
        t_skills = [s for s in t_skills if s != skill_name]

    sb.table("tenants").update({"skills": t_skills if t_skills else None}).eq("id", tenant_id).execute()

    try:
        if op == "CREATE":
            sb.table("agent_skills").upsert(
                {"user_id": agent_id, "tenant_id": tenant_id, "skill_name": skill_name},
                on_conflict="user_id,tenant_id,skill_name",
            ).execute()
        elif op == "DELETE":
            sb.table("agent_skills").delete().eq("user_id", agent_id).eq("tenant_id", tenant_id).eq("skill_name", skill_name).execute()
    except Exception:
        logger.warning("agent_skills 동기화 실패 (무시)", exc_info=True)


# ---------------------------------------------------------------------------
# 피드백 PR 기록
# ---------------------------------------------------------------------------

def record_feedback_pr(
    *,
    tenant_id: str,
    skill_name: str,
    branch_name: str,
    base_branch: str,
    title: str,
    description: str,
    requester_id: str | None = None,
    requester_name: str | None = None,
    git_pr_number: int | None = None,
    git_pr_url: str = "",
    git_repo_url: str = "",
) -> dict[str, Any] | None:
    """resource_pull_requests 테이블에 피드백 기반 PR 레코드를 삽입한다."""
    import uuid

    if not requester_id:
        requester_id = str(uuid.UUID(int=0))

    try:
        sb = get_supabase()
        row = {
            "tenant_id": tenant_id,
            "resource_type": "skill",
            "resource_id": skill_name,
            "branch_name": branch_name,
            "base_branch": base_branch,
            "title": title,
            "description": description,
            "status": "OPEN",
            "requester_id": requester_id,
            "requester_name": requester_name,
            "git_pr_number": git_pr_number,
            "git_pr_url": git_pr_url,
            "git_repo_url": git_repo_url,
        }
        resp = sb.table("resource_pull_requests").insert(row).execute()
        data = getattr(resp, "data", None)
        return data[0] if data else None
    except Exception:
        logger.warning(
            "피드백 PR 기록 실패: skill=%s branch=%s",
            skill_name, branch_name,
            exc_info=True,
        )
        return None


