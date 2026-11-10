"""생성한 프로세스의 에이전트 단계가 앞 단계 폼 값을 받는다 — inputData 를 엔진이 읽는 참조로.

튜토리얼 VM(인스턴스 4b98b714): 정의 「한빛오피스 대량 주문 견적 승인 프로세스」의 견적 산출 단계
inputData 가 ``["견적요청서", "재고확인결과"]`` — 폼 필드 참조가 아니라 데이터 이름이었다. 실행 엔진은
``<폼 id>.<필드>`` 만 읽으므로 하나도 못 읽고 바로 앞(재고 확인) 산출물만 넘겼고, 견적 산출 에이전트는
가격표를 찾고도 「입력 데이터에 제품명, 수량 등의 정보가 없어…」라며 0원 견적을 냈다.
"""
import json

import pytest

from core.rag import process_knowledge as pk
from core.skills import identified, tools


@pytest.fixture(autouse=True)
def _reset_context():
    room = tools._current_conversation_id.set("inputs-room")
    identified._cache["inputs-room"] = None
    kb = pk._generation_knowledge.set(None)
    yield
    tools._current_conversation_id.reset(room)
    pk._generation_knowledge.reset(kb)


def _form(*fields):
    body = "\n".join(
        f"      <text-field name='{key}' alias='{label}' type='text' disabled='false' readonly='false'></text-field>"
        for key, label in fields
    )
    return (
        "<section>\n  <div class='row' name='layout' alias='폼' is_multidata_mode='false'>\n"
        f"    <div class='col-sm-12'>\n{body}\n    </div>\n  </div>\n</section>\n"
    )


REQUEST_FIELDS = [
    ("customer_company", "회사명"), ("contact_person", "고객 담당자"), ("products_quantities", "제품명/수량"),
    ("delivery_location", "설치 지역"), ("desired_date", "희망 납기"), ("notes", "특이사항"),
]
STOCK_FIELDS = [
    ("product_name", "제품명"), ("available_qty", "현재 재고수량"), ("incoming_date", "입고 예정일"),
    ("can_meet_deadline", "납기 충족 가능"), ("remarks", "비고 및 기타"),
]
QUOTE_FIELDS = [
    ("unit_price", "제품별 단가"), ("discount_rate", "할인율(%)"), ("delivery_fee", "설치비"),
    ("total_price", "견적 총액"), ("calculation_notes", "계산·적용 내역"),
]


def _definition(calc_inputs=("견적요청서", "재고확인결과")):
    """VM 정의와 같은 꼴 — 에이전트 두 역할, 반려되면 견적 산출로 되돌아가는 연결."""
    return {
        "processDefinitionName": "한빛오피스 대량 주문 견적 승인 프로세스",
        "processDefinitionId": "bulk_quote",
        "roles": [
            {"name": "영업담당자", "endpoint": "sales_rep", "origin": "created"},
            {"name": "재고확인에이전트", "endpoint": "stock-checker-agent", "origin": "used"},
            {"name": "견적산출에이전트", "endpoint": "quote-calculator-agent", "origin": "used"},
            {"name": "영업팀장", "endpoint": "sales_mgr", "origin": "created"},
        ],
        "elements": [
            {"elementType": "Event", "id": "start_event", "name": "시작", "type": "StartEvent"},
            {"elementType": "Activity", "id": "collect_request", "name": "견적 요청 접수", "type": "UserActivity",
             "role": "영업담당자", "tool": "formHandler:collect_request", "inputData": [], "outputData": ["견적요청서"]},
            {"elementType": "Activity", "id": "check_stock", "name": "재고 확인", "type": "UserActivity",
             "role": "재고확인에이전트", "tool": "formHandler:check_stock", "agent": "stock-checker-agent",
             "agentMode": "draft", "orchestration": "deepagents",
             "inputData": ["견적요청서"], "outputData": ["재고확인결과"]},
            {"elementType": "Activity", "id": "calculate_quote", "name": "견적 산출", "type": "UserActivity",
             "role": "견적산출에이전트", "tool": "formHandler:calculate_quote", "agent": "quote-calculator-agent",
             "agentMode": "draft", "orchestration": "deepagents",
             "inputData": list(calc_inputs), "outputData": ["견적서초안"]},
            {"elementType": "Activity", "id": "sales_mgr_approval", "name": "영업팀장 승인", "type": "UserActivity",
             "role": "영업팀장", "tool": "formHandler:sales_mgr_approval",
             "inputData": ["견적서초안"], "outputData": ["승인결과"]},
            {"elementType": "Gateway", "id": "approval_gateway", "name": "승인 여부", "type": "ExclusiveGateway"},
            {"elementType": "Event", "id": "end_event", "name": "종료", "type": "EndEvent"},
            {"elementType": "Sequence", "id": "s1", "source": "start_event", "target": "collect_request"},
            {"elementType": "Sequence", "id": "s2", "source": "collect_request", "target": "check_stock"},
            {"elementType": "Sequence", "id": "s3", "source": "check_stock", "target": "calculate_quote"},
            {"elementType": "Sequence", "id": "s4", "source": "calculate_quote", "target": "sales_mgr_approval"},
            {"elementType": "Sequence", "id": "s5", "source": "sales_mgr_approval", "target": "approval_gateway"},
            {"elementType": "Sequence", "id": "s6", "source": "approval_gateway", "target": "end_event",
             "condition": "승인"},
            {"elementType": "Sequence", "id": "s7", "source": "approval_gateway", "target": "calculate_quote",
             "condition": "반려"},
        ],
    }


