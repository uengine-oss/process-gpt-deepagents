"""SafeToolLoader subclass that tags MCP tools with ``_mcp_server`` for subagent routing."""

from __future__ import annotations

from processgpt_agent_utils.tools.safe_tool_loader import SafeToolLoader


class TaggingSafeToolLoader(SafeToolLoader):
    """Attach ``_mcp_server`` so build_subagent_configs can attach baseline claude-skills tools."""

    def _load_mcp_tool(self, tool_name: str):  # type: ignore[no-untyped-def]
        loaded = super()._load_mcp_tool(tool_name)
        for t in loaded or []:
            try:
                setattr(t, "_mcp_server", tool_name)
            except Exception:
                pass
        return loaded
