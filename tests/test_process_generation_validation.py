import hashlib
import json

from core.skills import tools


def _valid_definition():
    return {
        "processDefinitionName": "검증 테스트 프로세스",
        "processDefinitionId": "validation_test",
        "roles": [{"name": "신청자", "endpoint": "requester", "origin": "created"}],
        "elements": [
            {"elementType": "Event", "id": "start", "name": "시작", "type": "StartEvent"},
            {
                "elementType": "Activity",
                "id": "submit_request",
                "name": "신청 접수",
                "type": "UserActivity",
                "role": "신청자",
                "outputData": ["신청 결과"],
            },
            {"elementType": "Event", "id": "end", "name": "종료", "type": "EndEvent"},
            {"elementType": "Sequence", "id": "seq_start", "source": "start", "target": "submit_request"},
            {"elementType": "Sequence", "id": "seq_end", "source": "submit_request", "target": "end"},
        ],
    }


def test_static_validation_rejects_definition_without_sequences():
    definition = _valid_definition()
    definition["elements"] = [
        element for element in definition["elements"] if element["elementType"] != "Sequence"
    ]

    _, defects = tools._static_process_defects(definition)

    assert any(item["type"] in {"unreachable_node", "endevent_unwired"} for item in defects)


def test_flattened_definition_restores_canonical_event_and_gateway_types():
    definition = _valid_definition()
    definition["elements"].insert(2, {
        "elementType": "Gateway",
        "id": "approval_gateway",
        "name": "승인 여부",
        "type": "ExclusiveGateway",
        "role": "신청자",
    })
    # Use a direct flattened fixture because completion persists this shape.
    flattened = {
        "processDefinitionName": definition["processDefinitionName"],
        "processDefinitionId": definition["processDefinitionId"],
        "roles": definition["roles"],
        "events": [
            {"id": "start", "name": "시작", "type": "startEvent"},
            {"id": "end", "name": "종료", "type": "endEvent"},
        ],
        "activities": [{
            "id": "submit_request", "name": "신청 접수", "type": "userTask",
            "role": ["신청자", "검토자"], "outputData": ["신청 결과"],
        }],
        "gateways": [{
            "id": "approval_gateway", "name": "승인 여부", "type": "exclusiveGateway",
            "role": "신청자",
        }],
        "sequences": [
            {"id": "seq_start", "source": "start", "target": "submit_request"},
            {"id": "seq_gateway", "source": "submit_request", "target": "approval_gateway"},
            {"id": "seq_end", "source": "approval_gateway", "target": "end"},
        ],
    }

    normalized, defects = tools._static_process_defects(flattened)

    assert not defects
    types = {element["id"]: element.get("type") for element in normalized["elements"]}
    assert types["start"] == "StartEvent"
    assert types["end"] == "EndEvent"
    assert types["approval_gateway"] == "ExclusiveGateway"
    activity = next(element for element in normalized["elements"] if element.get("id") == "submit_request")
    assert activity["role"] == "신청자/검토자"


def test_runtime_validator_uses_per_visit_form_values_and_rejects_object_condition_data():
    tools._ensure_skill_scripts_on_path()
    from validation.process_validator import ProcessValidator

    inputs = {"review": [{"decision": "반려"}, {"decision": "승인"}]}
    first = ProcessValidator._form_values_for("review", inputs, {"review": "review_form"}, 0)
    second = ProcessValidator._form_values_for("review", inputs, {"review": "review_form"}, 1)
    assert first["decision"] == "반려"
    assert second["decision"] == "승인"
    assert second["review_form"] == {"decision": "승인"}

    valid, reason = ProcessValidator._validate_definition_shape({
        "activities": [{"id": "review"}],
        "events": [{"id": "start", "type": "startEvent"}, {"id": "end", "type": "endEvent"}],
        "gateways": [{"id": "gw", "conditionData": [{"field": "decision"}]}],
        "sequences": [{"id": "s1", "source": "start", "target": "review"}],
    })
    assert valid is False
    assert "conditionData" in reason


