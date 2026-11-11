"""업무를 에이전트에 맡길 때의 팀 구성과 결과 요약.

지키려는 것
- 요약(_summary)은 폼 값이 아니다: 결과에서 떼어 내고, 없으면 결과를 건드리지 않는다.
- 팀 후보는 서브에이전트로 돌릴 수 있는 프로필(스킬 · MCP 묶음)만 — a2a · pgagent · 설명 없는 프로필은 뺀다.
- 고른 결과는 후보 안에서만, 많아야 3개. 필요 없으면 아무도 고르지 않는다(혼자 처리).
- 끄면(DEEPAGENTS_AUTO_TEAM=0) 목록 조회도 하지 않는다.
"""
import asyncio
import json

import pytest

from core.agents import team as team_mod
from core.agents.input_builder import build_user_message
from core.agents.team import (
    TeamChoice,
    build_team_prompt,
    choose_team,
    parse_team_choice,
    team_candidates,
    team_card_text,
    team_message_hint,
)
from core.chat.result_summary import split_result_summary


def test_summary_is_split_from_form_result():
    raw = json.dumps({"po_form": {"decision": "reject"}, "_summary": "부족 수량이 0개라  발주하지 않습니다."}, ensure_ascii=False)
    rest, summary = split_result_summary(raw)
    assert summary == "부족 수량이 0개라 발주하지 않습니다."
    assert json.loads(rest) == {"po_form": {"decision": "reject"}}


def test_result_without_summary_is_untouched():
    raw = '```json\n{"po_form": {"decision": "approve"}}\n```'
    assert split_result_summary(raw) == (raw, "")
    assert split_result_summary("그냥 글입니다") == ("그냥 글입니다", "")


def test_summary_that_is_not_text_is_dropped():
    rest, summary = split_result_summary('{"f": {"a": 1}, "_summary": {"x": 1}}')
    assert summary == "" and json.loads(rest) == {"f": {"a": 1}}


def test_form_instruction_asks_for_summary_next_to_form_key():
    msg = build_user_message("견적을 검토하세요", form_id="po_form", form_fields=[{"key": "decision", "text": "결정"}])
    assert '"_summary"' in msg
    assert '"po_form"' in msg


def _agent(i, **kw):
    row = {"id": f"a{i}", "is_agent": True, "agent_type": "agent", "username": f"에이전트{i}", "description": "설명"}
    row.update(kw)
    return row


def test_candidates_are_runnable_profiles_only():
    rows = [
        _agent(1),
        _agent(2, agent_type="a2a"),
        _agent(3, agent_type="pgagent"),
        _agent(4, description="", role="", goal="", skills="", tools=""),
        _agent(5, is_agent=False),
        _agent(6, agent_type=None, description="", skills="quote-check"),
        {"is_agent": True, "agent_type": "agent", "username": "아이디 없음", "description": "x"},
    ]
    assert [a["id"] for a in team_candidates(rows)] == ["a1", "a6"]


def test_prompt_lists_candidates_and_says_pick_only_when_needed():
    prompt = build_team_prompt(
        activity_name="발주 승인",
        instruction="재보충 판단 결과를 보고 승인하세요",
        form_fields=[{"key": "approval_decision", "text": "구매 승인/반려"}],
        candidates=[_agent(1, username="철강 품질 전문가", skills="steel-grade", tools="erp-mcp")],
    )
    assert "id=a1" in prompt and "철강 품질 전문가" in prompt and "steel-grade" in prompt and "erp-mcp" in prompt
    assert "꼭 필요한 것만" in prompt and "구매 승인/반려" in prompt


def test_parse_keeps_known_ids_dedupes_and_caps():
    cands = [_agent(i) for i in range(1, 6)]
    picked, reason = parse_team_choice(
        '답: {"agent_ids": ["a2", "zz", "a2", "a3", "a4", "a5"], "reason": "품질 기준이  필요"}', cands
    )
    assert [a["id"] for a in picked] == ["a2", "a3", "a4"]
    assert reason == "품질 기준이 필요"


def test_parse_empty_or_broken_means_alone():
    cands = [_agent(1)]
    assert parse_team_choice('{"agent_ids": [], "reason": "혼자 충분"}', cands) == ([], "혼자 충분")
    assert parse_team_choice("모르겠습니다", cands) == ([], "")


