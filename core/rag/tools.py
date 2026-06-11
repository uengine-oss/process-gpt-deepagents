"""Memento RAG 도구 — search_documents, retrieve_and_stage, list_documents, 등.

RunnableConfig.configurable 에서 tenant_id, knowledge_doc_ids 를 읽는다.
executor.py 에서 graph config 에 이 값을 주입해야 동작한다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from typing_extensions import Annotated

from core.rag import memento


logger = logging.getLogger(__name__)

_MAX_TOP_K = 10
_CURATED_FULL_THRESHOLD = 8
_SEEN_LIMIT = 25
_MAX_STAGING_CALLS = 3

_NAV_MAX_GREP_CALLS = 8
_NAV_MAX_READ_CALLS = 8
_NAV_MAX_PAGES_TOTAL = 30
_NAV_MAX_PAGES_PER_CALL = 10
_NAV_LIST_DOC_LIMIT = 1

_CURATED_PREFIX = "/curated/"
_SEEN_PREFIX = "/seen/"
_STAGING_PREFIX = "/staging/"
_CATALOG_PREFIX = "/catalog/"
_GREP_LOG_PREFIX = "/grep_log/"
_READ_LOG_PREFIX = "/read_log/"
_SUMMARY_PREFIX = "/summary/"

_CHUNK_TTL_SECONDS = 3600
_CHUNK_STORE: dict[tuple[str, str], dict[str, Any]] = {}


def _gc_chunk_store() -> None:
    cutoff = time.time() - _CHUNK_TTL_SECONDS
    stale = [k for k, v in _CHUNK_STORE.items() if v.get("_t", 0) < cutoff]
    for k in stale:
        _CHUNK_STORE.pop(k, None)


def _store_put(thread_id: str, chunk_id: str, body: str, meta: dict) -> None:
    _CHUNK_STORE[(thread_id, chunk_id)] = {"body": body, "meta": meta, "_t": time.time()}


def _store_get(thread_id: str, chunk_id: str) -> Optional[dict]:
    rec = _CHUNK_STORE.get((thread_id, chunk_id))
    if rec is None:
        return None
    rec["_t"] = time.time()
    return rec


def _ctx(config: RunnableConfig | None) -> dict:
    if not config:
        return {}
    cfg = config.get("configurable") or {}
    return cfg if isinstance(cfg, dict) else {}


def _ctx_known_file_ids(ctx: dict) -> set[str]:
    return {str(x) for x in (ctx.get("knowledge_doc_ids") or []) if x}


def _thread_id(ctx: dict) -> str:
    return str(ctx.get("thread_id") or ctx.get("conversation_id") or "default")


def _ids_from_state_files(state: dict, prefix: str) -> list[str]:
    files = (state or {}).get("files") or {}
    if not isinstance(files, dict):
        return []
    out: list[str] = []
    for path in files.keys():
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if rest.endswith(".md"):
            rest = rest[: -len(".md")]
        if rest:
            out.append(rest)
    return out


def _seen_chunk_ids(state: dict) -> list[str]:
    return _ids_from_state_files(state, _SEEN_PREFIX)


def _count_path_prefix(state: dict, prefix: str) -> int:
    files = (state or {}).get("files") or {}
    if not isinstance(files, dict):
        return 0
    return sum(1 for k in files.keys() if isinstance(k, str) and k.startswith(prefix))


def _status_line(seen_count: int, curated_count: int) -> str:
    return (
        f"[현재 진행 상황] seen {seen_count}/{_SEEN_LIMIT}개, "
        f"curated {curated_count}/{_CURATED_FULL_THRESHOLD}개. "
        f"→ seen 한도까지 {max(0, _SEEN_LIMIT - seen_count)}개 남음."
    )


def _err_msg(content: str, tool_call_id: str) -> Command:
    return Command(update={"messages": [ToolMessage(content=content, tool_call_id=tool_call_id)]})


def _ingest_and_format(chunks, thread_id: str) -> str:
    _gc_chunk_store()
    if not chunks:
        return ""
    out_blocks: list[str] = []
    for i, c in enumerate(chunks):
        cid = (getattr(c, "chunk_id", "") or "").strip()
        if not cid:
            fid_short = (getattr(c, "file_id", "") or "")[-12:]
            idx = getattr(c, "chunk_index", None)
            cid = f"{fid_short}-{idx if idx is not None else i}"
        meta = {
            "file_name": getattr(c, "file_name", "") or "",
            "file_id": getattr(c, "file_id", "") or "",
            "chunk_index": getattr(c, "chunk_index", None),
            "page_number": getattr(c, "page_number", None),
            "source": getattr(c, "source", None),
            "cite": c.cite() if hasattr(c, "cite") else "",
        }
        body = (getattr(c, "text", None) or "").strip()
        if not body:
            continue
        _store_put(thread_id, cid, body, meta)
        out_blocks.append(f"### chunk_id=`{cid}` {meta['cite']}\n\n{body}")
    if not out_blocks:
        return ""
    return (
        f"검색 결과 {len(out_blocks)}개 청크 (본문 포함). "
        f"답에 도움될 청크는 `keep_chunk(chunk_id=...)` 로 큐레이션.\n\n"
        + "\n\n".join(out_blocks)
    )


def _safe_path_segment(name: str, max_len: int = 60) -> str:
    import re as _re
    stem = (name or "").rsplit(".", 1)[0] or (name or "doc")
    safe = _re.sub(r"[^\w.\-가-힣]+", "_", stem)
    safe = safe.strip("._-") or "doc"
    return safe[:max_len]


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1 — search_documents
# ─────────────────────────────────────────────────────────────────────────────

@tool(parse_docstring=True)
async def search_documents(
    query: str,
    top_k: int = 5,
    *,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """사용자가 선택한 자료 *전체*에서 ``query`` 로 의미 검색한다.

    검색 대상 파일은 시스템이 자동 결정한다. 한 번 호출에 *총 top_k 개* 만 반환.
    받은 청크는 자동으로 "본 적 있음" 표시 — 다음 호출 시 같은 청크 재노출 없음.
    답에 도움될 청크는 ``keep_chunk(chunk_id)`` 호출.

    Args:
        query: 자연어 검색 질의. 한국어 + 영문 동의어 조합 권장 (3단어 이상).
        top_k: 반환 청크 수. 1~10 (기본 5).

    Returns:
        chunk_id + 본문 블록 목록.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    file_ids = sorted(_ctx_known_file_ids(ctx))
    thread_id = _thread_id(ctx)

    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    if not file_ids:
        return _err_msg(
            "검색할 자료가 없습니다 — 사용자가 지식베이스 자료를 선택하지 않았습니다.",
            tool_call_id,
        )

    curated_ids = _ids_from_state_files(state, _CURATED_PREFIX)
    seen_ids = _ids_from_state_files(state, _SEEN_PREFIX)

    if len(curated_ids) >= _CURATED_FULL_THRESHOLD:
        return _err_msg(
            f"이미 curated {len(curated_ids)}/{_CURATED_FULL_THRESHOLD} 개 (충분). 추가 검색 금지.",
            tool_call_id,
        )
    if len(seen_ids) >= _SEEN_LIMIT:
        return _err_msg(
            f"이미 seen {len(seen_ids)}/{_SEEN_LIMIT} 개 검토 (한도 도달). 종료하세요.",
            tool_call_id,
        )

    k = max(1, min(int(top_k or 5), _MAX_TOP_K))
    try:
        chunks = await memento.search(
            query=query, tenant_id=tenant_id, file_ids=file_ids, top_k=k,
            exclude_chunk_ids=seen_ids,
        )
    except Exception as exc:
        logger.warning("search_documents fail: %s", exc)
        return _err_msg(f"검색 중 오류: {exc}", tool_call_id)

    if not chunks:
        return _err_msg(
            f"query='{query}' 로 선택 자료에서 청크 없음. 다른 query 로 다시 시도.",
            tool_call_id,
        )

    body = _ingest_and_format(chunks, thread_id)
    files_update: dict = {}
    new_seen_count = 0
    for c in chunks:
        cid = (getattr(c, "chunk_id", "") or "").strip()
        if not cid:
            continue
        files_update[f"{_SEEN_PREFIX}{cid}"] = {"content": cid, "encoding": "utf-8"}
        new_seen_count += 1

    status = _status_line(len(seen_ids) + new_seen_count, len(curated_ids))
    out = (body or "(검색 결과 없음)") + "\n\n" + status

    return Command(update={
        "files": files_update,
        "messages": [ToolMessage(content=out, tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2 — keep_chunk
# ─────────────────────────────────────────────────────────────────────────────

@tool(parse_docstring=True)
async def keep_chunk(
    chunk_id: str,
    *,
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """검색 결과 중 답에 도움되는 청크를 큐레이션한다.

    호출 시 청크 본문이 /curated/<chunk_id>.md 에 저장되어 에이전트가 읽을 수 있게 된다.

    Args:
        chunk_id: search_documents 응답에 표시된 chunk_id= 값을 그대로 복사.

    Returns:
        큐레이션 결과 메시지.
    """
    ctx = _ctx(config)
    thread_id = _thread_id(ctx)
    cid = (chunk_id or "").strip()
    if not cid:
        return _err_msg("chunk_id 가 비어 있습니다.", tool_call_id)

    rec = _store_get(thread_id, cid)
    if rec is None:
        return _err_msg(
            f"chunk_id '{cid}' 를 찾지 못했습니다. search_documents 결과의 chunk_id= 값을 그대로 복사하세요.",
            tool_call_id,
        )

    body = rec["body"]
    meta = rec["meta"]
    path = f"/curated/{cid}.md"
    frontmatter_lines = [
        "---",
        f"chunk_id: {cid}",
        f"file_name: {meta.get('file_name', '')}",
        f"file_id: {meta.get('file_id', '')}",
        f"chunk_index: {meta.get('chunk_index', '')}",
        f"page_number: {meta.get('page_number', '')}",
        f"cite: {meta.get('cite', '')}",
        "---",
        "",
        body,
    ]
    content = "\n".join(str(x) for x in frontmatter_lines)

    return Command(update={
        "files": {path: {"content": content, "encoding": "utf-8"}},
        "messages": [ToolMessage(
            content=f"kept: {meta.get('cite', cid)} → {path}",
            tool_call_id=tool_call_id,
        )],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3 — retrieve_and_stage
# ─────────────────────────────────────────────────────────────────────────────

@tool(parse_docstring=True)
async def retrieve_and_stage(
    query: str,
    top_k: int = 5,
    *,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """``query`` 로 자료를 의미 검색해 결과를 /staging/ 에 저장한다.

    청크 본문은 컨텍스트에 반환하지 않고 /staging/ 가상 디렉토리에 저장.
    에이전트가 ls/read_file 로 읽는다.

    Args:
        query: 검색 질의. 한국어 + 영문 동의어 조합 권장.
        top_k: 검색 청크 수. 1~10 (기본 5).

    Returns:
        staged 청크 수 요약 메시지.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    file_ids = sorted(_ctx_known_file_ids(ctx))
    thread_id = _thread_id(ctx)

    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    if not file_ids:
        return _err_msg(
            "검색할 자료가 없습니다 — 사용자가 지식베이스 자료를 선택하지 않았습니다.",
            tool_call_id,
        )

    existing_files: dict = state.get("files") or {}
    existing_queries: set[str] = set()
    for path, fdata in existing_files.items():
        if not path.startswith(_STAGING_PREFIX):
            continue
        content = fdata.get("content", "") if isinstance(fdata, dict) else ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("query:"):
                existing_queries.add(stripped[len("query:"):].strip())
                break
    if len(existing_queries) >= _MAX_STAGING_CALLS and query not in existing_queries:
        return _err_msg(
            f"검색 계획 최대 개수({_MAX_STAGING_CALLS}개)를 초과했습니다. /staging/ 에 이미 충분한 정보가 있습니다.",
            tool_call_id,
        )

    k = max(1, min(int(top_k or 5), _MAX_TOP_K))
    try:
        chunks = await memento.search(
            query=query, tenant_id=tenant_id, file_ids=file_ids, top_k=k,
            exclude_chunk_ids=[],
        )
    except Exception as exc:
        logger.warning("retrieve_and_stage fail: %s", exc)
        return _err_msg(f"검색 중 오류: {exc}", tool_call_id)

    if not chunks:
        return _err_msg(
            f"query='{query}' 로 선택 자료에서 청크 없음. 다른 query 로 다시 시도.",
            tool_call_id,
        )

    _gc_chunk_store()
    files_update: dict = {}
    staged_ids: list[str] = []

    for i, c in enumerate(chunks):
        cid = (getattr(c, "chunk_id", "") or "").strip()
        if not cid:
            fid_short = (getattr(c, "file_id", "") or "")[-12:]
            idx = getattr(c, "chunk_index", None)
            cid = f"{fid_short}-{idx if idx is not None else i}"

        meta = {
            "file_name": getattr(c, "file_name", "") or "",
            "file_id": getattr(c, "file_id", "") or "",
            "chunk_index": getattr(c, "chunk_index", None),
            "page_number": getattr(c, "page_number", None),
            "source": getattr(c, "source", None),
            "cite": c.cite() if hasattr(c, "cite") else "",
        }
        body = (getattr(c, "text", None) or "").strip()
        if not body:
            continue

        _store_put(thread_id, cid, body, meta)

        frontmatter_lines = [
            "---",
            f"chunk_id: {cid}",
            f"file_name: {meta.get('file_name', '')}",
            f"file_id: {meta.get('file_id', '')}",
            f"chunk_index: {meta.get('chunk_index', '')}",
            f"page_number: {meta.get('page_number', '')}",
            f"cite: {meta.get('cite', '')}",
            f"query: {query}",
            "---",
            "",
            body,
        ]
        content = "\n".join(str(x) for x in frontmatter_lines)

        raw_name = meta.get("file_name", "") or cid
        stem = raw_name.rsplit(".", 1)[0][:20]
        page = meta.get("page_number", "")
        cidx = meta.get("chunk_index", "")
        cid_short = cid[:8] if len(cid) >= 8 else cid
        staged_name = f"{stem}_p{page}_c{cidx}_{cid_short}.md"

        files_update[f"{_STAGING_PREFIX}{staged_name}"] = {"content": content, "encoding": "utf-8"}
        staged_ids.append(cid)

    if not staged_ids:
        return _err_msg("검색 결과가 있었으나 본문이 모두 비어 있습니다.", tool_call_id)

    summary = (
        f"staged {len(staged_ids)}개 → /staging/ "
        f"(query='{query}', searched_files={len(file_ids)}개)\n"
        f"chunk_ids: {staged_ids}"
    )
    return Command(update={
        "files": files_update,
        "messages": [ToolMessage(content=summary, tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4 — list_documents
# ─────────────────────────────────────────────────────────────────────────────

_DOC_ROLE_DESC = {
    "content": "일반 검색·요약 대상 본문",
    "glossary": "용어 사전",
    "template": "보고서 양식",
    "reference": "참조 자료",
}


def _format_doc_card_md(card) -> str:
    role = (getattr(card, "doc_role", None) or "content")
    fm_lines = [
        "---",
        f"file_name: {card.file_name}",
        f"n_pages: {card.n_pages if card.n_pages is not None else ''}",
        f"folder_path: {card.folder_path or ''}",
        f"mime_type: {card.mime_type or ''}",
        f"source_type: {card.source_type or ''}",
        f"index_status: {card.index_status or ''}",
        f"doc_role: {role}",
        "---",
        "",
    ]
    role_desc = _DOC_ROLE_DESC.get(role, "")
    if role_desc:
        fm_lines.extend([f"## Role: {role}", "", role_desc, ""])
    abstract = (card.abstract or "").strip()
    if abstract:
        fm_lines.extend(["## Abstract", "", abstract, ""])
    else:
        fm_lines.extend(["## Abstract", "", "(abstract 없음 — grep 으로 본문 직접 탐색 권장)", ""])
    return "\n".join(fm_lines)


def _list_catalog_file_names(state: dict) -> list[str]:
    files = (state or {}).get("files") or {}
    if not isinstance(files, dict):
        return []
    out: list[str] = []
    for path, fdata in files.items():
        if not isinstance(path, str) or not path.startswith(_CATALOG_PREFIX):
            continue
        content = fdata.get("content", "") if isinstance(fdata, dict) else ""
        for line in (content or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("file_name:"):
                val = stripped[len("file_name:"):].strip()
                if val:
                    out.append(val)
                break
    return out


@tool
async def list_documents(
    *,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """사용자가 선택한 자료의 카탈로그를 가져온다.

    각 문서의 doc_card(abstract, n_pages 등)를 /catalog/ 에 저장하고 요약 목록을 반환.
    어떤 자료가 있는지 파악할 때 첫 단계로 호출. 보통 한 번이면 충분.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    file_ids = sorted(_ctx_known_file_ids(ctx))

    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    if not file_ids:
        return _err_msg("검색할 자료가 없습니다 — 사용자가 지식베이스 자료를 선택하지 않았습니다.", tool_call_id)

    if _count_path_prefix(state, _CATALOG_PREFIX) >= _NAV_LIST_DOC_LIMIT:
        existing = _list_catalog_file_names(state)
        return _err_msg(
            f"카탈로그는 이미 /catalog/ 에 있습니다. read_file 로 읽으세요. 파일 수: {len(existing)}",
            tool_call_id,
        )

    try:
        cards = await memento.list_catalog(tenant_id=tenant_id, file_ids=file_ids)
    except Exception as exc:
        logger.warning("list_documents fail: %s", exc)
        return _err_msg(f"카탈로그 조회 중 오류: {exc}", tool_call_id)

    if not cards:
        return _err_msg("선택한 자료에 대한 카탈로그가 비어 있습니다.", tool_call_id)

    files_update: dict = {}
    summary_lines: list[str] = []
    for card in cards:
        if not card.file_name:
            continue
        safe = _safe_path_segment(card.file_name)
        path = f"{_CATALOG_PREFIX}{safe}.md"
        files_update[path] = {"content": _format_doc_card_md(card), "encoding": "utf-8"}
        n_pg = f", {card.n_pages}p" if card.n_pages is not None else ""
        abstract_brief = (card.abstract or "").strip().replace("\n", " ")[:160] or "(요약 없음)"
        role = (getattr(card, "doc_role", None) or "content")
        role_tag = "" if role == "content" else f" [{role}]"
        summary_lines.append(f"- {card.file_name}{n_pg}{role_tag}: {abstract_brief}")

    if not files_update:
        return _err_msg("카탈로그를 만들 수 있는 자료가 없습니다.", tool_call_id)

    out = (
        f"카탈로그 {len(files_update)}개 항목을 /catalog/ 에 저장했습니다.\n\n"
        + "\n".join(summary_lines)
    )
    return Command(update={
        "files": files_update,
        "messages": [ToolMessage(content=out, tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5 — grep_in_document
# ─────────────────────────────────────────────────────────────────────────────

@tool(parse_docstring=True)
async def grep_in_document(
    file_name: str,
    pattern: str,
    regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 0,
    limit: int = 20,
    *,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """한 문서 안에서 정확한 토큰/패턴이 어느 페이지·라인에 있는지 찾는다.

    매칭 결과는 페이지번호 + 스니펫만 반환. 본문 전체는 read_document_page 로.

    Args:
        file_name: /catalog/ 에서 확인한 파일 이름. 확장자 포함.
        pattern: 검색 패턴. literal substring 또는 정규식.
        regex: True 면 pattern 을 정규식으로 해석.
        case_sensitive: 기본 False (대소문자 무시).
        context_lines: 매칭 라인 좌우 같이 반환할 라인 수 (0~5).
        limit: 최대 매칭 수 (1~50, 기본 20).

    Returns:
        매칭 위치(페이지, 라인) + 스니펫 목록.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    fname = (file_name or "").strip()
    pat = (pattern or "").strip()
    if not fname or not pat:
        return _err_msg("file_name 과 pattern 은 필수입니다.", tool_call_id)

    grep_log_count = _count_path_prefix(state, _GREP_LOG_PREFIX)
    if grep_log_count >= _NAV_MAX_GREP_CALLS:
        return _err_msg(
            f"grep 누적 호출 한도({_NAV_MAX_GREP_CALLS}) 도달. 더 이상 grep 안 됨.",
            tool_call_id,
        )

    lim = max(1, min(int(limit or 20), 50))
    ctx_l = max(0, min(int(context_lines or 0), 5))

    try:
        result = await memento.grep_document(
            tenant_id=tenant_id, file_name=fname, pattern=pat,
            regex=bool(regex), case_sensitive=bool(case_sensitive),
            context_lines=ctx_l, limit=lim,
        )
    except Exception as exc:
        logger.warning("grep_in_document fail: %s", exc)
        return _err_msg(f"grep 중 오류: {exc}", tool_call_id)

    if result.get("error"):
        return _err_msg(f"grep 실패: {result['error']}", tool_call_id)

    import re as _re_marker
    marker_key = _re_marker.sub(r"[^\w.\-]+", "_", f"{fname}__{pat}")[:80]
    marker_path = f"{_GREP_LOG_PREFIX}{grep_log_count + 1:02d}_{marker_key}.txt"
    marker_files = {marker_path: {"content": f"{fname} | {pat}", "encoding": "utf-8"}}
    remaining = _NAV_MAX_GREP_CALLS - (grep_log_count + 1)
    status_tail = f"\n[grep 남은 호출: {remaining}/{_NAV_MAX_GREP_CALLS}]"

    hits = result.get("hits") or []
    if not hits:
        return Command(update={
            "files": marker_files,
            "messages": [ToolMessage(
                content=(
                    f"'{fname}' 에서 패턴 '{pat}' 매칭 없음. 다른 단어/약어로 재시도."
                    + status_tail
                ),
                tool_call_id=tool_call_id,
            )],
        })

    out_lines = [
        f"Found {len(hits)} match(es) in '{fname}'"
        + (f" (total {result['total_matches']}, truncated)" if result.get("truncated") else f" (total {result['total_matches']})")
        + ":"
    ]
    for h in hits:
        out_lines.append(f"  [p.{h.page}, line {h.line}] {h.snippet}")
        if ctx_l > 0 and h.context:
            for ctx_line in h.context.splitlines():
                out_lines.append(f"      {ctx_line}")
    out_lines.append("\n→ 통째로 읽으려면 read_document_page(file_name, pages=\"N\" 또는 \"N-M\") 호출." + status_tail)

    return Command(update={
        "files": marker_files,
        "messages": [ToolMessage(content="\n".join(out_lines), tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 6 — read_document_page
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_page_count(spec: str) -> int:
    if not spec:
        return 0
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                total += max(1, int(b.strip()) - int(a.strip()) + 1)
            except (ValueError, AttributeError):
                total += 1
        else:
            total += 1
    return total


@tool(parse_docstring=True)
async def read_document_page(
    file_name: str,
    pages: str,
    *,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """문서의 특정 페이지(범위)를 통째로 읽어 본문을 직접 반환한다.

    Args:
        file_name: /catalog/ 에서 확인한 파일 이름. 확장자 포함.
        pages: 페이지 지정. ``"5"`` / ``"5-8"`` / ``"5,7,12"`` / ``"3-5,9"`` 형식.
            한 번 호출에 최대 10페이지.

    Returns:
        cite frontmatter + 페이지별 본문.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    fname = (file_name or "").strip()
    pages_spec = (pages or "").strip()
    if not fname or not pages_spec:
        return _err_msg("file_name 과 pages 는 필수입니다.", tool_call_id)

    requested_pages = _estimate_page_count(pages_spec)
    if requested_pages > _NAV_MAX_PAGES_PER_CALL:
        return _err_msg(
            f"read_document_page 1회 호출은 최대 {_NAV_MAX_PAGES_PER_CALL}페이지. "
            f"요청 {requested_pages}페이지 — 작은 범위로 나눠 호출하세요.",
            tool_call_id,
        )

    read_log_count = _count_path_prefix(state, _READ_LOG_PREFIX)
    if read_log_count >= _NAV_MAX_READ_CALLS:
        return _err_msg(
            f"read_document_page 누적 호출 한도({_NAV_MAX_READ_CALLS}) 도달. 정리해서 종료하세요.",
            tool_call_id,
        )

    files_in_state = (state or {}).get("files") or {}
    accumulated_pages = 0
    for path, fdata in files_in_state.items():
        if not isinstance(path, str) or not path.startswith(_READ_LOG_PREFIX):
            continue
        content = fdata.get("content", "") if isinstance(fdata, dict) else ""
        first_line = (content or "").splitlines()[0] if content else ""
        if first_line.startswith("count:"):
            try:
                accumulated_pages += int(first_line[len("count:"):].strip())
            except ValueError:
                pass

    if accumulated_pages >= _NAV_MAX_PAGES_TOTAL:
        return _err_msg(
            f"누적 페이지 한도({_NAV_MAX_PAGES_TOTAL}) 도달 — 더 이상 페이지 읽기 불가.",
            tool_call_id,
        )

    try:
        result = await memento.read_pages(tenant_id=tenant_id, file_name=fname, pages=pages_spec)
    except Exception as exc:
        logger.warning("read_document_page fail: %s", exc)
        return _err_msg(f"페이지 읽기 중 오류: {exc}", tool_call_id)

    if result.get("error"):
        return _err_msg(f"페이지 읽기 실패: {result['error']}", tool_call_id)

    page_items = result.get("pages") or []
    if not page_items:
        return _err_msg(
            f"'{fname}' 에서 페이지 {pages_spec} 본문이 비어 있습니다.",
            tool_call_id,
        )

    nums = sorted([p.page_number for p in page_items if p.page_number])
    range_label = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else f"{nums[0]}" if nums else pages_spec

    body_lines: list[str] = [
        "---",
        f"file_name: {fname}",
        f"pages: {range_label}",
        f"page_count: {len(page_items)}",
        f"cite: [{fname}, p.{range_label}]",
        "---",
        "",
    ]
    for p in page_items:
        body_lines.append(f"## p.{p.page_number}")
        body_lines.append("")
        body_lines.append(p.content or "")
        body_lines.append("")

    read_marker_path = f"{_READ_LOG_PREFIX}{read_log_count + 1:02d}_{_safe_path_segment(fname, 40)}_p{range_label}.txt"
    files_update = {
        read_marker_path: {
            "content": f"count:{len(page_items)}\nfile:{fname}\npages:{range_label}",
            "encoding": "utf-8",
        },
    }
    remaining_calls = _NAV_MAX_READ_CALLS - (read_log_count + 1)
    remaining_pages = _NAV_MAX_PAGES_TOTAL - (accumulated_pages + len(page_items))

    body_text = "\n".join(body_lines)
    footer = (
        f"\n[read_document_page 남은 호출: {remaining_calls}/{_NAV_MAX_READ_CALLS}, "
        f"남은 페이지: {remaining_pages}/{_NAV_MAX_PAGES_TOTAL}]"
    )
    return Command(update={
        "files": files_update,
        "messages": [ToolMessage(content=body_text + footer, tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# Tool 7 — summarize_document
# ─────────────────────────────────────────────────────────────────────────────

@tool(parse_docstring=True)
async def summarize_document(
    file_name: str,
    *,
    config: RunnableConfig,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """문서 1개를 페이지 batch 단위로 서버 측 병렬 mini-summary 처리한다.

    호출 1번에 그 문서의 모든 batch 요약 결과를 받음.
    각 batch 본문은 /summary/ 가상 파일로 저장됨.

    Args:
        file_name: /catalog/ 에서 확인한 파일 이름. 확장자 포함.

    Returns:
        batch 개수 + 페이지 수 요약 메시지.
    """
    ctx = _ctx(config)
    tenant_id = str(ctx.get("tenant_id") or "")
    if not tenant_id:
        return _err_msg("tenant_id 가 비어 있습니다.", tool_call_id)
    fname = (file_name or "").strip()
    if not fname:
        return _err_msg("file_name 은 필수입니다.", tool_call_id)

    try:
        result = await memento.summarize_document(tenant_id=tenant_id, file_name=fname)
    except Exception as exc:
        logger.warning("summarize_document fail: %s", exc)
        return _err_msg(f"요약 처리 중 오류: {exc}", tool_call_id)

    errors = result.get("errors") or []
    batches = result.get("batches") or []
    if not batches:
        err_msg_text = "; ".join(errors[:3]) if errors else "no batches returned"
        return _err_msg(f"'{fname}' 요약 결과 없음. {err_msg_text}", tool_call_id)

    n_pages = int(result.get("n_pages") or 0)
    n_batches = int(result.get("n_batches") or 0)
    abstract = str(result.get("abstract") or "")
    safe = _safe_path_segment(fname)

    files_update: dict = {}
    summary_lines: list[str] = []

    if abstract:
        files_update[f"{_SUMMARY_PREFIX}{safe}_abstract.md"] = {
            "content": (
                "---\n"
                f"file_name: {fname}\n"
                f"n_pages: {n_pages}\n"
                "type: abstract\n"
                "---\n\n"
                f"{abstract}\n"
            ),
            "encoding": "utf-8",
        }

    ok_count = 0
    for b in batches:
        batch_range = b.batch_range
        if not batch_range:
            continue
        if b.ok and b.summary:
            ok_count += 1
        path = f"{_SUMMARY_PREFIX}{safe}_p{batch_range}.md"
        body = b.summary if (b.ok and b.summary) else f"[batch p.{batch_range} 요약 실패: {b.error or 'unknown'}]"
        files_update[path] = {
            "content": (
                "---\n"
                f"file_name: {fname}\n"
                f"pages: {batch_range}\n"
                f"page_count: {b.page_count}\n"
                f"cite: [{fname}, p.{batch_range}]\n"
                "type: mini_summary\n"
                f"ok: {str(b.ok).lower()}\n"
                "---\n\n"
                f"{body}\n"
            ),
            "encoding": "utf-8",
        }
        summary_lines.append(
            f"- p.{batch_range} ({b.page_count}p): "
            + (f"{len(b.summary)}자 요약" if b.ok else f"실패 — {b.error[:80]}")
        )

    msg = (
        f"'{fname}' ({n_pages}p) — {n_batches}개 batch 요약 완료 (성공 {ok_count}/{n_batches}).\n"
        f"각 batch 본문은 /summary/ 에 stage.\n\n"
        + "\n".join(summary_lines)
    )
    if errors:
        msg += "\n\n[처리 중 오류]\n- " + "\n- ".join(errors[:5])

    return Command(update={
        "files": files_update,
        "messages": [ToolMessage(content=msg, tool_call_id=tool_call_id)],
    })


# ─────────────────────────────────────────────────────────────────────────────
# 도구 목록 반환
# ─────────────────────────────────────────────────────────────────────────────

def get_memento_tools() -> list:
    return [
        search_documents,
        keep_chunk,
        retrieve_and_stage,
        list_documents,
        grep_in_document,
        read_document_page,
        summarize_document,
    ]
