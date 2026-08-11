from __future__ import annotations

import logging
from typing import Any

from ..storage.db import get_supabase

logger = logging.getLogger(__name__)


def _get_proc_def_definition(tenant_id: str, proc_def_id: str) -> dict[str, Any] | None:
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    if not (tid and pdid):
        return None
    sb = get_supabase()
    try:
        resp = (
            sb.table("proc_def")
            .select("definition")
            .eq("tenant_id", tid)
            .eq("id", pdid)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("proc_def lookup failed | tenant_id=%s proc_def_id=%s", tid, pdid)
        return None
    rows = getattr(resp, "data", None)
    if not isinstance(rows, list) or not rows:
        return None
    row0 = rows[0]
    return row0.get("definition") if isinstance(row0, dict) else None


def _extract_activity_tools_skills(
    definition: Any,
    *,
    activity_id: str,
) -> tuple[list[str], list[str]]:
    aid = (activity_id or "").strip()
    if not aid or not isinstance(definition, dict):
        return ([], [])
    activities = definition.get("activities")
    if not isinstance(activities, list):
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
            return [str(x).strip() for x in value if str(x).strip()]
        return []

    tools = list(dict.fromkeys(_as_str_list(target.get("tools"))))
    skills = list(dict.fromkeys(_as_str_list(target.get("skills"))))
    return (tools, skills)


def load_activity_capabilities(
    *,
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
) -> tuple[list[str], list[str]]:
    """Return (tools, skills) declared for the given activity. Returns ([], []) on any error."""
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    aid = (activity_id or "").strip()
    if not (tid and pdid and aid):
        return ([], [])
    try:
        definition = _get_proc_def_definition(tid, pdid)
    except Exception:
        logger.exception(
            "activity capabilities lookup failed | tenant_id=%s proc_def_id=%s activity_id=%s",
            tid, pdid, aid,
        )
        return ([], [])
    return _extract_activity_tools_skills(definition, activity_id=aid)


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
        logger.exception(
            "child activity todo 조회 실패: root_proc_inst_id=%s activity_id=%s",
            rpid, aid,
        )
        return None


def _find_subprocess(definition: Any, activity_id: str) -> dict[str, Any] | None:
    """definition.subProcesses[]에서 id가 activity_id와 일치하는 항목을 반환한다."""
    aid = (activity_id or "").strip()
    if not aid or not isinstance(definition, dict):
        return None
    for sp in (definition.get("subProcesses") or []):
        if not isinstance(sp, dict):
            continue
        if str(sp.get("id") or "").strip() == aid:
            return sp
    return None


def load_subprocess_subagents(
    *,
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
) -> list[dict[str, Any]]:
    """서브프로세스 워크아이템의 children.activities[]를 서브에이전트 스펙 목록으로 반환한다.

    activity_id가 definition.subProcesses[].id와 매칭되지 않으면 빈 리스트를 반환한다.
    반환된 딕셔너리 형태는 core.agents.subagents._build_subagents가 소비한다:
    - tools: MCP 서버명 CSV (children.activities[].tools)
    - skills: 스킬명 CSV (children.activities[].skills)
    """
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    aid = (activity_id or "").strip()
    if not (tid and pdid and aid):
        return []

    try:
        definition = _get_proc_def_definition(tid, pdid)
    except Exception:
        logger.exception(
            "subprocess subagents lookup failed | tenant_id=%s proc_def_id=%s activity_id=%s",
            tid, pdid, aid,
        )
        return []

    if not isinstance(definition, dict):
        return []

    sp = _find_subprocess(definition, aid)
    if sp is None:
        return []

    children = sp.get("children") or {}
    if not isinstance(children, dict):
        return []

    child_activities = children.get("activities") or []
    if not child_activities:
        return []

    def _as_csv(value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, list):
            return ",".join(str(v).strip() for v in value if str(v).strip())
        if isinstance(value, str):
            return value.strip()
        return ""

    result: list[dict[str, Any]] = []
    for act in child_activities:
        if not isinstance(act, dict):
            continue
        name = (act.get("name") or act.get("id") or "").strip()
        if not name:
            continue
        result.append({
            "name": name,
            "username": name,
            "alias": str(act.get("id") or name),
            "description": (act.get("instruction") or act.get("description") or "").strip(),
            "role": (act.get("role") or "").strip(),
            "tools": _as_csv(act.get("tools")),
            "skills": _as_csv(act.get("skills")),
            "tool": str(act.get("tool") or ""),
            "agent_type": "subprocess_activity",
        })
    return result