def _work(tmp_path, definition):
    work = tmp_path / "process-bulk"
    (work / "forms").mkdir(parents=True)
    (work / "agents").mkdir()
    (work / "process-definition.json").write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    for aid, fields in (("collect_request", REQUEST_FIELDS), ("check_stock", STOCK_FIELDS),
                        ("calculate_quote", QUOTE_FIELDS),
                        ("sales_mgr_approval", [("rejection_reason", "반려 사유")])):
        (work / "forms" / f"{aid}.form").write_text(_form(*fields), encoding="utf-8")
    for slug, name in (("stock-checker-agent", "재고 확인 에이전트"), ("quote-calculator-agent", "견적 산출 에이전트")):
        (work / "agents" / f"{slug}.json").write_text(
            json.dumps({"id": slug, "name": name, "role": f"{name} 역할"}, ensure_ascii=False), encoding="utf-8")
    (work / "manifest.json").write_text(json.dumps({
        "skills": [],
        "agents": [
            {"id": "stock-checker-agent", "activity_ids": ["check_stock"]},
            {"id": "quote-calculator-agent", "activity_ids": ["calculate_quote"]},
        ],
        "dmn": [],
    }), encoding="utf-8")
    return work


def _activities(tmp_path, definition=None):
    out = tools._complete_one_process(_work(tmp_path, definition or _definition()), persist=True)
    flat = out["processDefinition"]
    return flat["processDefinitionId"], {a["id"]: a for a in flat["activities"]}


def _refs(proc_id, aid, fields):
    return [f"{proc_id}_{aid}_form.{key}" for key, _ in fields]


def test_agent_step_gets_the_request_form_and_the_previous_agent_result(tmp_path):
    proc_id, acts = _activities(tmp_path)
    calc = acts["calculate_quote"]["inputData"]
    # 사람이 낸 요청서의 모든 필드(제품·수량·설치 지역) + 앞 에이전트(재고 확인) 결과
    assert calc == _refs(proc_id, "collect_request", REQUEST_FIELDS) + _refs(proc_id, "check_stock", STOCK_FIELDS)
    assert acts["check_stock"]["inputData"] == _refs(proc_id, "collect_request", REQUEST_FIELDS)
    # 데이터 이름은 하나도 남지 않는다
    assert "견적요청서" not in calc and "재고확인결과" not in calc


def test_references_point_at_saved_form_ids(tmp_path):
    proc_id, acts = _activities(tmp_path)
    for act in acts.values():
        for ref in act["inputData"]:
            form_id, field = ref.split(".", 1)
            assert act["tool"] != f"formHandler:{form_id}", "자기 폼을 참조하지 않는다"
            assert form_id.startswith(f"{proc_id}_") and form_id.endswith("_form") and field


def test_person_step_names_become_refs_of_the_named_form_only(tmp_path):
    proc_id, acts = _activities(tmp_path)
    # 사람 단계는 모델이 고른 것만 — 앞선 폼 전체를 더하지 않는다
    assert acts["sales_mgr_approval"]["inputData"] == _refs(proc_id, "calculate_quote", QUOTE_FIELDS)
    assert acts["collect_request"]["inputData"] == []


def test_authoring_refs_are_moved_to_saved_form_ids(tmp_path):
    definition = _definition(calc_inputs=(
        "collect_request.products_quantities",          # 작성 때 폼 id = activity id
        "check_stock_form.available_qty",               # _form 꼬리
        "bulk_quote_collect_request_form.delivery_location",  # 스키마 예시 꼴 <옛 프로세스 id>_<id>_form
    ))
    proc_id, acts = _activities(tmp_path, definition)
    calc = acts["calculate_quote"]["inputData"]
    assert calc[:3] == [
        f"{proc_id}_collect_request_form.products_quantities",
        f"{proc_id}_check_stock_form.available_qty",
        f"{proc_id}_collect_request_form.delivery_location",
    ]
    # 모델이 고른 참조가 모자라도 에이전트 단계는 앞선 폼 전부를 받는다(중복 없이)
    assert set(calc) == set(_refs(proc_id, "collect_request", REQUEST_FIELDS) + _refs(proc_id, "check_stock", STOCK_FIELDS))
    assert len(calc) == len(set(calc))


def test_name_with_field_suffix_and_unknown_names(tmp_path):
    definition = _definition(calc_inputs=("견적 요청서.products_quantities", "없는자료"))
    proc_id, acts = _activities(tmp_path, definition)
    calc = acts["calculate_quote"]["inputData"]
    assert calc[0] == f"{proc_id}_collect_request_form.products_quantities"
    # 어느 폼인지 모르는 이름은 남긴다 — 엔진이 그 인스턴스의 완료 산출물을 넘긴다
    assert "없는자료" in calc


def test_own_output_name_as_input_is_dropped(tmp_path):
    """첫 단계가 제 산출물 이름(「견적요청서」)을 입력으로 적은 꼴 — 실제 생성 회차(take B)에서 나왔다."""
    definition = _definition()
    first = next(e for e in definition["elements"] if e.get("id") == "collect_request")
    first["inputData"] = ["견적요청서"]
    _, acts = _activities(tmp_path, definition)
    assert acts["collect_request"]["inputData"] == []


def test_rework_loop_does_not_pull_later_forms_into_the_agent_step(tmp_path):
    proc_id, acts = _activities(tmp_path)
    calc = acts["calculate_quote"]["inputData"]
    assert not any(ref.startswith(f"{proc_id}_sales_mgr_approval_form.") for ref in calc)


def test_upstream_follows_source_only_links():
    elements = [
        {"elementType": "Event", "id": "start", "type": "StartEvent"},
        {"elementType": "Activity", "id": "a", "source": "start"},
        {"elementType": "Activity", "id": "b", "source": "a"},
        {"elementType": "Activity", "id": "c"},
        {"elementType": "Sequence", "id": "s", "source": "b", "target": "c"},
    ]
    assert tools._upstream_activities(elements)["c"] == ["a", "b"]
