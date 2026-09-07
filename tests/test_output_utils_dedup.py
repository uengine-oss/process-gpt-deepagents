"""extract_final_text가 langchain/deepagents 에이전트 루프의 '중복 최종 턴'
(도구 호출 없는 응답 뒤에도 종료로 판단하지 못하고 한 턴 더 돌며 이미 낸 답을
다시 요약해 내는 현상)을 흡수해 마지막 블록만 남기는지 검증한다.
"""
from __future__ import annotations

from core.utils.output_utils import extract_final_text


def _state_with_content(content):
    return {"messages": [{"role": "user", "content": "재고를 점검해줘"}, {"role": "assistant", "content": content}]}


def test_collapses_near_duplicate_restated_final_turn():
    first = (
        "1) 요약\n\n실제 Odoo 조회 기준으로 점검한 결과, 현재 최소 재고 미만 품목은 없습니다.\n"
        "- 재주문 규칙(orderpoint) 대상 2개 품목 모두 현재고가 최소재고 이상입니다.\n"
        "- 따라서 즉시 추가 발주가 필요한 부족 품목은 0건입니다.\n"
        "- 실습용 키보드는 기존 발주 및 입고 예정이 있어 중복 발주 방지 관리가 필요합니다.\n"
        "원하시면 이어서 위험도 산식이나 보고 문안 형태로도 정리해드리겠습니다."
    )
    second = (
        "1) 요약\n\n실제 Odoo 조회 기준으로 점검한 결과, 현재 최소 재고 미만 품목은 없습니다.\n"
        "- 재주문 규칙(orderpoint) 대상 2개 품목 모두 현재고가 최소재고 이상입니다.\n"
        "- 따라서 즉시 추가 발주가 필요한 부족 품목은 0건입니다.\n"
        "- 실습용 키보드는 기존 발주가 있어 중복 발주 위험이 높습니다.\n"
        "원하시면 이어서 발주 필요 품목 자동 분류표 형태로도 정리해드릴게요."
    )
    content = [{"type": "text", "text": first}, {"type": "text", "text": second}]
    result = extract_final_text(_state_with_content(content))
    assert result == second
    assert "1) 요약" in result
    assert result.count("1) 요약") == 1


def test_keeps_all_blocks_when_content_genuinely_differs():
    intro = "네, 확인해보겠습니다. 잠시만 기다려 주세요." * 3  # 충분히 긴 도입부지만 결론과는 무관
    conclusion = (
        "점검 결과 실습용 무선 마우스와 실습용 키보드 모두 최소 재고 이상이며, "
        "키보드는 기존 발주 P00007의 미수령 잔량 40건이 있어 추가 발주는 필요하지 않습니다. "
        "입고 완료 여부만 추적 관리하시면 됩니다."
    )
    content = [{"type": "text", "text": intro}, {"type": "text", "text": conclusion}]
    result = extract_final_text(_state_with_content(content))
    assert intro in result
    assert conclusion in result


def test_single_text_block_unchanged():
    content = [{"type": "text", "text": "부족 품목이 없습니다."}]
    assert extract_final_text(_state_with_content(content)) == "부족 품목이 없습니다."


def test_plain_string_content_unchanged():
    assert extract_final_text(_state_with_content("부족 품목이 없습니다.")) == "부족 품목이 없습니다."


def test_short_trailing_block_not_treated_as_restatement():
    long_answer = "상세 점검 결과입니다. " * 20
    short_ack = "확인했습니다."
    content = [{"type": "text", "text": long_answer}, {"type": "text", "text": short_ack}]
    result = extract_final_text(_state_with_content(content))
    assert long_answer in result
    assert short_ack in result
