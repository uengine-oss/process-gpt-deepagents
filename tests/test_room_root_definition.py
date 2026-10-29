"""정의를 방 루트에 둔 생성도 턴 끝 검증·마무리를 거친다 + source 로만 이은 연결선을 채운다.

픽스처 `fixtures/quote_room` 은 튜토리얼 VM 방 c26c4108…(「한빛오피스 대량 주문 견적 승인」)의 작업 폴더를
대화 기록의 write_file 입력에서 되살린 것이다.
- process-definition.json: 모델이 처음 쓴 그대로. 시작 이벤트에서 나가는 Sequence 가 없고, 첫 단계
  request_quote 에 `"source": "start"` 만 있다(프론트 그림은 이 source 로 화살표를 그려 넣었다).
- agents/quote_calc_agent.json: 끝의 `}` 가 없다.
그 방에서는 정의가 방 루트(.bpmn/<room>/)에 있어 턴 끝 자동 검증·마무리가 정의를 못 보고 건너뛰었고,
모델의 「프로세스를 생성했어요」만 나가 시작 연결선 없는 정의와 에이전트 하나가 빠진 결과가 저장됐다.
"""
import json
import shutil
from pathlib import Path

import pytest

from core.skills import identified, tools

FIXTURE = Path(__file__).parent / "fixtures" / "quote_room"
ROOM = "c26c4108-5ce2-4bab-81fa-6fef34d84101"


async def _no_llm(*_args, **_kwargs):
    return None


@pytest.fixture
def room(tmp_path, monkeypatch):
    # 경고(게이트웨이 없는 분기)만 남아도 검증은 LLM 교정을 부른다 — 테스트에서는 부르지 않는다.
    tools._ensure_skill_scripts_on_path()
    import validate_process

    monkeypatch.setattr(validate_process, "_make_llm_call", lambda: _no_llm)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    tenant = tools._current_tenant_id.set("localhost")
    conv = tools._current_conversation_id.set(ROOM)
    identified._cache[ROOM] = None
    room_dir = tmp_path / "localhost" / ".bpmn" / ROOM
    shutil.copytree(FIXTURE, room_dir)
    yield room_dir
    tools._current_conversation_id.reset(conv)
    tools._current_tenant_id.reset(tenant)


def _sequences(definition):
    if "sequences" in definition:
        return {(s["source"], s["target"]) for s in definition["sequences"]}
    return {(e["source"], e["target"]) for e in definition["elements"] if e.get("elementType") == "Sequence"}


def test_definition_work_dirs_fall_back_to_room_root(tmp_path):
    room_dir = tmp_path / "room"
    room_dir.mkdir()
    assert tools.definition_work_dirs(room_dir, "c") == []
    (room_dir / "process-definition.json").write_text("{}", encoding="utf-8")
    assert tools.definition_work_dirs(room_dir, "c") == [room_dir]
    primary = room_dir / tools.canonical_process_dir_name("c")
    primary.mkdir()
    assert tools.definition_work_dirs(room_dir, "c") == [primary]


def test_missing_start_sequence_is_rebuilt_from_source():
    definition = json.loads((FIXTURE / "process-definition.json").read_text(encoding="utf-8"))
    assert ("start", "request_quote") not in _sequences(definition)

    normalized, defects = tools._static_process_defects(definition)

    assert ("start", "request_quote") in _sequences(normalized)
    assert not [d for d in defects if d["severity"] == "critical"]


def test_stale_source_does_not_add_a_second_incoming_edge():
    # approve_saleslead_post 는 이미 들어오는 연결이 있다 — 고친 뒤 안 바꾼 source 로 옛 길을 되살리지 않는다.
    definition = json.loads((FIXTURE / "process-definition.json").read_text(encoding="utf-8"))
    for element in definition["elements"]:
        if element.get("id") == "seq_gw_to_saleslead2":
            element["source"] = "approve_saleslead"
    normalized, _ = tools._normalize_to_processgpt_schema(definition)
    assert ("approval_gateway", "approve_saleslead_post") not in _sequences(normalized)


def _validate(definition_file):
    return json.loads(tools.validate_process_definition.invoke({"path": str(definition_file)}))


def test_room_root_definition_is_validated_and_completed(room):
    definition_file = room / "process-definition.json"
    assert tools.definition_work_dirs(room, ROOM) == [room]

    # 깨진 에이전트 파일은 검증에서 막힌다(시작 연결선은 채워져 흐름 critical 은 더 없다).
    first = _validate(definition_file)
    assert first["passed"] is False
    criticals = [d for d in first["remaining_defects"] if d["severity"] == "critical"]
    assert [d["type"] for d in criticals] == ["agent_file_invalid_json"]

    agent_file = room / "agents" / "quote_calc_agent.json"
    agent_file.write_text(agent_file.read_text(encoding="utf-8") + "}\n", encoding="utf-8")
    assert _validate(definition_file)["passed"] is True

    # 이 회차의 승인 단계는 게이트웨이 없이 승인/반려로 갈라진다 — 완료 관문은 이 경고도 막는다(기존 규칙).
    held = tools.complete_process_generation.invoke({})
    assert held.startswith("ERROR:")
    blockers = json.loads(held[len("ERROR:"):])["validation_errors"][0]["defects"]
    assert {d["type"] for d in blockers} == {"uncontrolled_split"}

    # 반려 연결을 빼 갈라짐을 없애면 끝까지 간다.
    definition = json.loads(definition_file.read_text(encoding="utf-8"))
    definition["elements"] = [
        e for e in definition["elements"]
        if e.get("id") not in ("seq_saleslead_reject_to_calc", "seq_post_reject_to_calc")
    ]
    definition_file.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    assert _validate(definition_file)["passed"] is True

    completed = json.loads(tools.complete_process_generation.invoke({}))
    assert completed["count"] == 1
    final = json.loads(definition_file.read_text(encoding="utf-8"))
    assert ("start", "request_quote") in _sequences(final)
    activities = {a["id"]: a for a in final["activities"]}
    assert activities["check_stock"]["agent"] == "stock_agent"
    assert activities["calc_quote"]["agent"] == "quote_calc_agent"
    assert activities["check_stock"]["orchestration"] == "deepagents"
    assert activities["calc_quote"]["orchestration"] == "deepagents"
