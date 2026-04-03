"""SafeToolLoader subclass that tags MCP tools with ``_mcp_server`` for subagent routing."""

from __future__ import annotations

import logging
from typing import List

from processgpt_agent_utils.tools.safe_tool_loader import SafeToolLoader
from processgpt_agent_utils.utils.context_manager import proc_inst_id_var

from .configurable_memento_tool import ConfigurableMementoTool

logger = logging.getLogger(__name__)


class TaggingSafeToolLoader(SafeToolLoader):
    """Attach ``_mcp_server`` so build_subagent_configs can attach baseline claude-skills tools."""

    def _load_memento(self) -> List:
        logger.debug("🔒 MementoTool(Configurable) 로드 시작 | tenant_id=%s", self.tenant_id)
        try:
            if not self.tenant_id:
                logger.info("⏭️ MementoTool 로드 생략: tenant_id 없음")
                return []
            tool = ConfigurableMementoTool(
                tenant_id=self.tenant_id,
                proc_inst_id=proc_inst_id_var.get(),
            )
            logger.info(
                "✅ MementoTool 로드 완료 | tenant_id=%s proc_inst_id=%s",
                self.tenant_id,
                proc_inst_id_var.get(),
            )
            return [tool]
        except Exception as e:
            logger.error(
                "❌ MementoTool 로드 실패 | tenant_id=%s proc_inst_id=%s err=%s",
                self.tenant_id,
                proc_inst_id_var.get(),
                str(e),
                exc_info=True,
            )
            raise

    def _load_mcp_tool(self, tool_name: str):  # type: ignore[no-untyped-def]
        loaded = super()._load_mcp_tool(tool_name)
        for t in loaded or []:
            try:
                setattr(t, "_mcp_server", tool_name)
            except Exception:
                pass
        return loaded
