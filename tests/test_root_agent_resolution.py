"""Tests for 딥 에이전트 루트 승격(root agent promotion) 규칙.

프로세스 설계 화면의 '루트 에이전트' 설정(activity.rootAgent)과, 설정이 없을 때의
기존 자동 휴리스틱이 서로를 덮어쓰지 않고 공존하는지를 고정한다.
"""

from core.chat.activity import (
    ROOT_AGENT_GENERIC,
    _extract_activity_root_agent,
    resolve_root_agent,
)


def _agent(agent_id: str) -> dict:
    return {"id": agent_id, "username": agent_id, "role": f"{agent_id} role"}


# --------------------------------------------------------------------------
# 자동(미설정) 규칙 — 기존 동작이 그대로 유지되어야 한다
# --------------------------------------------------------------------------

def test_auto_promotes_the_only_assigned_agent():
    profile, agents = resolve_root_agent(
        agents=[_agent("a")], agent_profile=None, explicit_root_agent_id=None
    )
    assert profile == _agent("a")
    assert agents == []


def test_auto_keeps_generic_root_when_multiple_agents_assigned():
    profile, agents = resolve_root_agent(
        agents=[_agent("a"), _agent("b")], agent_profile=None, explicit_root_agent_id=None
    )
    assert profile is None
    assert len(agents) == 2


def test_auto_drops_chat_agent_profile_in_group_chat():
    # 그룹 채팅: 그 턴의 응답자 하나만 루트가 되어버리지 않도록 프로필을 버린다.
    profile, _ = resolve_root_agent(
        agents=[_agent("a"), _agent("b")],
        agent_profile=_agent("a"),
        explicit_root_agent_id=None,
    )
    assert profile is None


def test_auto_keeps_chat_agent_profile_in_one_on_one():
    profile, _ = resolve_root_agent(
        agents=[_agent("a")], agent_profile=_agent("a"), explicit_root_agent_id=None
    )
    assert profile == _agent("a")


# --------------------------------------------------------------------------
# 명시 설정 — 설계자가 고른 값이 자동 규칙을 이긴다
# --------------------------------------------------------------------------

def test_pinned_agent_is_promoted_even_with_multiple_agents():
    agents_in = [_agent("a"), _agent("b"), _agent("c")]
    profile, agents = resolve_root_agent(
        agents=agents_in, agent_profile=None, explicit_root_agent_id="b"
    )
    assert profile == _agent("b")
    # 나머지는 서브에이전트 후보로 남는다(루트 제외는 호출부 책임).
    assert agents == agents_in


def test_generic_selection_blocks_single_agent_promotion():
    profile, agents = resolve_root_agent(
        agents=[_agent("a")], agent_profile=None, explicit_root_agent_id=ROOT_AGENT_GENERIC
    )
    assert profile is None
    assert agents == [_agent("a")]


def test_generic_selection_blocks_chat_profile_promotion():
    profile, _ = resolve_root_agent(
        agents=[_agent("a")],
        agent_profile=_agent("a"),
        explicit_root_agent_id=ROOT_AGENT_GENERIC,
    )
    assert profile is None


def test_stale_pin_falls_back_to_auto_rule():
    # 배정에서 빠진 에이전트를 가리키는 낡은 설정은 무시하고 자동 규칙을 탄다.
    profile, agents = resolve_root_agent(
        agents=[_agent("a")], agent_profile=None, explicit_root_agent_id="gone"
    )
    assert profile == _agent("a")
    assert agents == []


def test_stale_pin_with_multiple_agents_falls_back_to_generic():
    profile, _ = resolve_root_agent(
        agents=[_agent("a"), _agent("b")], agent_profile=None, explicit_root_agent_id="gone"
    )
    assert profile is None


def test_pin_matching_chat_agent_profile_outside_agents_list():
    # 채팅 경로에서는 응답 대상이 agents 목록이 아닌 agent_profile 로만 들어올 수 있다.
    profile, _ = resolve_root_agent(
        agents=[], agent_profile=_agent("a"), explicit_root_agent_id="a"
    )
    assert profile == _agent("a")


# --------------------------------------------------------------------------
# 프로세스 정의에서 설정값 읽기
# --------------------------------------------------------------------------

def test_extract_root_agent_from_definition():
    definition = {"activities": [{"id": "act1", "rootAgent": "agent-b"}]}
    assert _extract_activity_root_agent(definition, activity_id="act1") == "agent-b"


def test_extract_root_agent_accepts_snake_case_alias():
    definition = {"activities": [{"activity_id": "act1", "root_agent_id": "agent-b"}]}
    assert _extract_activity_root_agent(definition, activity_id="act1") == "agent-b"


def test_extract_root_agent_returns_none_when_unset_or_blank():
    assert _extract_activity_root_agent({"activities": [{"id": "act1"}]}, activity_id="act1") is None
    assert (
        _extract_activity_root_agent(
            {"activities": [{"id": "act1", "rootAgent": "  "}]}, activity_id="act1"
        )
        is None
    )
    assert _extract_activity_root_agent({"activities": []}, activity_id="act1") is None
    assert _extract_activity_root_agent(None, activity_id="act1") is None
