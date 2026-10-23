from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from ..observability import MCPContext, get_context, observe_tools

logger = logging.getLogger(__name__)

MCP_HUB_URL = os.getenv("MCP_HUB_URL", "http://mcp-hub:8500").rstrip("/")
MCP_HUB_ENABLED = os.getenv("MCP_HUB_ENABLED", "true").lower() != "false"

# MCP 로드 후 임시로 제거할 도구 이름들.
# (bpmn-process-generation 스킬 흐름과 충돌하므로 비활성화)
_DISABLED_MCP_TOOLS: set[str] = {
    # 지금 연결하려는 bpmn-process-generation 스킬로 대체되므로 비활성화.
    "create_consulting_process_workitem",
    "create_pdf2bpmn_workitem",
}


def is_mcp_server_enabled(server_config: Any) -> bool:
    """설정 화면의 켜기/끄기. 값이 없으면 켜진 것으로 본다(화면과 같은 기본값)."""
    return isinstance(server_config, dict) and server_config.get("enabled", True) is not False


async def hub_allowed_servers(tenant_id: str, agent_id: str) -> dict | None:
    """mcp-hub 에 이 에이전트가 쓸 수 있는 서버·툴을 물어본다.

    허브는 enabled · status · 접근권한 · 툴 화이트/블랙리스트를 모두 적용한
    결과만 돌려준다. 허브에 닿지 못하면 None 을 돌려주고, 호출자는 기존
    tenants.mcp 경로로 폴백한다 — 관측·거버넌스 계층이 죽었다고 업무가
    멈추면 안 된다.

    Returns:
        {서버이름: {"tools": [허용된 툴 이름] | None}} 또는 None
    """
    if not (MCP_HUB_ENABLED and tenant_id and agent_id):
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{MCP_HUB_URL}/agents/{agent_id}/tools",
                headers={"X-Tenant-Id": tenant_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("mcp-hub 조회 실패(%s) — tenants.mcp 로 폴백", exc)
        return None

    return {
        s["server_name"]: {"tools": s.get("allowed_tools")}
        for s in data.get("servers", [])
    }


_USER_PLACEHOLDERS = ("{{user_jwt}}", "{{user_email}}")


def resolve_user_placeholders(tenant_mcp: dict, user_jwt: str | None, user_email: str | None) -> dict:
    """mcpServers 의 headers·env 에 있는 {{user_jwt}}·{{user_email}} 을 요청 사용자 값으로 바꾼다.

    사용자별 자격증명을 쓰는 MCP 서버(예: 사용자 OAuth 토큰을 쥔 게이트웨이)를 테넌트 설정 하나로
    등록할 수 있게 한다. 값이 없어 치환하지 못한 서버는 잘못된 인증으로 호출하지 않도록 뺀다.
    """
    servers = (tenant_mcp or {}).get("mcpServers")
    if not isinstance(servers, dict):
        return tenant_mcp
    values = {"{{user_jwt}}": user_jwt or "", "{{user_email}}": user_email or ""}

    def sub(v):
        if not isinstance(v, str):
            return v
        for k, val in values.items():
            v = v.replace(k, val)
        return v

    out = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            out[name] = cfg
            continue
        blob = json.dumps({k: cfg.get(k) for k in ("headers", "env")}, ensure_ascii=False)
        used = [k for k in _USER_PLACEHOLDERS if k in blob]
        if not used:
            out[name] = cfg
            continue
        if any(not values[k] for k in used):
            logger.info("[MCP] skip %s: needs %s but the request has none", name, used)
            continue
        cfg = dict(cfg)
        for key in ("headers", "env"):
            if isinstance(cfg.get(key), dict):
                cfg[key] = {k: sub(v) for k, v in cfg[key].items()}
        out[name] = cfg
    return {**tenant_mcp, "mcpServers": out}


def _mcp_connection_config(server_config: dict) -> dict:
    """tenant_mcp mcpServers 항목을 langchain-mcp-adapters Connection 형식으로 변환."""
    if "command" in server_config:
        conn: dict = {
            "transport": "stdio",
            "command": server_config["command"],
            "args": list(server_config.get("args") or []),
        }
        if server_config.get("env"):
            conn["env"] = dict(server_config["env"])
        return conn

    transport_map = {
        "http": "streamable_http",
        "streamable_http": "streamable_http",
        "sse": "sse",
        "stdio": "stdio",
        "websocket": "websocket",
    }
    conn = {
        "url": server_config.get("url", ""),
        "transport": transport_map.get(server_config.get("type", "http"), "streamable_http"),
    }
    if server_config.get("headers"):
        conn["headers"] = server_config["headers"]
    return conn


async def _load_mcp_tools_grouped(
    server_names: list[str],
    tenant_mcp: dict,
    tool_filters: dict[str, list[str]] | None = None,
    errors: dict[str, str] | None = None,
    allowed: dict[str, dict] | None = None,
) -> dict[str, list]:
    """지정된 MCP 서버들에서 LangChain BaseTool 목록을 서버별로 로드한다.

    반환값은 실제로 그 도구를 제공한 서버 이름 -> BaseTool 목록. 여러 서버를 한 번에
    로드해도 각 도구가 어느 서버에서 왔는지 유실되지 않도록, 서버별로 개별 조회한다
    (chat_room context 의 connectors 를 정확히 귀속시키기 위해 필요).

    tool_filters 가 주어지면 서버별 허용 도구 이름 목록으로 추가 필터링한다
    (해당 서버 키가 없거나 빈 리스트면 그 서버는 전체 허용).

    errors 를 넘기면 설정이 없거나 불러오기에 실패한 서버의 이유를 서버 이름으로 담는다.

    설정 화면에서 끈 서버(`enabled: false`)는 불러오지 않는다 — 채팅 루트·업무 지정·서브에이전트·
    스킬 도구 카탈로그가 모두 여기를 거친다. 끈 것은 실패가 아니므로 errors 에 담지 않는다.
    """
    mcp_servers = tenant_mcp.get("mcpServers", {})
    connections = {}
    for name in server_names:
        if name in mcp_servers and not is_mcp_server_enabled(mcp_servers[name]):
            logger.info("MCP 서버 꺼짐, 건너뜀: %s", name)
        elif name in mcp_servers and allowed is not None and name not in allowed:
            logger.info("MCP 서버 권한 없음으로 제외: %s", name)
        elif name in mcp_servers:
            connections[name] = _mcp_connection_config(mcp_servers[name])
        else:
            logger.warning("MCP 서버 설정 없음: %s (사용 가능: %s)", name, list(mcp_servers))
            if errors is not None:
                errors[name] = "MCP 서버 설정 없음"

    if not connections:
        return {}

    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient(connections)

    async def _load_one(name: str) -> tuple[str, list]:
        try:
            tools = await client.get_tools(server_name=name)
            if _DISABLED_MCP_TOOLS:
                before = len(tools)
                tools = [t for t in tools if getattr(t, "name", "") not in _DISABLED_MCP_TOOLS]
                removed = before - len(tools)
                if removed:
                    logger.info("MCP 도구 임시 제거: %d개 %s (server=%s)", removed, sorted(_DISABLED_MCP_TOOLS), name)
            allowed = (tool_filters or {}).get(name)
            if allowed:
                before = len(tools)
                tools = [t for t in tools if getattr(t, "name", "") in allowed]
                if len(tools) != before:
                    logger.info("MCP 도구 허용목록 적용: %s → %d개 (server=%s)", allowed, len(tools), name)
            # 허브가 툴 단위 화이트/블랙리스트를 준 경우 여기서 적용한다.
            if allowed is not None:
                permitted = (allowed.get(name) or {}).get("tools")
                if permitted is not None:
                    allow_set = set(permitted)
                    dropped = [t.name for t in tools if t.name not in allow_set]
                    if dropped:
                        logger.info("MCP 툴 권한으로 제외 (%s): %s", name, dropped)
                    tools = [t for t in tools if t.name in allow_set]

            # 서버 귀속이 보존되는 이 지점에서 계측을 감싼다.
            tools = observe_tools(tools, name, get_context())
            return name, tools
        except Exception as e:
            logger.error("MCP tools 로드 실패 (%s): %s", name, e)
            if errors is not None:
                errors[name] = f"{type(e).__name__}: {e}"[:500]
            return name, []

    results = await asyncio.gather(*(_load_one(name) for name in connections))
    grouped = {name: tools for name, tools in results if tools}
    logger.info(
        "MCP tools 로드 완료: %s → %d개", list(connections),
        sum(len(t) for t in grouped.values()),
    )
    return grouped


def ensure_process_gpt_mcp(tenant_mcp: dict, tenant_id: str = "") -> dict:
    """SUPABASE_* 환경 변수가 있으면 process-gpt-mcp MCP를 mcpServers에 주입한다.

    이미 process-gpt-mcp가 있거나 SUPABASE_URL/KEY가 없으면 원본을 그대로 반환한다.
    """
    if not isinstance(tenant_mcp, dict):
        tenant_mcp = {}
    mcp_servers = dict(tenant_mcp.get("mcpServers") or {})
    if "process-gpt-mcp" in mcp_servers:
        return tenant_mcp

    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_anon = (
        os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SERVICE_ROLE_KEY")
        or ""
    ).strip()
    if not (sb_url and sb_anon):
        return tenant_mcp

    wa_env: dict[str, str] = {"SUPABASE_URL": sb_url, "SUPABASE_ANON_KEY": sb_anon}
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    if jwt_secret:
        wa_env["SUPABASE_JWT_SECRET"] = jwt_secret

    mcp_servers["process-gpt-mcp"] = {
        "command": "uvx",
        "args": ["process-gpt-mcp"],
        "env": wa_env,
    }
    logger.info("process-gpt-mcp MCP 주입 | tenant_id=%s", tenant_id)
    return {**tenant_mcp, "mcpServers": mcp_servers}


_LOCAL_BPM_TOOLS = frozenset({
    "get_current_user",
    "get_process_list",
    "get_process_detail",
    "get_form_fields",
    "get_instance_list",
    "get_todolist",
    "get_organization",
})


async def load_chat_root_tools(
    tenant_mcp: dict, tenant_id: str = "", agent_id: str = "", errors: dict[str, str] | None = None
) -> tuple[list, dict[str, str]]:
    """채팅 모드 루트 오케스트레이터용 MCP 도구를 로드한다.

    tenants.mcp.mcpServers 에 등록된 모든 서버의 도구를 로드하며,
    로컬 BPM 도구와 중복되는 항목은 제외한다.

    Returns:
        (tools, tool_servers) — tool_servers 는 도구 이름 -> 실제로 그 도구를
        제공한 MCP 서버 이름 매핑이다(여러 서버가 등록돼 있어도 도구별로
        정확한 서버를 chat_room context 의 connectors 에 귀속시키기 위함).
    """
    server_names = list((tenant_mcp.get("mcpServers") or {}).keys())
    if not server_names:
        return [], {}
    try:
        allowed = await hub_allowed_servers(tenant_id, agent_id)
        grouped = await _load_mcp_tools_grouped(server_names, tenant_mcp, errors=errors, allowed=allowed)
        tools: list = []
        tool_servers: dict[str, str] = {}
        before = 0
        for server, srv_tools in grouped.items():
            before += len(srv_tools)
            for t in srv_tools:
                if t.name in _LOCAL_BPM_TOOLS:
                    continue
                tools.append(t)
                tool_servers[t.name] = server
        if before != len(tools):
            logger.info(
                "MCP 도구 중복 제거: %d개 → %d개 (로컬 BPM 도구 %d개 제외)",
                before, len(tools), before - len(tools),
            )
        logger.info("채팅 루트 MCP 도구: %d개 (%s)", len(tools), server_names)
        return tools, tool_servers
    except Exception:
        logger.warning("채팅 루트 MCP 도구 로드 실패", exc_info=True)
        return [], {}


def fetch_tenant_mcp(tenant_id: str) -> dict:
    """테넌트의 MCP 설정(`tenants.mcp`)에 process-gpt-mcp 기본 주입까지 더한 값을 돌려준다.

    채팅 실행(executor)과 스킬 도구 카탈로그가 같은 설정으로 도구를 불러오게 한곳에 둔다.
    조회에 실패하면 기본 주입만 한 빈 설정을 돌려준다.
    """
    tenant_mcp: dict = {}
    if tenant_id:
        try:
            from core.storage.db import get_supabase

            result = get_supabase().table("tenants").select("mcp").eq("id", tenant_id).single().execute()
            fetched = (result.data or {}).get("mcp")
            if isinstance(fetched, dict):
                tenant_mcp = fetched
        except Exception:
            logger.warning("tenant_mcp DB 조회 실패 | tenant_id=%s", tenant_id, exc_info=True)
    return ensure_process_gpt_mcp(tenant_mcp, tenant_id)
