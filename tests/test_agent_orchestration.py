"""생성한 프로세스의 에이전트 단계는 이 런타임(deepagents)이 집는 실행기 이름을 들고 나간다.

튜토리얼 VM 실측: orchestration 이 비었거나(엔진이 crewai-deep-research 로 채움) 참고 문서의 옛 규칙대로
crewai-action 이면, todolist 행이 생겨도 그 값을 폴링하는 소비자가 없어 에이전트 단계가 시작되지 않았다.
"""
import json
import re
from pathlib import Path

import pytest

from core.skills import identified, tools


@pytest.fixture(autouse=True)
def _reset_room():
    token = tools._current_conversation_id.set("")
    yield
    tools._current_conversation_id.reset(token)


def _definition():
    return {
        "processDefinitionName": "견적 승인",
        "processDefinitionId": "quote",
        "roles": [
            {"name": "영업 담당", "endpoint": "sales_staff"},
            {"name": "재고 확인 에이전트", "endpoint": "stock_agent"},
        ],
        "elements": [
            {"elementType": "Event", "id": "start", "name": "시작", "type": "StartEvent"},
            {"elementType": "Activity", "id": "request_quote", "name": "견적 요청 접수", "type": "UserActivity",
             "role": "영업 담당", "outputData": ["견적요청정보"]},
            {"elementType": "Activity", "id": "check_stock", "name": "재고 확인", "type": "UserActivity",
             "role": "재고 확인 에이전트", "outputData": ["재고확인결과"]},
            {"elementType": "Event", "id": "end", "name": "종료", "type": "EndEvent"},
            {"elementType": "Sequence", "id": "s1", "source": "start", "target": "request_quote"},
            {"elementType": "Sequence", "id": "s2", "source": "request_quote", "target": "check_stock"},
            {"elementType": "Sequence", "id": "s3", "source": "check_stock", "target": "end"},
        ],
    }


def test_runtime_name_matches_the_poller():
    server = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    match = re.search(r'^AGENT_TYPE\s*=\s*"([^"]+)"', server, re.MULTILINE)
    assert match and match.group(1) == tools._AGENT_ORCH


def test_agent_steps_get_this_runtime_and_human_steps_stay_empty():
    elements = [
        {"elementType": "Activity", "id": "a", "agent": "stock_agent", "agentMode": "draft"},
        {"elementType": "Activity", "id": "b", "agent": "x", "agentMode": "draft", "orchestration": "crewai-action"},
        {"elementType": "Activity", "id": "c", "skills": ["calc"], "agentMode": "complete",
         "orchestration": "crewai-deep-research"},
        {"elementType": "Activity", "id": "d", "agentMode": "complete", "orchestration": "none"},
        {"elementType": "Activity", "id": "e", "agent": "x", "orchestration": "openai-deep-research"},
        {"elementType": "Activity", "id": "human", "agentMode": "none"},
        {"elementType": "Gateway", "id": "gw", "agentMode": "complete"},
    ]
    tools._assign_agent_orchestration(elements)
    by_id = {e["id"]: e for e in elements}
    assert by_id["a"]["orchestration"] == "deepagents"
    assert by_id["b"]["orchestration"] == "deepagents"
    assert by_id["c"]["orchestration"] == "deepagents"
    assert by_id["d"]["orchestration"] == "deepagents"
    # 소비자가 있는 다른 실행기를 일부러 고른 단계는 그대로 둔다.
    assert by_id["e"]["orchestration"] == "openai-deep-research"
    assert "orchestration" not in by_id["human"]
    assert "orchestration" not in by_id["gw"]


def test_completion_links_selected_agent_with_deepagents(tmp_path):
    tools._current_conversation_id.set("orch-room")
    identified._cache["orch-room"] = None
    work = tmp_path / "work"
    (work / "agents").mkdir(parents=True)
    (work / "process-definition.json").write_text(json.dumps(_definition(), ensure_ascii=False), encoding="utf-8")
    (work / "agents" / "stock_agent.json").write_text(json.dumps({
        "id": "stock_agent", "name": "재고 확인 자동화 에이전트", "role": "재고 확인 담당",
    }, ensure_ascii=False), encoding="utf-8")
    (work / "manifest.json").write_text(json.dumps({
        "skills": [], "agents": [{"id": "stock_agent", "activity_ids": ["check_stock"]}], "dmn": [],
    }), encoding="utf-8")

    out = tools._complete_one_process(work, persist=False)

    activities = {a["id"]: a for a in out["processDefinition"]["activities"]}
    assert activities["check_stock"]["agent"] == "stock_agent"
    assert activities["check_stock"]["orchestration"] == "deepagents"
    assert activities["request_quote"]["orchestration"] in (None, "")
