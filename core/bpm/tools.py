"""core/bpm/tools.py — BPM DB 조회 도구 (로컬 전용).

MCP 서버의 단순 DB 조회 도구들을 딥 에이전트 로컬 tool로 이식한 모듈.
SERVICE_ROLE_KEY 로 RLS 를 우회하므로, 반드시 tenant_id 필터를 적용한다.
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Optional

from langchain_core.tools import tool

from core.storage.db import get_supabase

logger = logging.getLogger(__name__)

_current_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bpm_tenant_id", default=""
)


def set_tenant_id(tenant_id: str) -> None:
    _current_tenant_id.set(tenant_id)


def _require_tenant_id() -> str:
    tid = _current_tenant_id.get()
    if not tid:
        raise RuntimeError("tenant_id가 설정되지 않았습니다.")
    return tid


def _json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _err(msg: str) -> str:
    return _json({"error": msg})


# ---------------------------------------------------------------------------
# 1. get_current_user
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_current_user(email: str) -> str:
    """사용자 이메일로 식별 정보(user_uid, username, role 등)를 조회한다.

    execute_process 전에 user_uid 확보가 필요할 때 사용한다.

    Args:
        email: 조회할 사용자 이메일 주소.
    """
    tenant_id = _require_tenant_id()
    try:
        resp = (
            get_supabase()
            .table("users")
            .select("id,email,username,role,profile,is_admin,is_agent")
            .eq("tenant_id", tenant_id)
            .eq("email", email)
            .execute()
        )
        data = resp.data
        if data:
            u = data[0]
            return _json({
                "user_uid": u.get("id"),
                "user_email": u.get("email"),
                "username": u.get("username"),
                "role": u.get("role", "user"),
                "profile": u.get("profile"),
                "is_admin": u.get("is_admin", False),
                "is_agent": u.get("is_agent", False),
            })
        return _err(f"이메일 '{email}'에 해당하는 사용자를 찾을 수 없습니다.")
    except Exception as e:
        logger.error("[get_current_user] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 2. get_process_list
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_process_list() -> str:
    """현재 테넌트의 프로세스 정의 목록(id, name)을 조회한다.

    사용자가 기존 프로세스에 대해 질문하거나 실행 요청했을 때 대상 프로세스 식별용으로 사용한다.
    """
    tenant_id = _require_tenant_id()
    try:
        resp = (
            get_supabase()
            .table("proc_def")
            .select("id,name")
            .eq("tenant_id", tenant_id)
            .execute()
        )
        return _json(resp.data)
    except Exception as e:
        logger.error("[get_process_list] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 3. get_process_detail
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_process_detail(process_id: str) -> str:
    """특정 프로세스 정의(definition)를 조회한다.

    실행 전 activity_id/form_key 추출과 역할 담당자 검증에 사용한다.

    Args:
        process_id: 프로세스 정의 ID.
    """
    tenant_id = _require_tenant_id()
    try:
        resp = (
            get_supabase()
            .table("proc_def")
            .select("id,name,definition")
            .eq("tenant_id", tenant_id)
            .eq("id", process_id)
            .execute()
        )
        data = resp.data
        if data:
            return _json(data[0])
        return _err("프로세스를 찾을 수 없습니다.")
    except Exception as e:
        logger.error("[get_process_detail] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 4. get_form_fields
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_form_fields(form_key: str) -> str:
    """form_key에 해당하는 폼 정의(fields_json 포함)를 조회한다.

    execute_process 전에 필수 입력 항목과 타입을 확인할 때 사용한다.

    Args:
        form_key: 폼 정의 ID.
    """
    tenant_id = _require_tenant_id()
    try:
        resp = (
            get_supabase()
            .table("form_def")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("id", form_key)
            .execute()
        )
        data = resp.data
        if data:
            return _json(data[0])
        return _err("폼을 찾을 수 없습니다.")
    except Exception as e:
        logger.error("[get_form_fields] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 5. get_instance_list
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_instance_list(user_uid: str, process_id: Optional[str] = None) -> str:
    """사용자 참여 프로세스 인스턴스 목록을 조회한다.

    process_id를 주면 특정 프로세스로 필터링하며,
    반환된 proc_inst_id는 get_todolist 입력으로 사용한다.

    Args:
        user_uid: 사용자 UID (get_current_user로 조회).
        process_id: 특정 프로세스로 필터링 (선택).
    """
    tenant_id = _require_tenant_id()
    try:
        q = (
            get_supabase()
            .table("bpm_proc_inst")
            .select("*")
            .eq("tenant_id", tenant_id)
            .contains("participants", [user_uid])
        )
        if process_id:
            q = q.eq("proc_def_id", process_id)
        resp = q.execute()
        return _json(resp.data)
    except Exception as e:
        logger.error("[get_instance_list] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 6. get_todolist
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_todolist(instance_ids: list[str]) -> str:
    """instance_ids의 활동 목록과 각 활동 output(실행 결과)을 조회한다.

    실행 결과/진행 상태 확인의 최종 단계에서 사용한다.

    Args:
        instance_ids: 프로세스 인스턴스 ID 목록.
    """
    tenant_id = _require_tenant_id()
    if not instance_ids:
        return _err("instance_ids가 비어있습니다.")
    try:
        resp = (
            get_supabase()
            .table("todolist")
            .select("*")
            .eq("tenant_id", tenant_id)
            .in_("proc_inst_id", instance_ids)
            .order("start_date")
            .execute()
        )
        result: dict = {}
        for todo in resp.data:
            def_id = todo.get("proc_def_id", "unknown")
            inst_id = todo.get("proc_inst_id", "unknown")
            if def_id not in result:
                result[def_id] = {"processDefinitionId": def_id, "instances": {}}
            if inst_id not in result[def_id]["instances"]:
                result[def_id]["instances"][inst_id] = {"instanceId": inst_id, "activities": []}
            result[def_id]["instances"][inst_id]["activities"].append({
                "activityId": todo.get("activity_id"),
                "activityName": todo.get("activity_name"),
                "status": todo.get("status"),
                "startDate": todo.get("start_date"),
                "endDate": todo.get("end_date"),
                "userId": todo.get("user_id"),
                "output": todo.get("output"),
            })
        return _json(result)
    except Exception as e:
        logger.error("[get_todolist] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 7. get_organization
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
def get_organization() -> str:
    """테넌트 조직도(부서/팀/구성원 구조)를 조회한다.

    조직/팀 구성 질문에 대해 단독으로 호출하면 된다.
    """
    tenant_id = _require_tenant_id()
    try:
        resp = (
            get_supabase()
            .table("configuration")
            .select("value")
            .eq("tenant_id", tenant_id)
            .eq("key", "organization")
            .execute()
        )
        data = resp.data
        if data:
            value = data[0].get("value", {})
            chart = value.get("chart", value) if isinstance(value, dict) else value
            return _json(chart)
        return _err("조직도 정보를 찾을 수 없습니다.")
    except Exception as e:
        logger.error("[get_organization] %s", e)
        return _err(str(e))


# ---------------------------------------------------------------------------
# 도구 목록 export
# ---------------------------------------------------------------------------
def get_bpm_tools() -> list:
    return [
        get_current_user,
        get_process_list,
        get_process_detail,
        get_form_fields,
        get_instance_list,
        get_todolist,
        get_organization,
    ]
