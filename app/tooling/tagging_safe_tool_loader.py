"""SafeToolLoader subclass that tags MCP tools with ``_mcp_server`` for subagent routing."""

from __future__ import annotations

import logging
import os
from typing import List

import requests
from processgpt_agent_utils.tools.knowledge_manager import MementoTool
from processgpt_agent_utils.tools.safe_tool_loader import SafeToolLoader
from processgpt_agent_utils.utils.context_manager import proc_inst_id_var

logger = logging.getLogger(__name__)


def _memento_retrieve_url() -> str | None:
    u = (os.environ.get("MEMENTO_RETRIEVE_URL") or "").strip().rstrip("/")
    return u or "http://memento-service:8005/retrieve"


class _EnvMementoTool(MementoTool):
    """MementoTool: GET URL from ``MEMENTO_RETRIEVE_URL`` with service default."""

    def _run(self, query: str) -> str:
        url = _memento_retrieve_url()
        if not url:
            return "사내문서 검색(Memento) URL이 비어 있습니다."
        logger.info("\n\n🔍 사내문서 검색 시작 | tenant_id=%s", self._tenant_id)

        try:
            logger.info(
                "🔍 사내문서 검색 시작 | tenant_id=%s, query=%s, url=%s",
                self._tenant_id,
                query,
                url,
            )
            resp = requests.get(
                url,
                params={
                    "query": query,
                    "tenant_id": self._tenant_id,
                    "proc_inst_id": self._proc_inst_id,
                },
                headers={"Accept": "application/json"},
                timeout=40,
            )
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw_text = resp.text or ""
            if not raw_text.strip():
                logger.info(
                    "📭 사내문서 검색 빈 응답 | tenant_id=%s query=%s status=%s",
                    self._tenant_id,
                    query,
                    resp.status_code,
                )
                return f"테넌트 '{self._tenant_id}'에서 '{query}' 검색 결과가 없습니다."

            try:
                data = resp.json()
            except Exception:
                logger.warning(
                    "❌ 사내문서 JSON 파싱 실패 | tenant_id=%s status=%s content_type=%s snippet=%s",
                    self._tenant_id,
                    resp.status_code,
                    content_type,
                    raw_text[:200],
                )
                return (
                    f"사내문서 검색 응답이 JSON이 아닙니다 "
                    f"(status={resp.status_code}, content_type='{content_type}')."
                )

            docs = data.get("response", [])
            logger.info("📄 사내문서 검색 결과: %d개", len(docs))
            if not docs:
                logger.info("📭 사내문서 검색 결과 없음 | tenant_id=%s query=%s", self._tenant_id, query)
                return f"테넌트 '{self._tenant_id}'에서 '{query}' 검색 결과가 없습니다."

            results = []
            for doc in docs:
                meta = doc.get("metadata", {}) or {}
                fname = meta.get("file_name", "unknown")
                idx = meta.get("chunk_index", "unknown")
                content = doc.get("page_content", "")
                results.append(f"📄 파일: {fname} (청크 #{idx})\n내용: {content}\n---")

            formatted_result = (
                f"테넌트 '{self._tenant_id}'에서 '{query}' 검색 결과:\n\n" + "\n".join(results)
            )
            logger.info("✅ 사내문서 검색 완료 | tenant_id=%s", self._tenant_id)
            return formatted_result

        except Exception as e:
            logger.error(
                "❌ 사내문서 검색 실패 | tenant_id=%s query=%s err=%s",
                self._tenant_id,
                query,
                str(e),
                exc_info=True,
            )
            raise


class TaggingSafeToolLoader(SafeToolLoader):
    """Attach ``_mcp_server`` so build_subagent_configs can attach baseline claude-skills tools.

    ``allowed_local_tools``: if given, only the named local tools are loaded;
    all others (_load_mem0, _load_dmn_rule, _load_human_asked, …) return [].
    Pass ``frozenset({"memento"})`` to restrict the deep-agent to memento only.
    """

    def __init__(self, *args, allowed_local_tools: frozenset[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._allowed_local_tools = allowed_local_tools  # None means "allow all"

    def _is_local_tool_allowed(self, name: str) -> bool:
        return self._allowed_local_tools is None or name in self._allowed_local_tools

    def _load_mem0(self) -> List:  # type: ignore[override]
        if not self._is_local_tool_allowed("mem0"):
            return []
        return super()._load_mem0()

    def _load_dmn_rule(self) -> List:  # type: ignore[override]
        if not self._is_local_tool_allowed("dmn_rule"):
            return []
        return super()._load_dmn_rule()

    def _load_human_asked(self) -> List:  # type: ignore[override]
        if not self._is_local_tool_allowed("human_asked"):
            return []
        return super()._load_human_asked()

    def _load_memento(self) -> List:
        logger.debug("🔒 MementoTool(환경 URL) 로드 시작 | tenant_id=%s", self.tenant_id)
        try:
            if not self.tenant_id:
                logger.info("⏭️ MementoTool 로드 생략: tenant_id 없음")
                return []
            tool = _EnvMementoTool(
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
