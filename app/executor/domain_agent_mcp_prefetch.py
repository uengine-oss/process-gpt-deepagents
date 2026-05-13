"""Merge domain-agent ``users.tools`` MCP server keys into executor tool prefetch."""

from __future__ import annotations

from typing import Any

# Keep in sync with ``app.agent.subagents`` (avoid importing subagents: pulls DB stack).
_VIRTUAL_TOOL_NAMES = frozenset({"computer-use"})


def _parse_tools_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _norm_mcp_server_lookup_key(s: str) -> str:
    return str(s or "").strip().lower().replace("_", "-")


def resolve_tenant_mcp_config_key_for_declared_server(
    *,
    mcp_servers: dict[str, Any],
    declared: str,
) -> str | None:
    """Map an agent-declared MCP token to a key present in tenant ``mcpServers`` (alias-safe).

    Mirrors :func:`app.agent.subagents.build_subagent_configs` matching where
    ``tool._mcp_server`` may be a longer tenant-scoped key that still ends with
    the short name stored on ``users.tools``.
    """
    decl_n = _norm_mcp_server_lookup_key(declared)
    if not decl_n:
        return None
    best_exact: str | None = None
    suffix_hits: list[str] = []
    for raw_key in mcp_servers.keys():
        rk = str(raw_key).strip()
        if not rk:
            continue
        kn = _norm_mcp_server_lookup_key(rk)
        if kn == decl_n:
            best_exact = rk
            break
        if kn.endswith(decl_n):
            suffix_hits.append(rk)
    if best_exact is not None:
        return best_exact
    if len(suffix_hits) == 1:
        return suffix_hits[0]
    if len(suffix_hits) > 1:
        return min(suffix_hits, key=len)
    return None


def extend_mcp_request_with_domain_agent_declared_servers(
    mcp_request: list[str],
    *,
    mcp_servers: Any,
    domain_agent_rows: Any,
) -> None:
    """Append MCP load keys required by ``domain_agent_rows[*].tools`` before the first tool load.

    Subagents only receive tools that already exist in ``all_tools`` from
    ``TaggingSafeToolLoader.create_tools_from_names``; omitting a server here
    makes configured tenant MCP tools silently missing for that run.
    """
    servers = mcp_servers if isinstance(mcp_servers, dict) else {}
    rows = domain_agent_rows if isinstance(domain_agent_rows, list) else []
    for ag in rows:
        if not isinstance(ag, dict):
            continue
        if str(ag.get("agent_type") or "").strip().lower() == "a2a":
            # a2a: ``users.tools`` lists tool names, not MCP server keys.
            continue
        for tok in _parse_tools_csv(ag.get("tools")):
            t = str(tok or "").strip()
            if not t or t in _VIRTUAL_TOOL_NAMES:
                continue
            resolved = resolve_tenant_mcp_config_key_for_declared_server(mcp_servers=servers, declared=t)
            load_key = resolved if resolved is not None else t
            if load_key not in mcp_request:
                mcp_request.append(load_key)
