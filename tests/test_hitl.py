from langgraph.types import Interrupt

from core.chat.hitl import (
    extract_interrupt_payload,
    find_interrupt_payload,
    normalize_hitl_payload,
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
