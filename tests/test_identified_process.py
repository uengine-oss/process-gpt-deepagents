"""식별 화면에서 전환한 프로세스 — 도출 구조는 그대로, 보강 필드만 남는다."""
import json

from core.skills import identified, tools


def _mined():
    """process-miner 가 내는 flattened 정의: 작성 → 검토 → (수정 필요 → 수정 → 검토 | 완료) → 발송."""
    act = lambda i, name, role, **kw: {  # noqa: E731
        "id": i, "name": name, "role": role, "type": "userTask", "tool": "", "agent": kw.get("agent", ""),
        "agentMode": kw.get("mode", ""), "orchestration": kw.get("orch", ""), "skills": [], "inputData": [],
        "outputData": [f"{name} 결과"], "properties": "{}", "description": "",
    }
    gw = lambda i, name, role: {  # noqa: E731
        "id": i, "name": name, "role": "", "type": "exclusiveGateway", "conditionData": [],
        "properties": json.dumps({"minedRole": role}),
    }
    seq = lambda i, s, t, c="": {"id": i, "source": s, "target": t, "condition": c, "name": "", "properties": "{}"}  # noqa: E731
    return {
        "processDefinitionId": "mined_quote",
        "processDefinitionName": "견적 처리 프로세스",
        "roles": [
            {"name": "홍길동", "endpoint": "u-hong", "resolutionRule": "핸드오버 로그에서 도출된 역할"},
            {"name": "문서 생성 전문가", "endpoint": "a-doc", "resolutionRule": "핸드오버 로그에서 도출된 역할"},
            {"name": "이팀장", "endpoint": "u-lee", "resolutionRule": "핸드오버 로그에서 도출된 역할"},
        ],
        "events": [
            {"id": "start_event", "name": "시작", "type": "startEvent", "properties": "{}"},
            {"id": "end_event", "name": "종료", "type": "endEvent", "properties": "{}"},
        ],
        "activities": [
            act("activity_01", "견적서 작성", "홍길동"),
            act("activity_02", "견적서 초안 작성", "문서 생성 전문가", agent="a-doc", mode="COMPLETE", orch="deepagents"),
            act("activity_03", "견적서 검토", "이팀장"),
            act("activity_04", "견적서 수정", "홍길동"),
        ],
        "gateways": [gw("gateway_exclusive_01", "반복 진입", "loop_entry"), gw("gateway_exclusive_02", "반복 판정", "loop_exit")],
        "sequences": [
            seq("sequence_01", "start_event", "activity_01"),
            seq("sequence_02", "activity_01", "activity_02"),
            seq("sequence_03", "activity_02", "gateway_exclusive_01"),
            seq("sequence_04", "gateway_exclusive_01", "activity_03"),
            seq("sequence_05", "activity_03", "gateway_exclusive_02"),
            seq("sequence_06", "gateway_exclusive_02", "activity_04", "견적서 수정 필요"),
            seq("sequence_07", "activity_04", "gateway_exclusive_01"),
            seq("sequence_08", "gateway_exclusive_02", "end_event", "완료"),
        ],
    }


def _node(pd, nid):
    return next(e for e in pd["elements"] if e.get("id") == nid)


def _edges(pd):
    return {(e["source"], e["target"], e.get("condition") or "") for e in pd["elements"] if e["elementType"] == "Sequence"}


def test_seeded_definition_is_valid_generation_input():
    pd = identified.to_elements(_mined())
    _, defects = tools._static_process_defects(pd)
    assert [d for d in defects if d.get("severity") == "critical"] == []


def test_supplements_are_kept_and_structure_is_restored():
    original = _mined()
    edited = identified.to_elements(original)
    # 에이전트가 보강한 것
    _node(edited, "activity_03")["tool"] = "formHandler:activity_03"
    _node(edited, "gateway_exclusive_02")["conditionData"] = ["activity_03.gateway_exclusive_02_decision"]
    # 에이전트가 흐름을 바꾼 것 — 담당자 변경, 단계 추가, 분기값 변경, 연결 삭제
    _node(edited, "activity_03")["role"] = "팀장"
    edited["elements"].append({"elementType": "Activity", "id": "notify", "name": "결과 통보", "type": "UserActivity",
                               "role": "홍길동", "outputData": ["통보"]})
    for e in edited["elements"]:
        if e.get("id") == "sequence_06":
            e["condition"] = "반려"
    edited["elements"] = [e for e in edited["elements"] if e.get("id") != "sequence_02"]
    edited["roles"] = [{"name": "팀장", "endpoint": "role_team_lead"}]

    merged, notes = identified.keep_identified_structure(original, edited)

    assert _edges(merged) == _edges(identified.to_elements(original))
    assert _node(merged, "activity_03")["role"] == "이팀장"
    assert _node(merged, "activity_03")["tool"] == "formHandler:activity_03"
    assert _node(merged, "gateway_exclusive_02")["conditionData"] == ["activity_03.gateway_exclusive_02_decision"]
    assert not any(e.get("id") == "notify" for e in merged["elements"])
    assert [r["endpoint"] for r in merged["roles"]] == ["u-hong", "a-doc", "u-lee"]
    joined = " ".join(notes)
    assert "결과 통보" in joined and "이팀장" in joined and "반려" in joined and "견적서 초안 작성" in joined


