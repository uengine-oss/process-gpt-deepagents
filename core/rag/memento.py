"""Memento async HTTP 클라이언트.

GET /search       — 청크 RAG (strict top_k)
GET /catalog      — 문서 카탈로그
GET /document/grep
GET /document/page
GET /summarize
GET /documents/full-text
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx


logger = logging.getLogger(__name__)

_MEMENTO_BASE_URL = os.getenv("MEMENTO_BASE_URL", "http://127.0.0.1:8005")

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_MEMENTO_BASE_URL,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class Chunk:
    text: str
    file_name: str
    chunk_id: str = ""
    chunk_index: Optional[int] = None
    page_number: Optional[int] = None
    source: Optional[str] = None
    file_id: str = ""
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def cite(self) -> str:
        idx = f"#{self.chunk_index}" if self.chunk_index is not None else ""
        page = f" p{self.page_number}" if self.page_number is not None else ""
        name = self.file_name or self.source or "doc"
        return f"[{name}{idx}{page}]"


async def search(
    query: str,
    *,
    tenant_id: str,
    file_ids: Optional[List[str]] = None,
    top_k: int = 5,
    exclude_chunk_ids: Optional[List[str]] = None,
) -> List[Chunk]:
    if not tenant_id:
        logger.warning("memento.search: tenant_id 비어있음 → 빈 결과")
        return []

    params: list[tuple[str, str]] = [
        ("query", query or ""),
        ("tenant_id", tenant_id),
        ("top_k", str(int(top_k))),
    ]
    for fid in (file_ids or []):
        if fid:
            params.append(("file_ids", str(fid)))
    for cid in (exclude_chunk_ids or []):
        if cid:
            params.append(("exclude_chunk_ids", str(cid)))

    try:
        resp = await _get_client().get("/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("memento /search 실패: %s", exc)
        return []

    items = data.get("response", []) if isinstance(data, dict) else []
    out: List[Chunk] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        meta = it.get("metadata") if isinstance(it.get("metadata"), dict) else {}
        out.append(
            Chunk(
                text=str(it.get("page_content") or ""),
                file_name=str(meta.get("file_name") or meta.get("source") or ""),
                chunk_id=str(meta.get("chunk_id") or ""),
                chunk_index=meta.get("chunk_index"),
                page_number=meta.get("page_number"),
                source=meta.get("source"),
                file_id=str(meta.get("file_id") or ""),
                raw_metadata=meta,
            )
        )
    logger.info(
        "memento.search tenant=%s q=%r top_k=%d excluded=%d → %d chunks",
        tenant_id, (query or "")[:80], int(top_k), len(exclude_chunk_ids or []), len(out),
    )
    return out


@dataclass
class DocCard:
    file_name: str
    file_id: str = ""
    abstract: Optional[str] = None
    n_pages: Optional[int] = None
    folder_path: str = ""
    mime_type: Optional[str] = None
    index_status: Optional[str] = None
    source_type: Optional[str] = None
    doc_role: str = "content"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GrepHit:
    file_name: str
    page: int
    line: int
    snippet: str
    match: str = ""
    context: str = ""


@dataclass
class PageContent:
    page_number: int
    content: str


@dataclass
class SummaryBatch:
    batch_range: str
    page_start: int
    page_end: int
    page_count: int
    summary: str
    ok: bool = True
    error: str = ""


async def list_catalog(
    *,
    tenant_id: str,
    file_ids: Optional[List[str]] = None,
    file_names: Optional[List[str]] = None,
) -> List[DocCard]:
    if not tenant_id:
        return []

    params: list[tuple[str, str]] = [("tenant_id", tenant_id)]
    for fid in (file_ids or []):
        if fid:
            params.append(("file_ids", str(fid)))
    for name in (file_names or []):
        if name:
            params.append(("file_names", str(name)))

    try:
        resp = await _get_client().get("/catalog", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("memento /catalog 실패: %s", exc)
        return []

    items = data.get("response", []) if isinstance(data, dict) else []
    out: List[DocCard] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        card = it.get("doc_card") if isinstance(it.get("doc_card"), dict) else {}
        out.append(
            DocCard(
                file_name=str(it.get("file_name") or ""),
                file_id=str(it.get("file_id") or ""),
                abstract=card.get("abstract"),
                n_pages=card.get("n_pages"),
                folder_path=str(it.get("folder_path") or ""),
                mime_type=it.get("mime_type"),
                index_status=it.get("index_status"),
                source_type=it.get("source_type"),
                doc_role=str(it.get("doc_role") or "content"),
                raw=it,
            )
        )
    logger.info(
        "memento.list_catalog tenant=%s ids=%d → %d cards",
        tenant_id, len(file_ids or []), len(out),
    )
    return out


async def grep_document(
    *,
    tenant_id: str,
    file_name: str,
    pattern: str,
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 0,
    limit: int = 30,
) -> Dict[str, Any]:
    if not tenant_id or not file_name or not pattern:
        return {"hits": [], "total_matches": 0, "truncated": False, "error": "required params missing"}

    params: list[tuple[str, str]] = [
        ("tenant_id", tenant_id),
        ("file_name", file_name),
        # 지금의 memento 는 문서를 path(폴더 포함 상대경로, 없으면 파일 이름으로 다시 찾음)로 받는다.
        # file_name 만 보내면 422 — 옛 memento 는 path 를 무시하므로 둘 다 보낸다.
        ("path", file_name),
        ("pattern", pattern),
        ("regex", "true" if regex else "false"),
        ("case_sensitive", "true" if case_sensitive else "false"),
        ("context_lines", str(int(context_lines))),
        ("limit", str(int(limit))),
    ]

    try:
        resp = await _get_client().get("/document/grep", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("memento /document/grep 실패: %s", exc)
        return {"hits": [], "total_matches": 0, "truncated": False, "error": str(exc)}

    items = data.get("response", []) if isinstance(data, dict) else []
    hits: List[GrepHit] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        hits.append(
            GrepHit(
                file_name=str(it.get("file_name") or file_name),
                page=int(it.get("page") or 0),
                line=int(it.get("line") or 0),
                snippet=str(it.get("snippet") or ""),
                match=str(it.get("match") or ""),
                context=str(it.get("context") or ""),
            )
        )
    return {
        "hits": hits,
        "total_matches": int(data.get("total_matches") or 0) if isinstance(data, dict) else 0,
        "truncated": bool(data.get("truncated")) if isinstance(data, dict) else False,
        "error": data.get("error") if isinstance(data, dict) else None,
    }


async def read_pages(
    *,
    tenant_id: str,
    file_name: str,
    pages: str,
) -> Dict[str, Any]:
    if not tenant_id or not file_name or not pages:
        return {"file_name": file_name, "pages": [], "error": "required params missing"}

    params: list[tuple[str, str]] = [
        ("tenant_id", tenant_id),
        ("file_name", file_name),
        ("path", file_name),  # grep_document 와 같은 이유
        ("pages", pages),
    ]
    try:
        resp = await _get_client().get("/document/page", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("memento /document/page 실패: %s", exc)
        return {"file_name": file_name, "pages": [], "error": str(exc)}

    items = data.get("pages", []) if isinstance(data, dict) else []
    pages_out: List[PageContent] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pn = it.get("page_number")
        try:
            pn_int = int(pn) if pn is not None else 0
        except (TypeError, ValueError):
            pn_int = 0
        pages_out.append(PageContent(page_number=pn_int, content=str(it.get("content") or "")))

    return {
        "file_name": file_name,
        "pages": pages_out,
        "error": data.get("error") if isinstance(data, dict) else None,
    }


async def summarize_document(
    *,
    tenant_id: str,
    file_name: str,
    batch_size: int = 20,
    parallelism: int = 5,
) -> Dict[str, Any]:
    if not tenant_id or not file_name:
        return {
            "file_name": file_name, "n_pages": 0, "n_batches": 0,
            "batch_size": batch_size, "abstract": "", "batches": [],
            "errors": ["tenant_id, file_name required"],
        }

    params: list[tuple[str, str]] = [
        ("tenant_id", tenant_id),
        ("file_name", file_name),
        ("batch_size", str(int(batch_size))),
        ("parallelism", str(int(parallelism))),
    ]

    try:
        resp = await _get_client().get(
            "/summarize",
            params=params,
            timeout=httpx.Timeout(300.0, connect=5.0),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("memento /summarize 실패: %s", exc)
        return {
            "file_name": file_name, "n_pages": 0, "n_batches": 0,
            "batch_size": batch_size, "abstract": "", "batches": [],
            "errors": [str(exc)],
        }

    if not isinstance(data, dict):
        return {
            "file_name": file_name, "n_pages": 0, "n_batches": 0,
            "batch_size": batch_size, "abstract": "", "batches": [],
            "errors": ["invalid response shape"],
        }

    raw_batches = data.get("batches") or []
    batches: list[SummaryBatch] = []
    for it in raw_batches:
        if not isinstance(it, dict):
            continue
        batches.append(
            SummaryBatch(
                batch_range=str(it.get("batch_range") or ""),
                page_start=int(it.get("page_start") or 0),
                page_end=int(it.get("page_end") or 0),
                page_count=int(it.get("page_count") or 0),
                summary=str(it.get("summary") or ""),
                ok=bool(it.get("ok", True)),
                error=str(it.get("error") or ""),
            )
        )

    return {
        "file_name": str(data.get("file_name") or file_name),
        "n_pages": int(data.get("n_pages") or 0),
        "n_batches": int(data.get("n_batches") or 0),
        "batch_size": int(data.get("batch_size") or batch_size),
        "abstract": str(data.get("abstract") or ""),
        "batches": batches,
        "errors": list(data.get("errors") or []),
    }
