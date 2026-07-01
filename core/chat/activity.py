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


def build_fallback_subagent(
    *,
    tenant_id: str,
    activity_tools: list[str],
    activity_skills: list[str],
) -> dict[str, Any] | None:
    """Build a SubAgent-compatible dict from activity-declared tools/skills.

    The returned dict shape is understood by core.agents.subagents._build_subagents:
    - tools: CSV/list of MCP server names
    - skills: CSV of skill names
    """
    if not activity_tools and not activity_skills:
        return None

    name = "업무 실행자"
    sys_lines = [
        "=== 역할 ===",
        "- 당신은 프로세스 액티비티에 정의된 도구/스킬을 사용해 업무를 수행하는 실행 에이전트입니다.",
        "- 결과는 간결하고 검증 가능한 형태(파일/JSON/근거)로 반환하세요.",
    ]
    if activity_tools:
        sys_lines.append(f"- 액티비티 지정 도구: {', '.join(activity_tools)}")
    if activity_skills:
        sys_lines.append(f"- 액티비티 지정 스킬: {', '.join(activity_skills)}")
    sys_lines.extend([
        "",
        "=== 우선순위 ===",
        "- 1) task() 위임(description)의 지시/출력 형식을 최우선으로 따른다.",
        "- 2) 스킬은 추정하지 말고, 필요한 경우 도구로 문서를 조회/로드한 뒤 사용한다.",
        "- 3) 실행이 가능하면 실행(샌드박스)하고, 불가하면 근거와 다음 시도안을 보고한다.",
    ])

    return {
        "name": name,
        "username": name,
        "alias": name,
        "description": (
            f"프로세스 액티비티 정의 기반 폴백 실행자 (tenant={tenant_id or '(unknown)'})"
        ),
        "system_prompt": "\n".join(sys_lines),
        "tools": ",".join(activity_tools),
        "skills": ",".join(activity_skills),
        "agent_type": "activity_fallback",
    }


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