class _FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        return type("R", (), {"content": self.answer})()


def test_choose_team_picks_from_directory(monkeypatch):
    async def directory(tenant_id):
        return [_agent(1, username="철강 품질 전문가"), _agent(2, agent_type="a2a")]

    monkeypatch.setattr(team_mod, "load_agent_directory", directory)
    model = _FakeModel('{"agent_ids": ["a1"], "reason": "강재 등급 확인이 필요합니다"}')
    choice = asyncio.run(choose_team(tenant_id="t", activity_name="발주 승인", instruction="x", model=model))
    assert [a["id"] for a in choice.agents] == ["a1"] and choice.candidates == 1
    assert "a2" not in model.prompts[0]
    assert "철강 품질 전문가" in team_card_text(choice)
    assert "철강-품질-전문가" in team_message_hint(choice) and "직접 처리" in team_message_hint(choice)


def test_choose_team_without_candidates_does_not_call_model(monkeypatch):
    async def directory(tenant_id):
        return [_agent(1, agent_type="a2a")]

    monkeypatch.setattr(team_mod, "load_agent_directory", directory)
    model = _FakeModel("{}")
    choice = asyncio.run(choose_team(tenant_id="t", activity_name="x", instruction="x", model=model))
    assert choice == TeamChoice() and model.prompts == []


def test_choose_team_can_be_turned_off(monkeypatch):
    called = []

    async def directory(tenant_id):
        called.append(tenant_id)
        return [_agent(1)]

    monkeypatch.setattr(team_mod, "load_agent_directory", directory)
    monkeypatch.setenv("DEEPAGENTS_AUTO_TEAM", "0")
    choice = asyncio.run(choose_team(tenant_id="t", activity_name="x", instruction="x", model=_FakeModel("{}")))
    assert choice == TeamChoice() and called == []


def test_choose_team_failure_falls_back_to_alone(monkeypatch):
    async def directory(tenant_id):
        return [_agent(1)]

    class Boom:
        async def ainvoke(self, prompt):
            raise RuntimeError("llm down")

    monkeypatch.setattr(team_mod, "load_agent_directory", directory)
    choice = asyncio.run(choose_team(tenant_id="t", activity_name="x", instruction="x", model=Boom()))
    assert choice.agents == [] and choice.candidates == 1
    assert "혼자 처리" in team_card_text(choice)
    assert team_message_hint(choice) == ""


@pytest.mark.parametrize("value", ["0", "false", "off", "No"])
def test_off_values(monkeypatch, value):
    monkeypatch.setenv("DEEPAGENTS_AUTO_TEAM", value)
    assert team_mod.auto_team_enabled() is False


# ── 이전 단계 입력 안내: 파일로 옮긴 항목이 있을 때만 read_file 을 안내한다 ──────────────

def _offload(monkeypatch, tmp_path, data):
    import executor

    monkeypatch.setattr(executor, "_instance_ws_dir", lambda instance_id, subdir: tmp_path / subdir)
    text = "제안서를 작성하세요\n[InputData]\n" + json.dumps(data, ensure_ascii=False)
    return executor._offload_input_data(text, "inst-1")


def test_small_inputs_are_inline_and_not_called_files(monkeypatch, tmp_path):
    out = _offload(monkeypatch, tmp_path, {"req_form": {"customer": "한빛물산", "detail": "회의실 가구"}})
    assert "read_file 로 읽지 말고" in out
    assert "파일로 저장했습니다" not in out
    assert '- customer: "한빛물산"' in out
    assert not (tmp_path / "inputs").exists() or not any((tmp_path / "inputs").iterdir())


def test_large_inputs_point_to_files_and_say_so(monkeypatch, tmp_path):
    out = _offload(monkeypatch, tmp_path, {"req_form": {"customer": "한빛물산", "body": "가" * 400}})
    assert "「→ 경로」로 표시된 항목만 read_file" in out
    assert "→ /workspace/inst-1/inputs/req_form_body.md" in out
    assert (tmp_path / "inputs" / "req_form_body.md").read_text(encoding="utf-8") == "가" * 400
