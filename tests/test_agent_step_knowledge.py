"""할 일로 실행되는 에이전트 단계도 지식 베이스 문서를 찾는다.

튜토리얼 VM: 규정 PDF(지식 베이스 「한빛오피스-영업자료」)로 만든 「한빛오피스 대량 주문 견적 승인 프로세스」를
실행하자 견적 산출 에이전트가 같은 폴더의 가격표를 보지 못하고 0원 견적을 냈다 —
``knowledge_doc_ids 구성 | n=0``. 채팅은 사용자가 고른 자료가 검색 범위인데, 할 일 실행에는 범위를 채울
곳이 없었고 RAG 도구는 빈 범위를 「자료 없음」으로 거절했다.
"""
import asyncio
import json

import pytest

from core.chat import activity as activity_mod
from core.rag import memento
from core.rag import process_knowledge as pk
from core.skills import identified, tools

POLICY = {
    "id": "knowledge/localhost/f-bf8008e689/c0e61f45.pdf",
    "file_name": "한빛오피스-견적승인규정.pdf",
    "folder_path": "한빛오피스-영업자료",
}
PRICE = {
    "id": "knowledge/localhost/f-bf8008e689/a703d57c.pdf",
    "file_name": "한빛오피스-제품가격표.pdf",
    "folder_path": "한빛오피스-영업자료",
}


@pytest.fixture(autouse=True)
def _reset_context():
    room = tools._current_conversation_id.set("")
    kb = pk._generation_knowledge.set(None)
    yield
    tools._current_conversation_id.reset(room)
    pk._generation_knowledge.reset(kb)


def _card(doc, status="indexed"):
    return memento.DocCard(
        file_name=doc["file_name"], file_id=doc["id"], folder_path=doc["folder_path"], index_status=status,
    )


class _Catalog:
    """memento.list_catalog 대역 — 폴더 범위 조회와 범위 없는(테넌트 전체) 조회를 따로 돌려준다."""

    def __init__(self, by_folder=None, tenant=None):
        self.by_folder = by_folder or {}
        self.tenant = tenant or []
        self.calls = []

    async def __call__(self, *, tenant_id, file_ids=None, file_names=None, folder_paths=None):
        self.calls.append({"tenant_id": tenant_id, "folder_paths": list(folder_paths or [])})
        if folder_paths:
            return [c for f in folder_paths for c in self.by_folder.get(f, [])]
        return list(self.tenant)


# ---------------------------------------------------------------------------
# memento 클라이언트
# ---------------------------------------------------------------------------

class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"response": [{"file_id": PRICE["id"], "file_name": PRICE["file_name"],
                              "folder_path": PRICE["folder_path"], "index_status": "indexed"}]}


class _Client:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, list(params or [])))
        return _Resp()


def test_catalog_sends_folder_scope(monkeypatch):
    client = _Client()
    monkeypatch.setattr(memento, "_get_client", lambda: client)
    cards = asyncio.run(memento.list_catalog(tenant_id="localhost", folder_paths=["한빛오피스-영업자료", "규정/2026"]))
    url, params = client.calls[0]
    assert url == "/catalog"
    assert ("folder_paths", "한빛오피스-영업자료") in params and ("folder_paths", "규정/2026") in params
    assert cards[0].file_id == PRICE["id"] and cards[0].folder_path == "한빛오피스-영업자료"


# ---------------------------------------------------------------------------
# 생성 — 대화의 선택을 정의에 남긴다
# ---------------------------------------------------------------------------

def test_chat_selection_adds_the_folder_of_each_picked_doc():
    metadata = {
        # 프론트(ChatRoomPage)가 보내는 모양: id = sourceRef(memento file_id)
        "knowledge_docs": [
            {"id": POLICY["id"], "source_type": "upload", "file_name": POLICY["file_name"],
             "folder_path": POLICY["folder_path"]},
            {"id": "files/dd214dab.hwpx", "source_type": "storage", "file_name": "양식.hwpx", "folder_path": ""},
        ],
        "knowledge_folders": ["/규정/2026/"],
    }
    sel = pk.chat_selection(metadata, {}, None)
    assert [d["id"] for d in sel["docs"]] == [POLICY["id"], "files/dd214dab.hwpx"]
    # 고른 폴더 + 고른 문서가 든 폴더. 루트(폴더 없음)에 둔 첨부의 폴더는 넣지 않는다.
    assert sel["folders"] == ["규정/2026", "한빛오피스-영업자료"]


