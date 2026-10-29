"""역할을 에이전트만 맡는 단계는 agentMode=complete — 초안을 제출할 사람이 없다.

튜토리얼 VM(인스턴스 4b98b714): 생성한 「한빛오피스 대량 주문 견적 승인 프로세스」의 재고 확인·견적 산출은
역할 endpoint 가 에이전트 하나뿐인데 마무리가 agentMode 를 draft 로 두었다. 실행 엔진은 정의의 agentMode 를
그대로 따라 DRAFT 할 일을 만들고, 화면은 담당자 본인에게만 제출을 연다 — 견적 산출이 초안 확인 대기로 멈췄다.
엔진의 담당자 규칙(completion determine_agent_mode: 에이전트만 → COMPLETE, 사람과 에이전트 → DRAFT)에 맞춘다.
"""
import json

import pytest

from core.rag import process_knowledge as pk
from core.skills import identified, tools


@pytest.fixture(autouse=True)
def _reset_context():
    room = tools._current_conversation_id.set("mode-room")
    identified._cache["mode-room"] = None
    kb = pk._generation_knowledge.set(None)
    yield
    tools._current_conversation_id.reset(room)
    pk._generation_knowledge.reset(kb)


FORM = (
    "<section>\n  <div class='row' name='layout' alias='폼' is_multidata_mode='false'>\n"
    "    <div class='col-sm-12'>\n"
    "      <text-field name='memo' alias='메모' type='text' disabled='false' readonly='false'></text-field>\n"
    "    </div>\n  </div>\n</section>\n"
)


def _activity(aid, role, **extra):
    return {"elementType": "Activity", "id": aid, "name": aid, "type": "UserActivity", "role": role,
            "tool": f"formHandler:{aid}", "outputData": [f"{aid} 결과"], **extra}


def _complete(tmp_path, roles, activities, manifest_agents=()):
    ids = [a["id"] for a in activities]
    elements = [{"elementType": "Event", "id": "start_event", "name": "시작", "type": "StartEvent"}]
    elements += activities
    elements.append({"elementType": "Event", "id": "end_event", "name": "종료", "type": "EndEvent"})
    chain = ["start_event"] + ids + ["end_event"]
    elements += [{"elementType": "Sequence", "id": f"s{i}", "source": src, "target": tgt}
                 for i, (src, tgt) in enumerate(zip(chain, chain[1:]))]
    work = tmp_path / "process-mode"
    (work / "forms").mkdir(parents=True)
    (work / "agents").mkdir()
    (work / "process-definition.json").write_text(json.dumps({
        "processDefinitionName": "에이전트 모드", "processDefinitionId": "mode", "roles": roles, "elements": elements,
    }, ensure_ascii=False), encoding="utf-8")
    for aid in ids:
        (work / "forms" / f"{aid}.form").write_text(FORM, encoding="utf-8")
    for slug in ("stock-checker-agent", "quote-calculator-agent"):
        (work / "agents" / f"{slug}.json").write_text(
            json.dumps({"id": slug, "name": slug, "role": "역할"}, ensure_ascii=False), encoding="utf-8")
    (work / "manifest.json").write_text(json.dumps({
        "skills": [], "agents": [dict(a) for a in manifest_agents], "dmn": [],
    }, ensure_ascii=False), encoding="utf-8")
    out = tools._complete_one_process(work, persist=True)
    return {a["id"]: a for a in out["processDefinition"]["activities"]}


ROLES = [
    {"name": "영업담당자", "endpoint": "sales_rep", "origin": "created"},
    {"name": "재고확인에이전트", "endpoint": "stock-checker-agent", "origin": "used"},
    {"name": "견적산출에이전트", "endpoint": "quote-calculator-agent", "default": "quote-calculator-agent",
     "origin": "used"},
    {"name": "영업팀장", "endpoint": "sales_mgr", "origin": "created"},
]


