"""Memento retrieve URL override for local/dev via MEMENTO_RETRIEVE_URL."""

from __future__ import annotations

import logging
import os

import requests

from processgpt_agent_utils.tools.knowledge_manager import MementoTool

logger = logging.getLogger(__name__)

DEFAULT_MEMENTO_RETRIEVE_URL = "https://memento.process-gpt.io/api/retrieve"


def memento_retrieve_url() -> str:
    return os.environ.get("MEMENTO_RETRIEVE_URL", DEFAULT_MEMENTO_RETRIEVE_URL).rstrip("/")


class ConfigurableMementoTool(MementoTool):
    """MementoTool with GET URL from MEMENTO_RETRIEVE_URL (default: production)."""

    def _run(self, query: str) -> str:
        url = memento_retrieve_url()
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
