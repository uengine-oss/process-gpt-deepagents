"""검증은 정의 파일뿐 아니라 저장에 쓰는 에이전트·manifest·폼 파일도 읽어 본다.

튜토리얼 VM 실측: 모델이 agents/quote_calc_agent.json 을 쓰면서 끝의 `}` 를 빠뜨렸고(write_file 입력
자체가 그렇게 끝남) 마무리와 프론트 저장이 그 파일을 말없이 건너뛰어, 고른 에이전트 둘 중 하나만 저장됐다.
"""
import json

import pytest

from core.skills import identified, tools


@pytest.fixture(autouse=True)
def _tenant_workspace(tmp_path, monkeypatch):
    # 검증 도구는 경로를 테넌트 작업공간 안으로만 푼다. 컨텍스트 값은 다음 테스트로 새지 않게 되돌린다.
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    tenant = tools._current_tenant_id.set("localhost")
    room = tools._current_conversation_id.set("")
    yield
    tools._current_conversation_id.reset(room)
    tools._current_tenant_id.reset(tenant)

# VM 회차의 실제 내용(끝 `}` 없음)
BROKEN_AGENT = (
    '{\n  "id": "quote_calc_agent",\n  "name": "견적 산출 자동화 에이전트",\n'
    '  "role": "견적 금액 산정 및 견적서 초안 자동 생성 담당자",\n'
    '  "description": "제품, 수량, 할인 정보를 기반으로 자동으로 각 항목별 금액과 할인, 설치비를 산정해 견적서를 작성한다."\n'
)
GOOD_FORM = (
    "<section>\n  <div class='row' name='a' alias='견적 요청 정보' is_multidata_mode='false'>\n"
    "    <div class='col-sm-6'>\n"
    "      <text-field name='company_name' alias='회사명' type='text' disabled='false' readonly='false'></text-field>\n"
    "    </div>\n  </div>\n</section>\n"
)


def _definition():
    return {
        "processDefinitionName": "검증 테스트 프로세스",
        "processDefinitionId": "validation_test",
        "roles": [{"name": "신청자", "endpoint": "requester", "origin": "created"}],
        "elements": [
            {"elementType": "Event", "id": "start", "name": "시작", "type": "StartEvent"},
            {"elementType": "Activity", "id": "submit_request", "name": "신청 접수", "type": "UserActivity",
             "role": "신청자", "outputData": ["신청 결과"]},
            {"elementType": "Event", "id": "end", "name": "종료", "type": "EndEvent"},
            {"elementType": "Sequence", "id": "seq_start", "source": "start", "target": "submit_request"},
            {"elementType": "Sequence", "id": "seq_end", "source": "submit_request", "target": "end"},
        ],
    }


def _work(tmp_path):
    work = tmp_path / "localhost" / ".bpmn" / "room" / "work"
    (work / "agents").mkdir(parents=True)
    (work / "forms").mkdir()
    (work / "process-definition.json").write_text(json.dumps(_definition(), ensure_ascii=False), encoding="utf-8")
    (work / "agents" / "stock_agent.json").write_text(
        json.dumps({"id": "stock_agent", "name": "재고 확인 자동화 에이전트"}, ensure_ascii=False), encoding="utf-8")
    (work / "forms" / "submit_request.form").write_text(GOOD_FORM, encoding="utf-8")
    return work


def test_good_work_files_have_no_defects(tmp_path):
    assert tools._work_file_defects(_work(tmp_path)) == []


def test_agent_file_missing_closing_brace_is_reported(tmp_path):
    work = _work(tmp_path)
    (work / "agents" / "quote_calc_agent.json").write_text(BROKEN_AGENT, encoding="utf-8")

    defects = tools._work_file_defects(work)

    assert [d["type"] for d in defects] == ["agent_file_invalid_json"]
    assert defects[0]["severity"] == "critical"
    assert defects[0]["file"] == "agents/quote_calc_agent.json"
    assert "agents/quote_calc_agent.json" in defects[0]["detail"]


def test_agent_without_name_manifest_and_forms_are_checked(tmp_path):
    work = _work(tmp_path)
    (work / "agents" / "nameless.json").write_text(json.dumps({"id": "nameless"}), encoding="utf-8")
    (work / "agents" / "many.json").write_text(json.dumps(["not an object"]), encoding="utf-8")
    (work / "manifest.json").write_text('{"agents": [', encoding="utf-8")
    (work / "forms" / "cut.form").write_text(GOOD_FORM[: GOOD_FORM.index("</div>")], encoding="utf-8")
    (work / "forms" / "empty.form").write_text("  \n", encoding="utf-8")

    by_file = {d["file"]: d["type"] for d in tools._work_file_defects(work)}

    assert by_file == {
        "agents/many.json": "agent_file_shape",
        "agents/nameless.json": "agent_file_missing_name",
        "manifest.json": "manifest_invalid_json",
        "forms/cut.form": "form_broken",
        "forms/empty.form": "form_empty",
    }


def test_validation_fails_and_leaves_no_receipt_for_broken_agent_file(tmp_path):
    work = _work(tmp_path)
    (work / "agents" / "quote_calc_agent.json").write_text(BROKEN_AGENT, encoding="utf-8")
    tools._current_conversation_id.set("broken-agent-room")
    identified._cache["broken-agent-room"] = None

    result = json.loads(tools.validate_process_definition.invoke({"path": str(work / "process-definition.json")}))

    assert result["passed"] is False
    assert any(d["type"] == "agent_file_invalid_json" for d in result["remaining_defects"])
    assert not (work / ".process-validation.json").exists()


def test_completion_gate_rejects_agent_file_broken_after_validation(tmp_path):
    conversation_id = "conversation-agent-files"
    tools._current_conversation_id.set(conversation_id)
    identified._cache[conversation_id] = None
    room = tmp_path / "localhost" / ".bpmn" / conversation_id
    active = room / tools.canonical_process_dir_name(conversation_id)
    active.mkdir(parents=True)
    definition_file = active / "process-definition.json"
    definition_file.write_text(json.dumps(_definition(), ensure_ascii=False), encoding="utf-8")
    assert json.loads(tools.validate_process_definition.invoke({"path": str(definition_file)}))["passed"] is True

    (active / "agents").mkdir()
    (active / "agents" / "quote_calc_agent.json").write_text(BROKEN_AGENT, encoding="utf-8")
    result = tools.complete_process_generation.invoke({})

    assert result.startswith("ERROR:")
    body = json.loads(result[len("ERROR:"):])
    assert body["validation_errors"][0]["defects"][0]["type"] == "agent_file_invalid_json"