def test_chat_selection_normalizes_ids_like_the_executor():
    sources = [{"file_name": "가격표.pdf",
                "file_path": "http://h/storage/v1/object/public/files/knowledge/a.pdf?token=1"}]
    from executor import _normalize_doc_id

    sel = pk.chat_selection({}, {"sources": sources}, _normalize_doc_id)
    assert sel["docs"] == [{"id": "knowledge/a.pdf", "file_name": "가격표.pdf", "folder_path": ""}]


def _flat_definition():
    return {
        "processDefinitionId": "p1",
        "activities": [
            {"id": "collect_request", "name": "견적 요청 접수", "role": "영업담당자", "agentMode": "none"},
            {"id": "check_stock", "name": "재고 확인", "agent": "stock-agent", "agentMode": "draft"},
            {"id": "calculate_quote", "name": "견적 산출", "agent": "quote-agent", "agentMode": "draft"},
        ],
    }


def test_stamp_records_sources_on_definition_and_agent_steps_only():
    definition = _flat_definition()
    pk.stamp_definition(definition, {"docs": [POLICY], "folders": ["한빛오피스-영업자료"]})
    assert definition["knowledgeDocs"] == [POLICY]
    assert definition["knowledgeFolders"] == ["한빛오피스-영업자료"]
    acts = {a["id"]: a for a in definition["activities"]}
    assert "knowledgeDocs" not in acts["collect_request"]  # 사람 단계는 그대로
    for aid in ("check_stock", "calculate_quote"):
        assert acts[aid]["knowledgeDocs"] == [POLICY]
        assert acts[aid]["knowledgeFolders"] == ["한빛오피스-영업자료"]


def test_stamp_keeps_a_step_the_model_narrowed():
    definition = _flat_definition()
    source = {"elements": [
        {"elementType": "Activity", "id": "calculate_quote", "knowledgeDocs": [PRICE]},
    ]}
    pk.stamp_definition(definition, {"docs": [POLICY], "folders": ["한빛오피스-영업자료"]}, source=source)
    acts = {a["id"]: a for a in definition["activities"]}
    assert acts["calculate_quote"]["knowledgeDocs"] == [PRICE]
    assert acts["calculate_quote"]["knowledgeFolders"] == []
    assert acts["check_stock"]["knowledgeDocs"] == [POLICY]


def test_stamp_follows_a_new_selection_on_a_later_completion():
    definition = _flat_definition()
    pk.stamp_definition(definition, {"docs": [POLICY], "folders": ["한빛오피스-영업자료"]})
    # 고치기 차례: 이전 마무리가 단계에 옮겨 둔 값은 좁힌 값이 아니다 — 늘어난 선택을 따른다.
    pk.stamp_definition(definition, {"docs": [PRICE], "folders": []})
    acts = {a["id"]: a for a in definition["activities"]}
    assert [d["id"] for d in definition["knowledgeDocs"]] == [POLICY["id"], PRICE["id"]]
    assert [d["id"] for d in acts["check_stock"]["knowledgeDocs"]] == [POLICY["id"], PRICE["id"]]


def test_stamp_without_selection_changes_nothing():
    definition = _flat_definition()
    pk.stamp_definition(definition, None)
    assert "knowledgeDocs" not in definition
    assert all("knowledgeDocs" not in a for a in definition["activities"])


def _elements_definition():
    return {
        "processDefinitionName": "한빛오피스 대량 주문 견적 승인 프로세스",
        "processDefinitionId": "quote",
        "roles": [
            {"name": "영업담당자", "endpoint": "sales_rep"},
            {"name": "견적산출에이전트", "endpoint": "quote_agent"},
        ],
        "elements": [
            {"elementType": "Event", "id": "start", "name": "시작", "type": "StartEvent"},
            {"elementType": "Activity", "id": "collect_request", "name": "견적 요청 접수", "type": "UserActivity",
             "role": "영업담당자", "outputData": ["견적요청서"]},
            {"elementType": "Activity", "id": "calculate_quote", "name": "견적 산출", "type": "UserActivity",
             "role": "견적산출에이전트", "outputData": ["견적서초안"]},
            {"elementType": "Event", "id": "end", "name": "종료", "type": "EndEvent"},
            {"elementType": "Sequence", "id": "s1", "source": "start", "target": "collect_request"},
            {"elementType": "Sequence", "id": "s2", "source": "collect_request", "target": "calculate_quote"},
            {"elementType": "Sequence", "id": "s3", "source": "calculate_quote", "target": "end"},
        ],
    }


