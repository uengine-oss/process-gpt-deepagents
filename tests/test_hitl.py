from langgraph.types import Interrupt

import pytest

from core.chat.hitl import (
    extract_interrupt_payload,
    find_interrupt_payload,
    hitl_tools_enabled,
    normalize_hitl_payload,
    records_human_answered,
    workitem_resume_answer,
)


def test_normalize_hitl_payload_coalesces_question_alias_and_options():
    payload = normalize_hitl_payload(
        {
            "message": "어느 프로세스를 만들까요?",
            "context": "후보를 골라주세요.",
            "options": [{"label": "구매", "description": "구매 프로세스"}],
            "multi_select": True,
        }
    )

    assert payload == {
        "question": "어느 프로세스를 만들까요?",
        "context": "후보를 골라주세요.",
        "options": [{"label": "구매", "description": "구매 프로세스"}],
        "multi_select": True,
    }


def test_extract_interrupt_payload_normalizes_interrupt_value():
    interrupt = Interrupt(
        value={
            "question": "승인할까요?",
            "context": "초안을 검토해주세요.",
        }
    )

    assert extract_interrupt_payload({"__interrupt__": (interrupt,)}) == {
        "question": "승인할까요?",
        "context": "초안을 검토해주세요.",
    }


def test_find_interrupt_payload_finds_nested_subgraph_interrupt():
    interrupt = Interrupt(
        value={
            "question": "계속 진행할까요?",
            "context": "",
            "options": [{"label": "승인", "description": "계속 진행"}],
        }
    )
    stream_update = {
        "agent:subgraph": {
            "tools": {
                "__interrupt__": (interrupt,),
            }
        }
    }

    assert find_interrupt_payload(stream_update) == {
        "question": "계속 진행할까요?",
        "context": "",
        "options": [{"label": "승인", "description": "계속 진행"}],
        "multi_select": False,
    }


# ---------------------------------------------------------------------------
# 워크아이템 HITL — COMPLETE 모드에서 질문이 결과로 제출되던 결함의 회귀 테스트
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "agent_mode, is_chat, expected",
    [
        # 워크아이템 COMPLETE: 도구가 없으면 질문을 평문 답으로 내 SUBMITTED 된다.
        ("COMPLETE", False, True),
        # 워크아이템 DRAFT: 사람이 초안을 검토하므로 주지 않는다.
        ("DRAFT", False, False),
        # 채팅은 종전대로 준다.
        ("", True, True),
        ("COMPLETE", True, True),
        ("DRAFT", True, False),
        # 모드가 비어 있는 워크아이템은 주지 않는다.
        ("", False, False),
        ("complete", False, True),
    ],
)
def test_hitl_tools_enabled(agent_mode, is_chat, expected):
    assert hitl_tools_enabled(agent_mode=agent_mode, is_chat=is_chat) is expected


def test_workitem_resume_uses_latest_feedback_verbatim_when_interrupt_pending():
    answer, run_state = workitem_resume_answer(
        is_chat=False,
        has_pending_interrupt=True,
        human_answer="",
        latest_feedback="5일까지 허용합니다",
        run_state={},
    )

    assert answer == "5일까지 허용합니다"
    # build_graph_inputs 가 Command(resume=) 경로를 타려면 tool_name 이 맞아야 한다.
    assert run_state["tool_name"] == "request_human_input"
    assert run_state["pending_field"] == "human_response"


def test_workitem_resume_keeps_existing_pending_field():
    _, run_state = workitem_resume_answer(
        is_chat=False,
        has_pending_interrupt=True,
        human_answer="",
        latest_feedback="답",
        run_state={"pending_field": "approval", "last_question": "승인?"},
    )

    assert run_state == {
        "pending_field": "approval",
        "last_question": "승인?",
        "tool_name": "request_human_input",
    }


@pytest.mark.parametrize(
    "is_chat, has_pending_interrupt, human_answer, latest_feedback",
    [
        # 채팅은 프론트가 human_response_answer 를 따로 실어 준다.
        (True, True, "", "피드백"),
        # 멈춘 질문이 없으면 일반 재작업 피드백이다(처음부터 다시 실행).
        (False, False, "", "피드백"),
        # 이미 답이 있으면 덮지 않는다.
        (False, True, "기존 답", "피드백"),
        # 줄 답이 없다.
        (False, True, "", ""),
    ],
)
def test_workitem_resume_leaves_other_cases_untouched(
    is_chat, has_pending_interrupt, human_answer, latest_feedback
):
    given_state = {"pending_field": "x"}
    answer, run_state = workitem_resume_answer(
        is_chat=is_chat,
        has_pending_interrupt=has_pending_interrupt,
        human_answer=human_answer,
        latest_feedback=latest_feedback,
        run_state=given_state,
    )

    assert answer == human_answer
    assert run_state is given_state


def test_human_answered_event_only_in_chat():
    # 워크아이템에서 내면 event_type enum 에 없는 값이라 같은 묶음 이벤트까지 유실된다.
    assert records_human_answered(is_chat=True) is True
    assert records_human_answered(is_chat=False) is False
