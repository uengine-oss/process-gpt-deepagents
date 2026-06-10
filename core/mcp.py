from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


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


async def _load_mcp_tools(server_names: list[str], tenant_mcp: dict) -> list:
    """지정된 MCP 서버들에서 LangChain BaseTool 목록을 로드한다."""
    mcp_servers = tenant_mcp.get("mcpServers", {})
    connections = {}
    for name in server_names:
        if name in mcp_servers:
            connections[name] = _mcp_connection_config(mcp_servers[name])
        else:
            logger.warning("MCP 서버 설정 없음: %s (사용 가능: %s)", name, list(mcp_servers))

    if not connections:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient
    client = MultiServerMCPClient(connections)
    try:
        tools = await client.get_tools()
        logger.info("MCP tools 로드 완료: %s → %d개", list(connections), len(tools))
        return tools
    except Exception as e:
        logger.error("MCP tools 로드 실패 (%s): %s", list(connections), e)
        return []


def ensure_work_assistant_mcp(tenant_mcp: dict, tenant_id: str = "") -> dict:
    """SUPABASE_* 환경 변수가 있으면 work-assistant MCP를 mcpServers에 주입한다.

    이미 work-assistant가 있거나 SUPABASE_URL/KEY가 없으면 원본을 그대로 반환한다.
    """
    if not isinstance(tenant_mcp, dict):
        tenant_mcp = {}
    mcp_servers = dict(tenant_mcp.get("mcpServers") or {})
    if "work-assistant" in mcp_servers:
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

    mcp_servers["work-assistant"] = {
        "command": "uvx",
        "args": ["process-gpt-mcp"],
        "env": wa_env,
    }
    logger.info("work-assistant MCP 주입 | tenant_id=%s", tenant_id)
    return {**tenant_mcp, "mcpServers": mcp_servers}


async def load_chat_root_tools(tenant_mcp: dict) -> list:
    """채팅 모드 루트 오케스트레이터용 work-assistant 도구를 로드한다."""
    if "work-assistant" not in (tenant_mcp.get("mcpServers") or {}):
        return []
    try:
        tools = await _load_mcp_tools(["work-assistant"], tenant_mcp)
        logger.info("work-assistant 루트 도구: %d개", len(tools))
        return tools
    except Exception:
        logger.warning("work-assistant 루트 도구 로드 실패", exc_info=True)
        return []