def test_agent_only_roles_complete_and_person_roles_with_an_agent_stay_draft(tmp_path):
    acts = _complete(tmp_path, ROLES, [
        _activity("collect_request", "영업담당자"),
        _activity("check_stock", "재고확인에이전트", agent="stock-checker-agent", agentMode="draft",
                  orchestration="deepagents"),
        # 모델이 05 의 옛 규칙(에이전트만 배정 → draft)을 따라 draft 를 적어도 complete 로
        _activity("calculate_quote", "견적산출에이전트", agent="quote-calculator-agent", agentMode="draft"),
        # 사람 역할에 붙인 에이전트(초안 도우미)는 사람이 확인하고 제출한다
        _activity("sales_mgr_approval", "영업팀장", agent="quote-calculator-agent"),
    ])
    assert acts["check_stock"]["agentMode"] == "complete"
    assert acts["calculate_quote"]["agentMode"] == "complete"
    assert acts["calculate_quote"]["orchestration"] == "deepagents"
    assert acts["sales_mgr_approval"]["agentMode"] != "complete"
    assert acts["collect_request"]["agentMode"] == "none"


def test_agent_attached_only_through_manifest_also_completes(tmp_path):
    acts = _complete(tmp_path, ROLES, [
        _activity("collect_request", "영업담당자"),
        _activity("check_stock", "재고확인에이전트"),
    ], manifest_agents=[{"id": "stock-checker-agent", "activity_ids": ["check_stock"]}])
    assert acts["check_stock"]["agent"] == "stock-checker-agent"
    assert acts["check_stock"]["agentMode"] == "complete"


def test_role_without_agent_field_on_the_step_gets_the_role_agent(tmp_path):
    acts = _complete(tmp_path, ROLES, [
        _activity("collect_request", "영업담당자"),
        _activity("calculate_quote", "견적산출에이전트"),
    ])
    assert acts["calculate_quote"]["agent"] == "quote-calculator-agent"
    assert acts["calculate_quote"]["agentMode"] == "complete"
    assert acts["calculate_quote"]["orchestration"] == "deepagents"


def test_role_shared_by_a_person_and_an_agent_stays_draft(tmp_path):
    roles = [
        {"name": "영업담당자", "endpoint": "sales_rep"},
        {"name": "재고 담당", "endpoint": ["stock-checker-agent", "stock_clerk"]},
        {"name": "견적 담당", "endpoint": "quote-calculator-agent, quote_clerk@example.com"},
    ]
    acts = _complete(tmp_path, roles, [
        _activity("collect_request", "영업담당자"),
        _activity("check_stock", "재고 담당", agent="stock-checker-agent"),
        _activity("calculate_quote", "견적 담당", agent="quote-calculator-agent"),
    ])
    # 그대로 둔다 — none 이면 엔진이 실제 담당자로 정한다(사람과 에이전트 → DRAFT)
    assert acts["check_stock"]["agentMode"] != "complete"
    assert acts["calculate_quote"]["agentMode"] != "complete"


def test_explicit_human_review_keeps_draft(tmp_path):
    acts = _complete(tmp_path, ROLES, [
        _activity("collect_request", "영업담당자"),
        _activity("calculate_quote", "견적산출에이전트", agent="quote-calculator-agent", humanReview=True),
    ])
    assert acts["calculate_quote"]["agentMode"] == "draft"


def test_unknown_role_holder_is_not_treated_as_an_agent(tmp_path):
    roles = [{"name": "영업담당자", "endpoint": "sales_rep"}, {"name": "재고확인", "endpoint": "role_stock"}]
    acts = _complete(tmp_path, roles, [
        _activity("collect_request", "영업담당자"),
        _activity("check_stock", "재고확인", agent="stock-checker-agent"),
    ])
    assert acts["check_stock"]["agentMode"] != "complete"


def test_agent_only_step_also_receives_the_earlier_forms(tmp_path):
    """complete 로 바뀐 단계도 에이전트 단계라 앞선 폼 필드를 받는다(두 고침이 함께 동작)."""
    acts = _complete(tmp_path, ROLES, [
        _activity("collect_request", "영업담당자"),
        _activity("calculate_quote", "견적산출에이전트"),
    ])
    assert [ref.split(".", 1)[1] for ref in acts["calculate_quote"]["inputData"]] == ["memo"]
    assert acts["calculate_quote"]["inputData"][0].endswith("_collect_request_form.memo")