def test_process_work_dirs_ignore_stale_hitl_directories(tmp_path):
    room = tmp_path / "room"
    room.mkdir()
    primary = tools.canonical_process_dir_name("conversation-1")
    expected = [room / primary, room / f"{primary}-2"]
    for path in expected + [room / "process-old1234"]:
        path.mkdir()

    assert tools.process_work_dirs(room, "conversation-1") == expected


def test_process_work_dirs_fall_back_to_self_named_directory(tmp_path):
    # 모델이 안내받은 process-<hash> 대신 제 이름으로 폴더를 만들면, 정의 파일이 있는 가장 최근 폴더를 쓴다.
    import os

    room = tmp_path / "room"
    for name, mtime in (("older-draft", 1_000), ("sales-proposal", 2_000)):
        folder = room / name
        folder.mkdir(parents=True)
        definition = folder / "process-definition.json"
        definition.write_text("{}", encoding="utf-8")
        os.utime(definition, (mtime, mtime))
    (room / ".skill-evals" / "x").mkdir(parents=True)
    (room / ".skill-evals" / "x" / "process-definition.json").write_text("{}", encoding="utf-8")
    (room / "notes").mkdir()

    assert tools.process_work_dirs(room, "conversation-1") == [room / "sales-proposal"]

    primary = room / tools.canonical_process_dir_name("conversation-1")
    primary.mkdir()
    assert tools.process_work_dirs(room, "conversation-1") == [primary]


def test_exclusive_gateway_gets_real_form_decision_contract(tmp_path):
    definition = _valid_definition()
    definition["elements"] = [
        definition["elements"][0],
        definition["elements"][1],
        {
            "elementType": "Gateway", "id": "approval_gateway", "name": "승인 여부",
            "type": "ExclusiveGateway", "conditionData": [],
        },
        {"elementType": "Activity", "id": "approved", "name": "승인 처리", "type": "UserActivity"},
        {"elementType": "Activity", "id": "rework", "name": "재작업", "type": "UserActivity"},
        definition["elements"][2],
        {"elementType": "Sequence", "id": "s0", "source": "start", "target": "submit_request"},
        {"elementType": "Sequence", "id": "s1", "source": "submit_request", "target": "approval_gateway"},
        {"elementType": "Sequence", "id": "s2", "source": "approval_gateway", "target": "approved", "condition": "승인"},
        {"elementType": "Sequence", "id": "s3", "source": "approval_gateway", "target": "rework", "condition": "반려"},
        {"elementType": "Sequence", "id": "s4", "source": "approved", "target": "end"},
        {"elementType": "Sequence", "id": "s5", "source": "rework", "target": "end"},
    ]
    form_dir = tmp_path / "forms"
    form_dir.mkdir()
    form = form_dir / "submit_request.form"
    form.write_text(
        "<section><div class='row'><div class='col-sm-12'>"
        "<textarea-field name='memo' alias='검토 의견'></textarea-field>"
        "</div></div></section>", encoding="utf-8",
    )

    assert tools._ensure_gateway_decision_contract(definition, tmp_path) is True
    activity = next(e for e in definition["elements"] if e.get("id") == "submit_request")
    gateway = next(e for e in definition["elements"] if e.get("id") == "approval_gateway")
    assert activity["tool"] == "formHandler:submit_request"
    assert gateway["conditionData"] == ["submit_request.approval_gateway_decision"]
    html = form.read_text(encoding="utf-8")
    assert "name='approval_gateway_decision'" in html
    assert "value='승인'" in html and "value='반려'" in html
    assert tools._ensure_gateway_decision_contract(definition, tmp_path) is False


