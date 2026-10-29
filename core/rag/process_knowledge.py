"""프로세스 단계 에이전트가 읽을 지식 베이스 문서.

채팅 턴의 검색 범위는 사용자가 고른 자료다(metadata.knowledge_docs·knowledge_folders·업로드).
할 일(todolist) 실행에는 고를 사람이 없어 범위가 비었고, RAG 도구는 빈 범위를 「자료 없음」으로
거절했다 — 규정 PDF 로 만든 프로세스의 견적 산출 에이전트가 같은 지식 베이스 폴더의 가격표를
보지 못하고 0원 견적을 냈다(튜토리얼 VM, ``knowledge_doc_ids 구성 | n=0``).

- 생성: 정의를 만든 대화의 지식 베이스 선택(문서와 그 문서가 든 폴더)을 정의와 에이전트 단계에
  남긴다 — ``definition.knowledgeDocs / knowledgeFolders`` 와 에이전트 단계의
  ``activity.knowledgeDocs / knowledgeFolders``. 단계에도 적는 까닭: 편집기 저장은 정의를 BPMN XML 에서
  다시 만들며 단계의 이전 값은 합쳐 두지만(mergeProcessDefinitionActivities) 정의 최상위 값은 버린다.
- 실행: 단계 → 정의 순으로 찾고, 폴더는 memento 카탈로그로 그 안의 문서로 펼친다. 아무것도 없으면
  테넌트 지식 베이스 전체(memento 카탈로그 규칙대로 채팅 첨부는 뺀다)를 범위로 쓴다.

``knowledgeDocs`` 항목: ``{"id": <memento file_id = storage 경로>, "file_name": ..., "folder_path": ...}``.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any, Callable, Optional

from core.rag import memento

logger = logging.getLogger(__name__)

DOCS_KEY = "knowledgeDocs"
FOLDERS_KEY = "knowledgeFolders"

ORIGIN_ACTIVITY = "activity"
ORIGIN_PROCESS = "process"
ORIGIN_TENANT = "tenant"

# 테넌트 전체 폴백에서 범위로 넘길 문서 수 상한. memento /search 는 file_ids 를 쿼리 문자열로 받는다.
TENANT_FALLBACK_MAX_DOCS = 200
# 단계 지시문에 이름을 적어 줄 문서 수 상한(나머지는 「외 N건」).
NOTE_MAX_NAMES = 20
# 채팅 첨부의 memento file_id 접두어(memento knowledge_files._CHAT_ATTACHMENT_PREFIXES 와 같다).
# 테넌트 전체 폴백에는 넣지 않는다 — 남의 대화방에 올린 파일이 다른 사람의 업무 단계로 새지 않게.
# memento 카탈로그도 범위 없는 조회에서 이들을 빼지만, knowledge_files 가 비면 processed_files 로
# 폴백하며 다시 섞는다.
_CHAT_ATTACHMENT_PREFIXES = ("files/", "session/")

# 이번 채팅 턴의 지식 베이스 선택 — executor 가 턴 시작 때 넣고, 생성 마무리(_complete_one_process)가
# 정의에 남긴다. 마무리는 asyncio.to_thread 로 돌아 컨텍스트가 그대로 넘어간다.
_generation_knowledge: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "process_generation_knowledge", default=None
)


# ---------------------------------------------------------------------------
# 값 정리
# ---------------------------------------------------------------------------

def _clean_folder(value: Any) -> str:
    return str(value or "").strip().strip("/")


def normalize_docs(value: Any, normalize_id: Callable[[Any], str] | None = None) -> list[dict]:
    """문서 목록을 ``{id, file_name, folder_path}`` 로 맞춘다. 문자열은 id 로 본다. id 로 중복 제거."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            raw_id = (
                item.get("id") or item.get("file_id") or item.get("fileId")
                or item.get("sourceRef") or item.get("source_ref")
                or item.get("file_path") or item.get("filePath") or ""
            )
            name = item.get("file_name") or item.get("fileName") or item.get("name") or ""
            folder = item.get("folder_path") if "folder_path" in item else item.get("folderPath")
        else:
            raw_id, name, folder = item, "", ""
        doc_id = normalize_id(raw_id) if normalize_id else str(raw_id or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append({"id": doc_id, "file_name": str(name or "").strip(), "folder_path": _clean_folder(folder)})
    return out


def normalize_folders(value: Any) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        folder = _clean_folder(item)
        if folder and folder not in out:
            out.append(folder)
    return out


def _with_doc_folders(docs: list[dict], folders: list[str]) -> list[str]:
    """선택한 폴더에 각 문서가 든 폴더를 더한다 — 같은 폴더의 다른 자료(규정 옆의 가격표)까지 닿게.

    폴더 없이 루트에 둔 문서는 더하지 않는다(루트 = 지식 베이스 전체라 범위가 무너진다).
    """
    out = list(folders)
    for doc in docs:
        folder = _clean_folder(doc.get("folder_path"))
        if folder and folder not in out:
            out.append(folder)
    return out


# ---------------------------------------------------------------------------
# 생성 — 대화의 선택을 정의에 남긴다
# ---------------------------------------------------------------------------

def chat_selection(
    metadata: dict | None,
    extras: dict | None,
    normalize_id: Callable[[Any], str] | None = None,
) -> dict:
    """채팅 턴의 지식 베이스 선택 — ``{"docs": [...], "folders": [...]}``.

    프론트는 고른 문서·이번 메시지 첨부를 ``metadata.knowledge_docs`` 로(id = memento file_id),
    폴더째 고른 것은 ``metadata.knowledge_folders`` 로 보낸다.
    """
    md = metadata if isinstance(metadata, dict) else {}
    ex = extras if isinstance(extras, dict) else {}
    docs = normalize_docs(
        list(md.get("knowledge_docs") or []) + list(ex.get("sources") or []),
        normalize_id,
    )
    return {"docs": docs, "folders": _with_doc_folders(docs, normalize_folders(md.get("knowledge_folders")))}


def set_generation_knowledge(selection: dict | None) -> None:
    _generation_knowledge.set(selection if selection and (selection.get("docs") or selection.get("folders")) else None)


def generation_knowledge() -> Optional[dict]:
    return _generation_knowledge.get()


def is_agent_step(activity: Any) -> bool:
    """에이전트가 처리하는 단계인가 — 담당 에이전트가 있거나 에이전트 모드가 켜졌다."""
    if not isinstance(activity, dict):
        return False
    if str(activity.get("agent") or "").strip():
        return True
    return str(activity.get("agentMode") or "").strip().lower() not in ("", "none")


def _declared(activity: dict) -> tuple[list[dict], list[str]]:
    return normalize_docs(activity.get(DOCS_KEY)), normalize_folders(activity.get(FOLDERS_KEY))


def stamp_definition(
    definition: dict,
    selection: dict | None = None,
    *,
    source: dict | None = None,
) -> None:
    """정의(flattened 또는 elements[])에 지식 베이스 자료를 적는다.

    Args:
        definition: 적을 정의. 제자리에서 고친다.
        selection: 대화의 선택. 없으면 이번 턴의 선택(``generation_knowledge()``).
        source: 정의를 만든 원본(elements[]). flatten 이 모르는 키를 버리므로, 모델이 원본에
            단계별로 적어 둔 값과 정의 최상위 값을 여기서 되살린다.

    규칙: 에이전트 단계마다 이미 적힌 값(모델이 좁혀 둔 단계별 자료)은 그대로 두고, 없으면 정의
    전체의 자료를 적는다. 사람 단계는 건드리지 않는다.
    """
    if not isinstance(definition, dict):
        return
    if selection is None:
        selection = generation_knowledge()
    sel_docs = normalize_docs((selection or {}).get("docs"))
    sel_folders = normalize_folders((selection or {}).get("folders"))

    src = source if isinstance(source, dict) else {}
    # 이전 마무리가 남긴 정의 전체 값(고치기 차례에 다시 마무리할 때). 자료는 늘기만 한다.
    prev_docs = normalize_docs(definition.get(DOCS_KEY) or src.get(DOCS_KEY))
    prev_folders = normalize_folders(definition.get(FOLDERS_KEY) or src.get(FOLDERS_KEY))
    proc_docs = normalize_docs(prev_docs + sel_docs)
    proc_folders = normalize_folders(prev_folders + sel_folders)

    def _is_previous_stamp(docs: list[dict], folders: list[str]) -> bool:
        # 단계 값이 이전 정의 전체 값을 그대로 옮겨 둔 것이면 모델이 좁힌 값이 아니다 — 새 값을 따른다.
        return bool(prev_docs or prev_folders) and (
            [d["id"] for d in docs] == [d["id"] for d in prev_docs] and folders == prev_folders
        )

    source_by_id: dict[str, dict] = {}
    for el in src.get("elements") or []:
        if isinstance(el, dict) and el.get("id"):
            source_by_id.setdefault(str(el["id"]), el)

    def _activities(defn: dict) -> list[dict]:
        acts = [a for a in (defn.get("activities") or []) if isinstance(a, dict)]
        acts += [
            e for e in (defn.get("elements") or [])
            if isinstance(e, dict) and e.get("elementType") == "Activity"
        ]
        for sub in defn.get("subProcesses") or []:
            children = sub.get("children") if isinstance(sub, dict) else None
            if isinstance(children, dict):
                acts += _activities(children)
        return acts

    stamped = 0
    for act in _activities(definition):
        if not is_agent_step(act):
            continue
        docs, folders = _declared(act)
        if not (docs or folders):
            docs, folders = _declared(source_by_id.get(str(act.get("id") or ""), {}))
        if not (docs or folders) or _is_previous_stamp(docs, folders):
            docs, folders = proc_docs, proc_folders
        if not (docs or folders):
            continue
        act[DOCS_KEY] = [dict(d) for d in docs]
        act[FOLDERS_KEY] = list(folders)
        stamped += 1

    if proc_docs or proc_folders:
        definition[DOCS_KEY] = proc_docs
        definition[FOLDERS_KEY] = proc_folders
    if proc_docs or proc_folders or stamped:
        logger.info(
            "정의에 지식 베이스 자료 기록 | docs=%d folders=%s agent_steps=%d",
            len(proc_docs), proc_folders, stamped,
        )


# ---------------------------------------------------------------------------
# 실행 — 단계가 읽을 문서를 정한다
# ---------------------------------------------------------------------------

def declared_scope(definition: Any, activity_id: str) -> tuple[list[dict], list[str], str]:
    """정의에 적힌 단계의 자료 — (문서, 폴더, 출처). 단계 값이 정의 전체 값보다 앞선다."""
    if not isinstance(definition, dict):
        return [], [], ""
    from core.chat.activity import _find_activity

    act = _find_activity(definition, activity_id)
    if act is not None:
        docs, folders = _declared(act)
        if docs or folders:
            return docs, folders, ORIGIN_ACTIVITY
    docs, folders = _declared(definition)
    if docs or folders:
        return docs, folders, ORIGIN_PROCESS
    return [], [], ""


def _card_doc(card: Any) -> dict | None:
    file_id = str(getattr(card, "file_id", "") or "").strip()
    if not file_id or str(getattr(card, "index_status", "") or "") == "excluded":
        return None
    return {
        "id": file_id,
        "file_name": str(getattr(card, "file_name", "") or ""),
        "folder_path": _clean_folder(getattr(card, "folder_path", "")),
    }


async def resolve_task_knowledge(
    tenant_id: str,
    definition: Any,
    activity_id: str,
) -> tuple[list[dict], str]:
    """할 일 실행 단계가 검색할 문서 — (문서 목록, 출처). 비면 테넌트 지식 베이스 전체로 폴백한다."""
    docs, folders, origin = declared_scope(definition, activity_id)
    out = normalize_docs(docs)
    if folders:
        cards = await memento.list_catalog(tenant_id=tenant_id, folder_paths=folders)
        out = normalize_docs(out + [d for d in map(_card_doc, cards) if d])
    if not out:
        # 정의에 자료가 없다(이 고침 전에 만든 정의, 자료 없이 만든 정의) — 아무것도 없는 것보다
        # 테넌트 지식 베이스를 찾게 한다. 범위 없는 카탈로그는 채팅 첨부를 뺀 지식 베이스다.
        cards = await memento.list_catalog(tenant_id=tenant_id)
        out = normalize_docs([
            d for d in map(_card_doc, cards)
            if d and not d["id"].startswith(_CHAT_ATTACHMENT_PREFIXES)
        ])
        if len(out) > TENANT_FALLBACK_MAX_DOCS:
            logger.warning(
                "지식 베이스 전체 폴백이 상한을 넘어 앞 %d건만 범위로 | tenant=%s total=%d",
                TENANT_FALLBACK_MAX_DOCS, tenant_id, len(out),
            )
            out = out[:TENANT_FALLBACK_MAX_DOCS]
        origin = ORIGIN_TENANT if out else ""
    return out, origin


async def prepare_task_knowledge(
    *,
    tenant_id: str,
    proc_def_id: str,
    activity_id: str,
) -> tuple[list[dict], str]:
    """executor 용 — 정의를 읽어 단계의 문서를 정한다. 어떤 실패도 실행을 막지 않는다(빈 목록)."""
    if not (tenant_id and proc_def_id and activity_id):
        return [], ""
    try:
        from core.chat.activity import _get_proc_def_definition

        definition = await asyncio.to_thread(_get_proc_def_definition, tenant_id, proc_def_id)
        docs, origin = await resolve_task_knowledge(tenant_id, definition, activity_id)
    except Exception:
        logger.warning(
            "단계 지식 베이스 자료 조회 실패(자료 없이 진행) | proc_def_id=%s activity_id=%s",
            proc_def_id, activity_id, exc_info=True,
        )
        return [], ""
    logger.info(
        "단계 지식 베이스 자료 | proc_def_id=%s activity_id=%s origin=%s n=%d names=%s",
        proc_def_id, activity_id, origin or "-", len(docs),
        [d.get("file_name") or d.get("id") for d in docs[:5]],
    )
    return docs, origin


_ORIGIN_LABEL = {
    ORIGIN_ACTIVITY: "이 단계에 지정된 자료",
    ORIGIN_PROCESS: "이 프로세스를 만든 자료와 그 폴더",
    ORIGIN_TENANT: "지식 베이스 전체(이 프로세스에 지정된 자료가 없음)",
}


def knowledge_note(docs: list[dict], origin: str) -> str:
    """단계 지시문 앞에 붙일 안내. 문서가 없으면 빈 문자열."""
    if not docs:
        return ""
    lines = []
    if origin == ORIGIN_TENANT and len(docs) > NOTE_MAX_NAMES:
        # 지식 베이스 전체에서 이름순 앞 몇 건은 이 단계와 무관한 우연한 목록이다 — 개수만 알린다.
        lines.append(f"- 지식 베이스 문서 {len(docs)}건 (이름은 list_documents 로 확인)")
    else:
        for doc in docs[:NOTE_MAX_NAMES]:
            name = doc.get("file_name") or doc.get("id")
            folder = doc.get("folder_path")
            lines.append(f"- {name}" + (f" ({folder})" if folder else ""))
        if len(docs) > NOTE_MAX_NAMES:
            lines.append(f"- 외 {len(docs) - NOTE_MAX_NAMES}건")
    return (
        f"[참고 자료 — 지식 베이스: {_ORIGIN_LABEL.get(origin, '지식 베이스')}]\n"
        + "\n".join(lines)
        + "\n가격·기준·수치가 필요하면 먼저 search_documents 로 위 자료를 찾아 그 값을 쓴다"
        " (목록은 list_documents, 원문 쪽은 read_document_page). 자료에 없는 값은 지어내지 말고"
        " 근거가 없다고 적는다."
    )
