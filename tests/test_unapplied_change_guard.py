"""고치기 차례에 「고쳤다」고만 하고 정의·산출물이 넘긴 때와 같으면 결과 전달을 보류한다.

튜토리얼 VM 실측(방 c26c4108…): 첫 고치기 차례의 edit_file 은 notify_customer 의 `source` 만 바꿨고
(흐름 그대로), 다음 차례는 도구 호출 없이 「이미 규정대로 프로세스 흐름을 수정했습니다」라고 답했다.
"""
import json
import shutil
from pathlib import Path

import pytest

from core.skills import definition_guard, identified, tools

FIXTURE = Path(__file__).parent / "fixtures" / "quote_room"
ROOM = "c26c4108-5ce2-4bab-81fa-6fef34d84101"
FIX_REQUEST = (
    "규정 제5조와 다른 곳이 있어. 견적 총액이 3,000만 원 이상이면 영업팀장 승인 뒤에 재무 담당 승인을 받고 "
    "나서 고객 회신으로 가야 해. 이 순서로 흐름을 고쳐 줘."
)
CLAIM = (
    "이미 규정대로 프로세스 흐름을 수정했습니다. 이제 아래처럼 동작합니다: - 견적 총액이 3,000만 원 미만이면 "
    "영업팀장 승인 후 바로 고객 회신 … 변경된 프로세스를 확인 후 저장 버튼만 눌러주시면 됩니다."
)


async def _no_llm(*_args, **_kwargs):
    return None


@pytest.fixture
def delivered_room(tmp_path, monkeypatch):
    """VM 방의 작업 폴더를 결과까지 넘긴 상태로 만든다(반려 연결을 빼 게이트웨이 없는 갈라짐 경고를 없앰)."""
    tools._ensure_skill_scripts_on_path()
    import validate_process

    monkeypatch.setattr(validate_process, "_make_llm_call", lambda: _no_llm)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    tenant = tools._current_tenant_id.set("localhost")
    conv = tools._current_conversation_id.set(ROOM)
    identified._cache[ROOM] = None
    room = tmp_path / "localhost" / ".bpmn" / ROOM
    shutil.copytree(FIXTURE, room)
    agent = room / "agents" / "quote_calc_agent.json"
    agent.write_text(agent.read_text(encoding="utf-8") + "}\n", encoding="utf-8")
    definition_file = room / "process-definition.json"
    definition = json.loads(definition_file.read_text(encoding="utf-8"))
    definition["elements"] = [
        e for e in definition["elements"]
        if e.get("id") not in ("seq_saleslead_reject_to_calc", "seq_post_reject_to_calc")
    ]
    definition_file.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")
    assert json.loads(tools.validate_process_definition.invoke({"path": str(definition_file)}))["passed"] is True
    assert json.loads(tools.complete_process_generation.invoke({}))["count"] == 1
    assert definition_guard.delivered_signature(room)
    yield room
    tools._current_conversation_id.reset(conv)
    tools._current_tenant_id.reset(tenant)


def _notice(room, *, request=FIX_REQUEST, reply=CLAIM, tools_used=()):
    return definition_guard.unapplied_change_notice(
        room, ROOM, request=request, reply=reply, tools_used=set(tools_used),
    )


def _flat(room):
    return json.loads((room / "process-definition.json").read_text(encoding="utf-8"))


def _write_flat(room, flat, indent=2):
    (room / "process-definition.json").write_text(json.dumps(flat, ensure_ascii=False, indent=indent), encoding="utf-8")


def test_claim_without_any_tool_call_is_held(delivered_room):
    notice = _notice(delivered_room, request="네, 그렇게 고쳐 주세요.")
    assert notice and "결과 전달을 보류" in notice


def test_reformatting_or_editing_only_source_fields_is_not_a_change(delivered_room):
    flat = _flat(delivered_room)
    flat["activities"].reverse()
    for activity in flat["activities"]:
        if activity["id"] == "notify_customer":
            activity["source"] = "approve_saleslead_post"  # VM 첫 고치기 차례의 edit_file 이 바꾼 것
    _write_flat(delivered_room, flat, indent=None)
    assert _notice(delivered_room, tools_used={"read_file", "edit_file"})


def test_real_flow_change_passes(delivered_room):
    flat = _flat(delivered_room)
    for sequence in flat["sequences"]:
        if sequence["source"] == "approval_gateway" and sequence["target"] == "approve_saleslead_post":
            sequence["target"] = "approve_saleslead"
    _write_flat(delivered_room, flat)
    assert _notice(delivered_room, tools_used={"read_file", "write_file"}) is None


def test_renaming_a_step_passes(delivered_room):
    flat = _flat(delivered_room)
    flat["activities"][0]["name"] += " (수정)"
    _write_flat(delivered_room, flat)
    assert _notice(delivered_room) is None


def test_form_change_passes(delivered_room):
    form = next((delivered_room / "forms").glob("*.form"))
    form.write_text(form.read_text(encoding="utf-8").replace("</section>", "<!-- x --></section>"), encoding="utf-8")
    assert _notice(delivered_room) is None


@pytest.mark.parametrize("request_text", ["이 프로세스는 어떻게 동작해?", "견적 산출은 누가 해?"])
def test_question_turn_is_left_alone(delivered_room, request_text):
    assert _notice(delivered_room, request=request_text, reply="재무 담당 승인이 추가되어 있습니다.") is None


def test_reply_that_claims_nothing_is_left_alone(delivered_room):
    assert _notice(delivered_room, reply="이미 그 순서로 되어 있어요. 3,000만 원 이상은 영업팀장 승인 뒤 재무 담당으로 갑니다.") is None


def test_turn_with_non_file_tools_is_left_alone(delivered_room):
    assert _notice(delivered_room, tools_used={"read_file", "update_agent"}) is None


def test_system_blocks_do_not_count_as_a_change_request(delivered_room):
    request = "이 프로세스 설명해 줘\n[ExecutionPolicy]\n- 필요하면 정의를 수정하고 고쳐도 됩니다."
    assert _notice(delivered_room, request=request) is None


def test_no_delivered_result_means_no_guard(delivered_room):
    (delivered_room / definition_guard.DELIVERED_MARK).unlink()
    assert _notice(delivered_room) is None


def test_english_reply_gets_english_notice(delivered_room):
    notice = _notice(delivered_room, request="Please fix the flow.", reply="I've updated the process flow as requested.")
    assert notice and notice.startswith("I said it was fixed")


def test_delivered_mark_is_not_a_workspace_artifact():
    # 확장자가 없어 작업 폴더 재전송(미리보기 확장자 목록)과 문서 수집에 걸리지 않는다.
    assert Path(definition_guard.DELIVERED_MARK).suffix == ""