def test_completion_writes_the_chat_selection_into_the_saved_definition(tmp_path):
    tools._current_conversation_id.set("kb-room")
    identified._cache["kb-room"] = None
    work = tmp_path / "work"
    (work / "agents").mkdir(parents=True)
    (work / "process-definition.json").write_text(json.dumps(_elements_definition(), ensure_ascii=False),
                                                  encoding="utf-8")
    (work / "agents" / "quote_agent.json").write_text(json.dumps({
        "id": "quote_agent", "name": "견적 산출 에이전트", "role": "견적 산출 담당",
    }, ensure_ascii=False), encoding="utf-8")
    (work / "manifest.json").write_text(json.dumps({
        "skills": [], "agents": [{"id": "quote_agent", "activity_ids": ["calculate_quote"]}], "dmn": [],
    }), encoding="utf-8")
    pk.set_generation_knowledge(pk.chat_selection({"knowledge_docs": [POLICY]}, {}))

    out = tools._complete_one_process(work, persist=True)

    flat = out["processDefinition"]
    assert flat["knowledgeFolders"] == ["한빛오피스-영업자료"]
    acts = {a["id"]: a for a in flat["activities"]}
    assert acts["calculate_quote"]["agent"] == "quote_agent"
    assert acts["calculate_quote"]["knowledgeDocs"][0]["id"] == POLICY["id"]
    assert acts["calculate_quote"]["knowledgeFolders"] == ["한빛오피스-영업자료"]
    assert "knowledgeDocs" not in acts["collect_request"]
    # 프론트 「저장」은 작업 폴더의 이 파일(flattened)을 그대로 proc_def.definition 에 넣는다.
    saved = json.loads((work / "process-definition.json").read_text(encoding="utf-8"))
    assert saved["activities"] == flat["activities"]


# ---------------------------------------------------------------------------
# 실행 — 단계가 읽을 문서
# ---------------------------------------------------------------------------

def test_step_value_comes_before_the_process_value():
    definition = _flat_definition()
    definition["knowledgeDocs"] = [POLICY]
    definition["activities"][2]["knowledgeDocs"] = [PRICE]
    assert pk.declared_scope(definition, "calculate_quote") == ([PRICE], [], "activity")
    assert pk.declared_scope(definition, "check_stock") == ([POLICY], [], "process")
    assert pk.declared_scope(_flat_definition(), "check_stock") == ([], [], "")


def test_folder_is_expanded_to_the_documents_in_it(monkeypatch):
    catalog = _Catalog(by_folder={"한빛오피스-영업자료": [_card(POLICY), _card(PRICE)]})
    monkeypatch.setattr(memento, "list_catalog", catalog)
    definition = _flat_definition()
    pk.stamp_definition(definition, {"docs": [POLICY], "folders": ["한빛오피스-영업자료"]})

    docs, origin = asyncio.run(pk.resolve_task_knowledge("localhost", definition, "calculate_quote"))

    assert origin == "activity"
    assert [d["id"] for d in docs] == [POLICY["id"], PRICE["id"]]  # 고른 규정 + 같은 폴더의 가격표
    assert catalog.calls == [{"tenant_id": "localhost", "folder_paths": ["한빛오피스-영업자료"]}]


def test_definition_without_sources_falls_back_to_the_knowledge_base(monkeypatch):
    # 이 고침 전에 저장한 정의(VM 90216c1b…)는 자료가 비어 있다 — 아무것도 없는 것보다 지식 베이스 전체.
    excluded = dict(PRICE, id="knowledge/localhost/old.pdf", file_name="옛 가격표.pdf")
    chat_file = {"id": "files/dd214dab.hwpx", "file_name": "양식.hwpx", "folder_path": ""}
    catalog = _Catalog(tenant=[_card(POLICY), _card(PRICE), _card(excluded, status="excluded"), _card(chat_file)])
    monkeypatch.setattr(memento, "list_catalog", catalog)

    docs, origin = asyncio.run(pk.resolve_task_knowledge("localhost", _flat_definition(), "calculate_quote"))

    assert origin == "tenant"
    assert [d["id"] for d in docs] == [POLICY["id"], PRICE["id"]]
    assert catalog.calls == [{"tenant_id": "localhost", "folder_paths": []}]


