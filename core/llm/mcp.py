from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# MCP 로드 후 임시로 제거할 도구 이름들.
# (bpmn-process-generation 스킬 흐름과 충돌하므로 비활성화)
_DISABLED_MCP_TOOLS: set[str] = {
    # 지금 연결하려는 bpmn-process-generation 스킬로 대체되므로 비활성화.
    "create_consulting_process_workitem",
    "create_pdf2bpmn_workitem",
}


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
        # 로드 후 비활성 도구 임시 제거
        if _DISABLED_MCP_TOOLS:
            before = len(tools)
            tools = [t for t in tools if getattr(t, "name", "") not in _DISABLED_MCP_TOOLS]
            removed = before - len(tools)
            if removed:
                logger.info("MCP 도구 임시 제거: %d개 %s", removed, sorted(_DISABLED_MCP_TOOLS))
        logger.info("MCP tools 로드 완료: %s → %d개", list(connections), len(tools))
        return tools
    except Exception as e:
        logger.error("MCP tools 로드 실패 (%s): %s", list(connections), e)
        return []


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


async def load_chat_root_tools(tenant_mcp: dict) -> list:
    """채팅 모드 루트 오케스트레이터용 MCP 도구를 로드한다.

    tenants.mcp.mcpServers 에 등록된 모든 서버의 도구를 로드하며,
    로컬 BPM 도구와 중복되는 항목은 제외한다.
    """
    server_names = list((tenant_mcp.get("mcpServers") or {}).keys())
    if not server_names:
        return []
    try:
        tools = await _load_mcp_tools(server_names, tenant_mcp)
        before = len(tools)
        tools = [t for t in tools if t.name not in _LOCAL_BPM_TOOLS]
        if before != len(tools):
            logger.info(
                "MCP 도구 중복 제거: %d개 → %d개 (로컬 BPM 도구 %d개 제외)",
                before, len(tools), before - len(tools),
            )
        logger.info("채팅 루트 MCP 도구: %d개 (%s)", len(tools), server_names)
        return tools
    except Exception:
        logger.warning("채팅 루트 MCP 도구 로드 실패", exc_info=True)
        return []
