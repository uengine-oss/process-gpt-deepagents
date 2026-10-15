"""채팅에서 '/스킬명' 으로 지정한 스킬은 첫 모델 호출부터 그 지시가 대화에 실려 있어야 한다.

배경: 예전에는 지정한 스킬로 스킬 목록만 좁혔다. 모델은 목록을 보고 `read_file` 로
SKILL.md 를 읽는 호출을 한 번 더 한 뒤에야 일을 시작했다. 게다가 스킬 목록은 thread 당
한 번만 스캔돼 체크포인트에 남으므로, 대화 도중의 좁히기는 먹지 않거나(이미 전체 목록이
있음) 거꾸로 다음 턴까지 그 스킬만 보이게 남았다.

지금은 deepagents 의 `pinned_skills` 로 SKILL.md 본문을 첫 모델 호출 전에 대화에 붙인다
(`build_graph_inputs(..., pinned_skills=[...])`). 실제 `create_deep_agent` 그래프를 가짜
모델로 돌려, 모델이 받은 요청과 호출 횟수로 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from core.agents.input_builder import build_graph_inputs, pinned_skill_outcome
from core.skills.skills import FilteredSkillsMiddleware, ToolBindingSkillsMiddleware, add_skill_reload_inputs

MARKER = "MEETING-PREP-STEP-7f3a"
SKILL_PATH = "/skills/meeting-prep/SKILL.md"


def _write_skill(root: Path, name: str, marker: str) -> None:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 스킬\n---\n\n# {name}\n답변 첫 줄에 {marker} 를 적는다.\n",
        encoding="utf-8",
    )


class _SkillFollowingModel(BaseChatModel):
    """실제 모델처럼 행동한다: 스킬 지시가 대화에 있으면 바로 답하고, 없으면 SKILL.md 를 읽는다."""

    requests: list = []

    @property
    def _llm_type(self) -> str:
        return "skill-following"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.requests.append(list(messages))
        if any(MARKER in str(m.content) for m in messages if not isinstance(m, AIMessage)):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=f"{MARKER}\n회의 준비안"))])
        if any(isinstance(m, ToolMessage) for m in messages):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="일반 답변"))])
        msg = AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"file_path": SKILL_PATH, "limit": 1000}, "id": f"r{len(self.requests)}"},
        ])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _agent(root: Path, saver, *, allowed: list[str] | None = None):
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    backend = FilesystemBackend(root_dir=root, virtual_mode=True)
    model = _SkillFollowingModel(requests=[])
    kwargs: dict = dict(model=model, backend=backend, checkpointer=saver, system_prompt="너는 업무 비서다.")
    # core/agents/agent.py 와 같은 조립 — 기본 SkillsMiddleware 자리를 서비스 미들웨어가 대신한다.
    if allowed is None:
        kwargs["middleware"] = [ToolBindingSkillsMiddleware(backend=backend, sources=["/skills/"])]
    else:
        # 액티비티·프로필로 목록을 좁힌 경우.
        kwargs["middleware"] = [FilteredSkillsMiddleware(backend=backend, sources=["/skills/"], allowed_skills=allowed)]
    return create_deep_agent(**kwargs), model


def _inputs(text: str, *, pinned: list[str] | None, has_checkpoint: bool = False):
    inputs, _ = build_graph_inputs(
        text, human_answer_stripped="", run_state={}, checkpointer=object(), checkpoint_thread_id="t",
        recent_messages=[], has_checkpoint=has_checkpoint, has_pending_interrupt=False, pinned_skills=pinned,
    )
    return inputs


async def _with_reload(inputs: dict, graph, saver, config, root: Path) -> dict:
    """executor 처럼 이 thread 의 체크포인트를 보고 스킬 재로드 입력을 더한다."""
    tup = await saver.aget_tuple(config)
    values = dict(tup.checkpoint.get("channel_values") or {}) if tup else {}
    inputs = dict(inputs)
    await add_skill_reload_inputs(inputs, graph, config, values, [str(root / "skills")], ["/skills"])
    return inputs


def _read_file_calls(state) -> list:
    return [c for m in state["messages"] if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] == "read_file"]


async def test_pinned_skill_is_in_the_first_model_request(tmp_path):
    _write_skill(tmp_path, "meeting-prep", MARKER)
    graph, model = _agent(tmp_path, InMemorySaver())

    state = await graph.ainvoke(_inputs("내일 회의 준비해줘", pinned=["meeting-prep"]),
                                config={"configurable": {"thread_id": "t1"}})

    first = model.requests[0]
    assert any(MARKER in str(m.content) for m in first if isinstance(m, HumanMessage)), "첫 요청에 스킬 지시가 없다"
    assert len(model.requests) == 1, "스킬을 찾아 읽는 모델 호출이 생겼다"
    assert _read_file_calls(state) == []
    assert state["messages"][-1].content.startswith(MARKER)
    pinned = [m for m in state["messages"] if m.additional_kwargs.get("lc_source") == "pinned_skill"]
    assert len(pinned) == 1
    assert "description:" not in pinned[0].content, "frontmatter 는 빼고 본문만 붙여야 한다"


async def test_without_pin_the_model_spends_a_call_reading_the_skill(tmp_path):
    """대조군 — 고정하지 않으면 SKILL.md 를 읽는 호출이 하나 더 든다(위 테스트가 무엇을 줄였는지)."""
    _write_skill(tmp_path, "meeting-prep", MARKER)
    graph, model = _agent(tmp_path, InMemorySaver())

    state = await graph.ainvoke(_inputs("내일 회의 준비해줘", pinned=None), config={"configurable": {"thread_id": "t2"}})

    assert len(model.requests) == 2
    assert len(_read_file_calls(state)) == 1


async def test_skill_added_mid_conversation_is_still_pinned(tmp_path):
    """스킬 목록은 첫 턴에 스캔돼 남는다. 그 뒤 생긴 스킬은 스킬 재로드로 목록에 들어와 고정된다."""
    _write_skill(tmp_path, "other", "OTHER-0000")
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t3"}}
    graph, _ = _agent(tmp_path, saver)
    await graph.ainvoke(await _with_reload(_inputs("안녕", pinned=None), graph, saver, config, tmp_path), config=config)

    _write_skill(tmp_path, "meeting-prep", MARKER)
    graph, model = _agent(tmp_path, saver)
    inputs = _inputs("내일 회의 준비해줘", pinned=["meeting-prep"], has_checkpoint=True)
    state = await graph.ainvoke(await _with_reload(inputs, graph, saver, config, tmp_path), config=config)

    assert len(model.requests) == 1
    assert state["messages"][-1].content.startswith(MARKER)


async def test_pin_works_when_skill_list_is_filtered(tmp_path):
    """목록을 좁힌 경우에도, 고정할 스킬이 목록에 있으면 고정된다(build_agent 가 목록에 더한다)."""
    _write_skill(tmp_path, "other", "OTHER-0000")
    _write_skill(tmp_path, "meeting-prep", MARKER)
    graph, model = _agent(tmp_path, InMemorySaver(), allowed=["other", "meeting-prep"])

    state = await graph.ainvoke(_inputs("내일 회의 준비해줘", pinned=["meeting-prep"]),
                                config={"configurable": {"thread_id": "t4"}})

    assert len(model.requests) == 1
    assert state["messages"][-1].content.startswith(MARKER)


@pytest.mark.parametrize("has_checkpoint", [False, True])
def test_graph_inputs_carry_pins_without_forcing_a_rescan(has_checkpoint):
    """고정만으로 다시 스캔하지 않는다 — 대화 도중 추가한 스킬은 스킬 재로드가 목록에 넣는다
    (tests/test_skill_reload.py::test_skill_added_mid_conversation_can_be_pinned)."""
    inputs = _inputs("x", pinned=["meeting-prep"], has_checkpoint=has_checkpoint)
    assert inputs["pinned_skills"] == ["meeting-prep"]
    assert "skills_metadata" not in inputs, "스킬이 바뀌지 않았는데 고정하는 턴마다 다시 스캔하면 안 된다"


def test_graph_inputs_without_pins_leave_skill_state_alone():
    inputs = _inputs("x", pinned=None)
    assert "pinned_skills" not in inputs
    assert "skills_metadata" not in inputs, "고정하지 않는 턴까지 매번 다시 스캔하면 안 된다"


def _pin_msg(name: str) -> HumanMessage:
    return HumanMessage(content=f'<skill name="{name}">…</skill>',
                        additional_kwargs={"lc_source": "pinned_skill", "skill": {"name": name}})


def test_outcome_looks_only_at_this_turn_and_flags_a_reread():
    """두 번째 턴: 지난 턴의 고정은 세지 않고, 고정했는데 SKILL.md 를 또 읽으면 reread 로 잡는다."""
    state = {"messages": [
        HumanMessage(content="/minwon-classifier ..."), _pin_msg("minwon-classifier"), AIMessage(content="{}"),
        HumanMessage(content="/contract-renewal-reviewer ..."), _pin_msg("contract-renewal-reviewer"),
        AIMessage(content="", tool_calls=[{"name": "read_file", "id": "r1",
                                           "args": {"file_path": "/skills/t/local/contract-renewal-reviewer/SKILL.md"}}]),
        ToolMessage(content="...", tool_call_id="r1"), AIMessage(content="권고: 재협상"),
    ]}

    outcome = pinned_skill_outcome(state, ["contract-renewal-reviewer"])

    assert outcome["pinned"] == ["contract-renewal-reviewer"]
    assert outcome["missing"] == []
    assert outcome["reread"] == ["contract-renewal-reviewer"]


def test_outcome_reports_a_pin_that_never_landed():
    state = {"messages": [HumanMessage(content="/ghost ..."), AIMessage(content="답")]}
    assert pinned_skill_outcome(state, ["ghost"])["missing"] == ["ghost"]


def test_hitl_resume_does_not_carry_pins():
    inputs, resume = build_graph_inputs(
        "x", human_answer_stripped="승인", run_state={"tool_name": "request_human_input"}, checkpointer=object(),
        checkpoint_thread_id="t", has_pending_interrupt=True, pinned_skills=["meeting-prep"],
    )
    assert resume and isinstance(inputs, Command)