def test_skill_on_a_person_step_only_drafts_and_observed_agent_stays():
    original = _mined()
    edited = identified.to_elements(original)
    _node(edited, "activity_01").update({"skills": ["견적서 작성 가이드"], "agentMode": "complete", "orchestration": "deepagents"})
    _node(edited, "activity_02").update({"agent": "other-agent", "agentMode": "draft"})

    merged, _ = identified.keep_identified_structure(original, edited)

    assert _node(merged, "activity_01")["skills"] == ["견적서 작성 가이드"]
    assert _node(merged, "activity_01")["agentMode"] == "draft"  # 사람(홍길동)이 확인하고 제출
    assert _node(merged, "activity_02")["agent"] == "a-doc"
    assert str(_node(merged, "activity_02")["agentMode"]).lower() == "complete"


def test_reconverting_yields_the_same_flow():
    """같은 도출 정의를 두 번 전환하면 에이전트가 무엇을 바꾸든 흐름은 같다."""
    original = _mined()
    a = identified.to_elements(original)
    b = identified.to_elements(original)
    _node(b, "activity_04")["name"] = "견적서 보완"
    b["elements"] = [e for e in b["elements"] if e.get("id") != "sequence_07"]
    ma, _ = identified.keep_identified_structure(original, a)
    mb, _ = identified.keep_identified_structure(original, b)
    shape = lambda pd: (  # noqa: E731
        sorted((e["id"], e.get("name"), e.get("role")) for e in pd["elements"] if e["elementType"] != "Sequence"),
        sorted(_edges(pd)),
    )
    assert shape(ma) == shape(mb)


def test_validation_restores_structure_without_llm_repair(tmp_path, monkeypatch):
    original = _mined()
    monkeypatch.setattr(identified, "load_identified_definition", lambda room: original if room == "room-1" else None)
    monkeypatch.setattr(tools, "_bpmn_work_dir", lambda: tmp_path)
    token = tools._current_conversation_id.set("room-1")
    try:
        target = tmp_path / "process-definition.json"
        edited = identified.to_elements(original)
        _node(edited, "activity_03")["name"] = "검토 및 승인"
        target.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

        result = json.loads(tools.validate_process_definition.invoke({}))

        saved = json.loads(target.read_text(encoding="utf-8"))
        assert _node(saved, "activity_03")["name"] == "견적서 검토"
        assert result["repaired"] is False
        assert any("견적서 검토" in n for n in identified.read_notes(tmp_path))
    finally:
        tools._current_conversation_id.reset(token)


def test_decision_field_lists_options_in_items_attribute(tmp_path):
    """ProcessGPT select-field 는 items 속성만 읽는다. <option> 자식으로 쓰면 드롭다운이 비어
    실행 때 갈래를 고를 수 없었다(갈래 형태 e2e 에서 발견)."""
    pd = identified.to_elements(_mined())
    forms = tmp_path / "forms"
    forms.mkdir()
    (forms / "activity_03.form").write_text(
        "<section><div class='row'><div class='col-sm-12'>"
        "<text-field name='r' alias='결과'></text-field></div></div></section>",
        encoding="utf-8",
    )
    tools._ensure_gateway_decision_contract(pd, tmp_path)
    html = (forms / "activity_03.form").read_text(encoding="utf-8")
    assert "<option" not in html
    import html as _h
    import re as _re
    items = _re.search(r"items='([^']*)'", html).group(1)
    assert json.loads(_h.unescape(items)) == [{"견적서 수정 필요": "견적서 수정 필요"}, {"완료": "완료"}]
    assert "is_dynamic_load='fixed'" in html
