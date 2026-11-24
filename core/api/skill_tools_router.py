"""스킬 도구 바인딩 화면용 API — 도구 카탈로그, 도구별 스킬 연결 현황, 스킬 전용 도구 지정.

모든 경로는 SDK 테넌트 가드로 인증된 사용자의 테넌트만 다룬다. 스킬 전용 지정은 그 테넌트의
관리자(`users.is_admin` 또는 관리자 역할)만 바꿀 수 있다 — 화면의 관리자 판정(localStorage)은
표시용일 뿐이다.
"""
from __future__ import annotations

import asyncio
import logging

from processgpt_agent_sdk import tenant_auth
from processgpt_agent_sdk.tenant_auth import request_tenant_id, request_user_id, tenant_guard
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.skills import tool_catalog

logger = logging.getLogger(__name__)


_ADMIN_ROLES = frozenset({"admin", "superadmin", "roleadmin"})


def _is_admin_role(role) -> bool:
    """화면(`isAdminRole`)과 같은 규칙 — 대소문자·`_`·`-` 를 무시하고 admin 계열이면 관리자."""
    return isinstance(role, str) and role.strip().lower().replace("_", "").replace("-", "") in _ADMIN_ROLES


def _is_tenant_admin(user_id: str, tenant_id: str) -> bool:
    if not user_id:
        return False
    from core.storage.db import get_supabase

    try:
        rows = (
            get_supabase().table("users").select("is_admin,role")
            .eq("id", user_id).eq("tenant_id", tenant_id).limit(1).execute()
        ).data or []
    except Exception:
        logger.warning("[skill-tools] 관리자 확인 실패 | tenant=%s", tenant_id, exc_info=True)
        return False
    return bool(rows) and (bool(rows[0].get("is_admin")) or _is_admin_role(rows[0].get("role")))


async def _request_user(request: Request) -> dict:
    """MCP 설정의 {{user_jwt}}·{{user_email}} 에 넣을 요청 사용자 값 — 채팅 실행처럼 그 사용자로 서버에 붙는다.

    가드가 이미 토큰을 검증했다(다시 검증해도 SDK 캐시에 걸린다). 값을 못 얻으면 비워 두고, 그 값이
    필요한 서버는 카탈로그가 부르지 않고 뺀다.
    """
    auth = request.headers.get("authorization") or ""
    user_jwt = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    user_email = ""
    try:
        user_email = (await tenant_auth.resolve_identity(request)).email or ""
    except Exception:
        pass
    return {"user_jwt": user_jwt or None, "user_email": user_email or None}


@tenant_guard
async def get_catalog(request: Request) -> JSONResponse:
    return JSONResponse(await tool_catalog.build_catalog(request_tenant_id(request), **await _request_user(request)))


@tenant_guard
async def get_bindings(request: Request) -> JSONResponse:
    return JSONResponse(await tool_catalog.build_bindings(request_tenant_id(request), **await _request_user(request)))


@tenant_guard
async def get_skill_only(request: Request) -> JSONResponse:
    names = await asyncio.to_thread(tool_catalog.load_skill_only_tools, request_tenant_id(request), use_cache=False)
    return JSONResponse({"tools": sorted(names)})


@tenant_guard
async def put_skill_only(request: Request) -> JSONResponse:
    tenant_id = request_tenant_id(request)
    if not await asyncio.to_thread(_is_tenant_admin, request_user_id(request), tenant_id):
        return JSONResponse({"error": "테넌트 관리자만 스킬 전용 도구를 지정할 수 있습니다"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        saved = await asyncio.to_thread(tool_catalog.save_skill_only_tools, tenant_id, body.get("tools"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    logger.info("[skill-tools] 스킬 전용 도구 지정 | tenant=%s user=%s tools=%s", tenant_id, request_user_id(request), saved)
    return JSONResponse({"tools": saved})


@tenant_guard
async def patch_skill_only(request: Request) -> JSONResponse:
    """바꾼 도구만 `{"add": [...], "remove": [...]}` 로 받아 현재 지정에 적용한다."""
    tenant_id = request_tenant_id(request)
    if not await asyncio.to_thread(_is_tenant_admin, request_user_id(request), tenant_id):
        return JSONResponse({"error": "테넌트 관리자만 스킬 전용 도구를 지정할 수 있습니다"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        saved = await asyncio.to_thread(
            tool_catalog.patch_skill_only_tools, tenant_id, body.get("add"), body.get("remove")
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    logger.info(
        "[skill-tools] 스킬 전용 도구 변경 | tenant=%s user=%s add=%s remove=%s → %s",
        tenant_id, request_user_id(request), body.get("add"), body.get("remove"), saved,
    )
    return JSONResponse({"tools": saved})


routes = [
    Route("/skill-tools/catalog", get_catalog, methods=["GET"]),
    Route("/skill-tools/bindings", get_bindings, methods=["GET"]),
    Route("/skill-tools/skill-only", get_skill_only, methods=["GET"]),
    Route("/skill-tools/skill-only", put_skill_only, methods=["PUT"]),
    Route("/skill-tools/skill-only", patch_skill_only, methods=["PATCH"]),
]