def test_explicit_markdown_and_html_artifacts_are_materialized(tmp_path):
    definition = _valid_definition()
    request = "운영 가이드 Markdown(.md)와 종결보고서 HTML 템플릿도 생성해줘"

    assert tools._ensure_requested_supplemental_artifacts(definition, tmp_path, request) is True
    markdown = (tmp_path / "operation-guide.md").read_text(encoding="utf-8")
    html = (tmp_path / "closeout-report.html").read_text(encoding="utf-8")
    assert "# 검증 테스트 프로세스 운영 가이드" in markdown
    assert "신청 접수" in markdown
    assert "CAPA 종결보고서" in html
    assert "<table>" in html
    assert tools._ensure_requested_supplemental_artifacts(definition, tmp_path, request) is False


def test_process_definition_tools_preserve_structure_and_write_atomically(tmp_path):
    definition_file = tmp_path / "process-definition.json"
    written = json.loads(tools.write_process_definition.invoke({
        "definition": _valid_definition(),
        "path": str(definition_file),
    }))
    assert written["written"] is True
    before = json.loads(definition_file.read_text(encoding="utf-8"))

    updated = json.loads(tools.update_process_definition.invoke({
        "path": str(definition_file),
        "element_updates": [{
            "id": "submit_request",
            "changes": {
                "skills": ["request-review"],
                "agentMode": "complete",
                "orchestration": "deepagents",
            },
        }],
        "top_level_updates": {"skills": ["request-review"]},
    }))
    assert updated["updated"] is True
    after = json.loads(definition_file.read_text(encoding="utf-8"))
    assert [item["id"] for item in after["elements"]] == [item["id"] for item in before["elements"]]
    activity = next(item for item in after["elements"] if item["id"] == "submit_request")
    assert activity["skills"] == ["request-review"]
    assert activity["orchestration"] == "deepagents"
    assert after["skills"] == ["request-review"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_process_definition_update_rejects_unsafe_change_without_touching_file(tmp_path):
    definition_file = tmp_path / "process-definition.json"
    tools.write_process_definition.invoke({
        "definition": _valid_definition(),
        "path": str(definition_file),
    })
    original = definition_file.read_bytes()

    rejected = json.loads(tools.update_process_definition.invoke({
        "path": str(definition_file),
        "element_updates": [{
            "id": "submit_request",
            "changes": {"id": "corrupted_id"},
        }],
    }))
    assert rejected == {
        "updated": False,
        "error": "immutable_element_identity",
        "element_id": "submit_request",
    }
    assert definition_file.read_bytes() == original


def test_completion_requires_exact_validated_definition_and_ignores_stale_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    conversation_id = "conversation-validated"
    tools._current_conversation_id.set(conversation_id)
    tools._current_tenant_id.set("localhost")
    room = tmp_path / "localhost" / ".bpmn" / conversation_id
    active = room / tools.canonical_process_dir_name(conversation_id)
    stale = room / "process-stale01"
    active.mkdir(parents=True)
    stale.mkdir()
    definition_file = active / "process-definition.json"
    definition_file.write_text(json.dumps(_valid_definition(), ensure_ascii=False), encoding="utf-8")
    (stale / "process-definition.json").write_text(json.dumps({"elements": []}), encoding="utf-8")

    before_validation = tools.complete_process_generation.invoke({})
    assert before_validation.startswith("ERROR:")
    assert "validation_required" in before_validation

    validated = json.loads(tools.validate_process_definition.invoke({"path": str(definition_file)}))
    assert validated["passed"] is True

    completed = json.loads(tools.complete_process_generation.invoke({}))
    assert completed["count"] == 1
    assert completed["processes"][0]["dir"] == active.name
    final_bytes = definition_file.read_bytes()
    receipt = json.loads((active / ".process-validation.json").read_text(encoding="utf-8"))
    assert receipt["definition_sha256"] == hashlib.sha256(final_bytes).hexdigest()
    assert completed["processes"][0]["definitionSha256"] == receipt["definition_sha256"]
    assert json.loads((stale / "process-definition.json").read_text(encoding="utf-8")) == {"elements": []}