def test_empty_folder_also_falls_back(monkeypatch):
    catalog = _Catalog(by_folder={}, tenant=[_card(PRICE)])
    monkeypatch.setattr(memento, "list_catalog", catalog)
    definition = _flat_definition()
    definition["knowledgeFolders"] = ["지운-폴더"]

    docs, origin = asyncio.run(pk.resolve_task_knowledge("localhost", definition, "check_stock"))

    assert (origin, [d["id"] for d in docs]) == ("tenant", [PRICE["id"]])


def test_tenant_fallback_is_capped(monkeypatch):
    many = [_card({"id": f"knowledge/t/{i}.pdf", "file_name": f"{i}.pdf", "folder_path": "a"}) for i in range(250)]
    monkeypatch.setattr(memento, "list_catalog", _Catalog(tenant=many))
    docs, _ = asyncio.run(pk.resolve_task_knowledge("t", {}, "x"))
    assert len(docs) == pk.TENANT_FALLBACK_MAX_DOCS


def test_prepare_reads_the_saved_definition(monkeypatch):
    definition = _flat_definition()
    pk.stamp_definition(definition, {"docs": [POLICY], "folders": []})
    seen = {}

    def _fake_get(tenant_id, proc_def_id):
        seen["args"] = (tenant_id, proc_def_id)
        return definition

    monkeypatch.setattr(activity_mod, "_get_proc_def_definition", _fake_get)
    monkeypatch.setattr(memento, "list_catalog", _Catalog())

    docs, origin = asyncio.run(pk.prepare_task_knowledge(
        tenant_id="localhost", proc_def_id="90216c1b", activity_id="calculate_quote",
    ))

    assert seen["args"] == ("localhost", "90216c1b")
    assert (origin, docs) == ("activity", [POLICY])


def test_prepare_never_breaks_the_run(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(activity_mod, "_get_proc_def_definition", _boom)
    assert asyncio.run(pk.prepare_task_knowledge(
        tenant_id="localhost", proc_def_id="p", activity_id="a",
    )) == ([], "")
    # 프로세스 단계가 아닌 할 일(정의·단계 id 없음)은 찾지 않는다.
    assert asyncio.run(pk.prepare_task_knowledge(tenant_id="localhost", proc_def_id="", activity_id="a")) == ([], "")


def test_note_lists_the_documents_and_tells_the_agent_to_search_them():
    note = pk.knowledge_note([POLICY, PRICE], "process")
    assert "한빛오피스-제품가격표.pdf (한빛오피스-영업자료)" in note
    assert "search_documents" in note and "지어내지" in note
    assert pk.knowledge_note([], "tenant") == ""
    many = [{"id": f"k/{i}.pdf", "file_name": f"{i}.pdf", "folder_path": ""} for i in range(25)]
    assert "외 5건" in pk.knowledge_note(many, "process")
    # 지식 베이스 전체 폴백이 길면 이름순 앞 몇 건 대신 개수만 — 무관한 문서를 단서처럼 보이지 않게.
    tenant_note = pk.knowledge_note(many, "tenant")
    assert "25건" in tenant_note and "0.pdf" not in tenant_note


def test_rag_search_uses_the_resolved_ids(monkeypatch):
    """executor 가 넘긴 knowledge_doc_ids 로 search_documents 가 memento 를 부른다(빈 범위면 거절)."""
    from core.rag import tools as rag_tools

    seen = {}

    async def _fake_search(*, query, tenant_id, file_ids, top_k, exclude_chunk_ids):
        seen["file_ids"] = file_ids
        return [memento.Chunk(text="HB-C01 메쉬 의자 에어 180,000원", file_name=PRICE["file_name"],
                              chunk_id="c1", file_id=PRICE["id"])]

    monkeypatch.setattr(memento, "search", _fake_search)
    call = {
        "name": "search_documents", "type": "tool_call", "id": "tc1",
        "args": {"query": "메쉬 의자 가격", "state": {"files": {}}},
    }
    config = {"configurable": {"tenant_id": "localhost", "thread_id": "t",
                               "knowledge_doc_ids": [POLICY["id"], PRICE["id"]]}}
    result = asyncio.run(rag_tools.search_documents.ainvoke(call, config=config))
    assert seen["file_ids"] == sorted([POLICY["id"], PRICE["id"]])
    assert "180,000원" in str(result)
