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


def _find_activity(definition: Any, activity_id: str) -> dict[str, Any] | None:
    """definition.activities[] 에서 activity_id 와 일치하는 항목을 반환한다."""
    aid = (activity_id or "").strip()
    if not aid or not isinstance(definition, dict):
        return None
    activities = definition.get("activities")
    if not isinstance(activities, list):
        return None

    for act in activities:
        if not isinstance(act, dict):
            continue
        for key in ("id", "activity_id", "activityId", "key"):
            if str(act.get(key) or "").strip() == aid:
                return act
    return None


def _extract_activity_tools_skills(
    definition: Any,
    *,
    activity_id: str,
) -> tuple[list[str], list[str]]:
    target = _find_activity(definition, activity_id)
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


# 액티비티에 배정된 에이전트가 2명 이상일 때, 그중 하나를 딥 에이전트의 "루트"로
# 승격할지 설계자가 고르는 값(프로세스 설계 화면의 '메인 에이전트').
#   - None  : 미설정(자동) — 배정 에이전트가 1명이면 승격하는 기존 휴리스틱을 그대로 둔다.
#   - GENERIC: 승격 금지 — 제네릭 딥 에이전트가 배정 에이전트 전원을 서브에이전트로 위임한다.
#   - 그 외  : 해당 에이전트 id 를 루트로 승격하고 나머지는 서브에이전트로 남긴다.
ROOT_AGENT_GENERIC = "__none__"


def _extract_activity_root_agent(definition: Any, *, activity_id: str) -> str | None:
    target = _find_activity(definition, activity_id)
    if target is None:
        return None
    for key in ("rootAgent", "root_agent", "rootAgentId", "root_agent_id"):
        if key not in target:
            continue
        value = target.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        return text
    return None


def load_activity_root_agent(
    *,
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
) -> str | None:
    """액티비티에 설정된 루트 에이전트 지정값을 반환한다. 미설정/조회 실패 시 None."""
    tid = (tenant_id or "").strip()
    pdid = (proc_def_id or "").strip()
    aid = (activity_id or "").strip()
    if not (tid and pdid and aid):
        return None
    try:
        definition = _get_proc_def_definition(tid, pdid)
    except Exception:
        logger.exception(
            "activity root agent lookup failed | tenant_id=%s proc_def_id=%s activity_id=%s",
            tid, pdid, aid,
        )
        return None
    return _extract_activity_root_agent(definition, activity_id=aid)


def resolve_root_agent(
    *,
    agents: list,
    agent_profile: dict | None,
    explicit_root_agent_id: str | None,
    activity_id: str = "",
) -> tuple[dict | None, list]:
    """딥 에이전트의 루트 프로필과 서브에이전트로 남을 에이전트 목록을 확정한다.

    반환: (agent_profile, agents)
      - agent_profile 이 None 이면 제네릭 딥 에이전트가 루트다.
      - agents 는 서브에이전트 후보 원본이며, 루트로 승격된 항목의 제외는
        호출부의 _exclude_root_profile() 이 담당한다.
    """
    agent_list = list(agents or [])
    pinned = (explicit_root_agent_id or "").strip() or None

    if pinned == ROOT_AGENT_GENERIC:
        # '기본 딥 에이전트' 명시 선택 — 배정 에이전트가 1명이어도 승격하지 않고
        # 제네릭 루트가 전원을 서브에이전트로 위임한다.
        logger.info(
            "루트 에이전트: 기본 딥 에이전트로 고정(승격 없음) | activity_id=%s agents=%d",
            activity_id, len(agent_list),
        )
        return (None, agent_list)

    if pinned:
        picked = next(
            (a for a in agent_list if isinstance(a, dict) and str(a.get("id") or "") == pinned),
            None,
        )
        if picked is not None:
            # 지정된 에이전트를 루트로 승격한다. 나머지 배정 에이전트는 그대로 남아
            # 서브에이전트로 위임된다.
            logger.info(
                "루트 에이전트 승격 | activity_id=%s root=%s subagents=%d",
                activity_id, pinned, len(agent_list) - 1,
            )
            return (picked, agent_list)
        if agent_profile and str(agent_profile.get("id") or "") == pinned:
            # 채팅 경로: 지정 대상이 이미 이번 턴의 agent_profile 이다.
            return (agent_profile, agent_list)
        # 배정에서 빠진 에이전트를 가리키는 낡은 설정 — 아래 자동 규칙으로 폴백한다.
        logger.warning(
            "지정된 루트 에이전트를 배정 목록에서 찾지 못해 자동 규칙으로 폴백 | activity_id=%s root=%s",
            activity_id, pinned,
        )

    # 자동(미설정) 규칙.
    # 방에 참여한 에이전트가 둘 이상이면(그룹 채팅) 특정 프로필 하나를 루트로 승격하지
    # 않는다 — 참여 에이전트 전원이 제네릭 딥 에이전트의 서브에이전트로 위임되는,
    # 여러 에이전트 협업 시나리오이기 때문이다.
    if agent_profile and len(agent_list) > 1:
        agent_profile = None

    # 채팅처럼 명시적인 agent_profile 신호가 없는 경우(주로 프로세스/워크리스트 모드):
    # row.user_id로 직접 배정된 에이전트가 정확히 1명이면 그 에이전트를 서브에이전트가
    # 아니라 루트로 승격한다. 2명 이상이면(레인 협업) 기존처럼 전원 서브에이전트로 위임한다.
    if not agent_profile and len(agent_list) == 1:
        return (agent_list[0], [])

    return (agent_profile, agent_list)


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
