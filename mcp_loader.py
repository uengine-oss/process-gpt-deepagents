"""Load MCP tools from default and optional tenant MCP servers.

Default MCP servers (claude-skills, computer-use) are always attempted.
Failures loading any server do not block subagent configuration;
tools from failed servers are simply omitted.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 기본 제공 MCP 서버 (Cursor mcp.json 기준). 반드시 제공되는 서버.
DEFAULT_MCP_SERVER_NAMES = ("claude-skills", "computer-use")

# 기본 URL (환경변수로 오버라이드 가능)
DEFAULT_MCP_SERVERS: dict[str, dict[str, Any]] = {
    "claude-skills": {
        "url": os.getenv("MCP_CLAUDE_SKILLS_URL", "http://localhost:8765/mcp"),
        "transport": "http",
    },
    "computer-use": {
        "url": os.getenv("MCP_COMPUTER_USE_URL", "http://127.0.0.1:8888/mcp"),
        "transport": "http",
    },
}


def _cursor_mcp_json_path() -> Path | None:
    """Return path to Cursor mcp.json if it exists."""
    base = os.getenv("CURSOR_MCP_CONFIG_DIR") or os.path.expanduser("~/.cursor")
    path = Path(base) / "mcp.json"
    return path if path.is_file() else None


def _parse_mcp_servers_from_json(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert mcp.json-style config to langchain-mcp-adapters connection format."""
    out: dict[str, dict[str, Any]] = {}
    raw = config.get("mcpServers") or config
    for name, raw_conn in (raw or {}).items():
        if not isinstance(raw_conn, dict):
            continue
        if "url" in raw_conn:
            out[name] = {
                "url": raw_conn["url"],
                "transport": raw_conn.get("transport", "http"),
            }
    return out


def get_default_mcp_connections() -> dict[str, dict[str, Any]]:
    """Return connection config for default MCP servers.

    Uses DEFAULT_MCP_SERVERS; if Cursor mcp.json exists, URLs for
    claude-skills and computer-use are overridden from it.
    """
    connections = dict(DEFAULT_MCP_SERVERS)
    path = _cursor_mcp_json_path()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            from_file = _parse_mcp_servers_from_json(data)
            for name in DEFAULT_MCP_SERVER_NAMES:
                if name in from_file:
                    connections[name] = from_file[name]
        except Exception as e:
            logger.warning("Could not read MCP config from %s: %s", path, e)
    return connections


async def load_default_mcp_tools(
    *,
    tenant_id: str | None = None,
    merge_tenant_mcp: bool = True,
) -> list[Any]:
    """Load tools from default MCP servers (and optionally tenant MCP).

    claude-skills and computer-use are always attempted. If merge_tenant_mcp
    is True and tenant_id is set, tenant MCP config from DB is merged in.
    Any server that fails to load is skipped; the function never raises.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning(
            "langchain-mcp-adapters not installed; MCP tools will not be loaded."
        )
        return []

    connections = get_default_mcp_connections()

    if merge_tenant_mcp and tenant_id:
        try:
            from database import fetch_tenant_mcp
            tenant_mcp = fetch_tenant_mcp(tenant_id)
            if tenant_mcp:
                from_file = _parse_mcp_servers_from_json(
                    tenant_mcp if isinstance(tenant_mcp, dict) else {}
                )
                for name, conn in from_file.items():
                    if name not in connections:
                        connections[name] = conn
        except Exception as e:
            logger.warning("Could not merge tenant MCP for tenant_id=%s: %s", tenant_id, e)

    all_tools: list[Any] = []
    for server_name, conn in list(connections.items()):
        try:
            client = MultiServerMCPClient({server_name: conn})
            tools = await client.get_tools(server_name=server_name)
            for t in tools:
                setattr(t, "_mcp_server", server_name)
            all_tools.extend(tools)
            logger.info(
                "MCP server '%s' loaded %d tool(s)",
                server_name,
                len(tools),
            )
        except Exception as e:
            logger.warning(
                "MCP server '%s' failed to load tools (subagent config continues): %s",
                server_name,
                e,
                exc_info=False,
            )
    return all_tools
