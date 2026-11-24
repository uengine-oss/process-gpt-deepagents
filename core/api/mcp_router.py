"""공통 MCP 서버 이름 — GET /mcp/common.

운영자가 COMMON_MCP_SERVERS(_FILE) 로 정한, 모든 테넌트의 에이전트에 붙는 MCP 서버 가운데 켜진 것의
이름만 돌려준다. Process GPT Lite 서버가 자기 /api/config 에 "자기 도구가 에이전트에 연결돼 있는지"를
알리려고 부른다. 주소·헤더·환경 변수는 내보내지 않는다 — 그래서 /health 처럼 인증 없이 열어 둔다
(클러스터 안에서만 닿는 경로다).
"""
from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.llm.mcp import enabled_common_mcp_server_names


async def get_common_mcp_servers(_request: Request) -> JSONResponse:
    # 파일 설정이면 stat·읽기가 끼므로 이벤트 루프 밖에서 부른다.
    names = await asyncio.to_thread(enabled_common_mcp_server_names)
    return JSONResponse({"servers": names})


routes = [Route("/mcp/common", get_common_mcp_servers, methods=["GET"])]
