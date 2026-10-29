"""생성 결과 카드(process_result)는 이번 대화가 쓴 정의에서 나오는가.

예전에는 도구 입출력에서 처음 보인 계약 모양 JSON 이 카드가 됐다. 그래서 스킬의 스키마
예시(assets/templates/process-definition.schema.json, '휴가 신청 프로세스')를 read_file 한
순간, 또는 다른 프로세스 정의를 조회한 순간 그것이 카드에 실렸고, 정작 이번에 만든 정의는
묻혔다. 결과는 이 대화의 프로세스 폴더(`.bpmn/<room>/process-*/`)에서만 만든다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.skills import tools as skill_tools  # noqa: E402
from executor import _extract_output_contract, _room_process_result  # noqa: E402

TENANT = "t-result"
ROOM = "3f1c0a9b2e7d4c11a0b2c3d4e5f60718"
SKILL_TEMPLATE = Path(__file__).resolve().parents[1] / "system-skills/process-gpt-system/bpmn-process-generation-skill/assets/templates/process-definition.schema.json"

DEFINITION = {
    "processDefinitionName": "Moonlight Coffee proposal",
    "processDefinitionId": "moonlight_coffee_proposal",
    "description": "Design request mail to the customer reply",
    "isHorizontal": True,
    "data": [],
    "roles": [{"name": "Sales", "endpoint": "sales", "resolutionRule": "the requester", "origin": "created"}],
    "elements": [
        {"elementType": "Event", "id": "start_event", "name": "Request received", "role": "Sales", "type": "StartEvent"},
        {"elementType": "Sequence", "id": "seq_1", "source": "start_event", "target": "receive_mail"},
        {"elementType": "Activity", "id": "receive_mail", "name": "Receive request mail", "role": "Sales", "type": "UserActivity",
         "tool": "formHandler:receive_mail", "inputData": [], "outputData": ["request"], "checkpoints": [], "duration": 1},
        {"elementType": "Sequence", "id": "seq_2", "source": "receive_mail", "target": "end_event"},
        {"elementType": "Event", "id": "end_event", "name": "Reply sent", "role": "Sales", "type": "EndEvent"},
    ],
}


@pytest.fixture
def room(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path)
    token = skill_tools._current_tenant_id.set(TENANT)
    yield tmp_path / TENANT / ".bpmn" / skill_tools._conv_slug(ROOM)
    skill_tools._current_tenant_id.reset(token)


def test_스키마_예시는_계약_모양이라_옛_탐지는_잘못_잡았다():
    # 읽기만 한 참고자료도 '출력계약'으로 보였다 — 이것이 카드가 '휴가 신청 프로세스'였던 이유
    if not SKILL_TEMPLATE.is_file():
        pytest.skip("skill template not present")
    found = _extract_output_contract(SKILL_TEMPLATE.read_text(encoding="utf-8"))
    assert found and found["processDefinition"]["processDefinitionName"] == "휴가 신청 프로세스"


def test_결과는_이_대화의_프로세스_폴더에서_만든다(room):
    work = room / skill_tools.canonical_process_dir_name(ROOM)
    work.mkdir(parents=True)
    (work / "process-definition.json").write_text(json.dumps(DEFINITION, ensure_ascii=False), encoding="utf-8")

    out = _room_process_result(ROOM)

    assert out is not None
    assert out["processDefinition"]["processDefinitionName"] == "Moonlight Coffee proposal"
    assert [a["name"] for a in out["processDefinition"]["activities"]] == ["Receive request mail"]


def test_정의를_아직_쓰지_않은_대화는_결과가_없다(room):
    # 컨설팅 단계(초안만 제안)에는 카드가 뜨지 않는다
    room.mkdir(parents=True)
    assert _room_process_result(ROOM) is None
