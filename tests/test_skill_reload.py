"""대화 도중 스킬을 추가·수정·삭제하면 같은 대화(thread)의 다음 턴에 반영된다(Skill Reloading).

deepagents 의 SkillsMiddleware 는 스킬 목록을 thread 마다 한 번 읽어 `skills_metadata` 에 두고,
체크포인터가 있으면 이후 턴에도 그대로 쓴다. 다시 읽게 하는 공식 방법은 `skills_metadata` 를
`None` 으로 두는 것이다(deepagents>=0.7.16, docs: Reload skills). 서비스(executor)는 스킬
파일이 바뀐 턴에만 그 값을 실행 입력에 넣는다 — 바뀌지 않은 턴에는 다시 스캔하지 않는다.

체크포인터 없는 실행은 매번 새로 읽어 재로드가 된 것처럼 보이므로, 여기서는 서비스와 같은
조건을 쓴다: executor 와 같은 순서로 체크포인트를 보고, 턴마다 `build_agent` 로 에이전트를
새로 조립해 `skill_reload_inputs` 를 실행 입력에 더한 뒤, 체크포인터 하나와 같은 thread_id 로 돌린다. 샌드박스는 파일시스템 백엔드, 모델은 대본대로 움직이며 매 요청의
시스템 프롬프트와 도구 이름을 기록하는 가짜 모델로만 바꾼다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from deepagents.middleware import skills as ds_skills
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import core.agents.agent as agent_mod
from core.agents.input_builder import build_graph_inputs
from core.skills import tool_catalog
from core.skills.skills import skill_reload_inputs

THREAD = {"configurable": {"thread_id": "room-1"}}


@tool
def count_words(text: str) -> str:
    """문장의 단어 수를 센다."""
    return f"WORDS={len(text.split())}"


class _ScriptedModel(BaseChatModel):
    """대본대로 도구를 부르고, 매 요청의 시스템 프롬프트·도구 이름·메시지를 기록한다."""

    script: list = []
    tool_names: list = []
    prompts: list = []
    bound: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound = [getattr(t, "name", None) or t.get("name") or t["function"]["name"] for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.tool_names.append(list(self.bound))
        self.prompts.append(
            "\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        )
        step = len(self.tool_names)
        calls = self.script[step - 1] if step <= len(self.script) else []
        if not calls:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="끝"))])
        tool_calls = [{"name": n, "args": a, "id": f"c{step}-{i}"} for i, (n, a) in enumerate(calls)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))])


def _skill_dir(root: Path, name: str) -> Path:
    return root / "skills" / "coll" / name


def _write_skill(root: Path, name: str, description: str, *, include_tools: str = "", body: str = "지시대로 한다.") -> None:
    d = _skill_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    meta = f"metadata:\n  include_tools: {include_tools}\n" if include_tools else ""
    path = d / "SKILL.md"
    existed = path.exists()
    old = path.stat().st_mtime_ns if existed else 0
    path.write_text(f"---\nname: {name}\ndescription: {description}\n{meta}---\n\n# {name}\n{body}\n", encoding="utf-8")
    if existed and path.stat().st_mtime_ns == old:
        # 파일 시스템 시각 해상도가 낮아도 '바뀐 파일'로 보이게 한다(실제 저장은 시각이 바뀐다).
        os.utime(path, ns=(old + 1_000_000, old + 1_000_000))


def _delete_skill(root: Path, name: str) -> None:
    d = _skill_dir(root, name)
    for p in sorted(d.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    d.rmdir()


SKILL_ONLY: set[str] = set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """build_agent 의 바깥 의존(샌드박스·모델·스킬 위치·git 설정·관리자 지정 저장소)만 바꿔 끼운다."""
    from deepagents.backends.filesystem import FilesystemBackend

    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    model = _ScriptedModel(script=[], tool_names=[], prompts=[])

    def _no_git(_tenant_id):
        raise RuntimeError("no git provider in tests")

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda *a, **k: backend)
    monkeypatch.setattr(agent_mod, "_build_model", lambda: model)
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_mod, "_get_skills", lambda *a, **k: [str(tmp_path / "skills" / "coll")])
    monkeypatch.setattr(agent_mod, "_get_git_provider", _no_git)
    SKILL_ONLY.clear()
    monkeypatch.setattr(tool_catalog, "load_skill_only_tools", lambda *_a, **_k: frozenset(SKILL_ONLY))

    # SkillsMiddleware 가 스킬 소스를 실제로 스캔한 횟수.
    scans: list[str] = []
    real_alist = ds_skills._alist_skills_with_errors

    async def _counting_alist(backend_, source_path):
        scans.append(source_path)
        return await real_alist(backend_, source_path)

    monkeypatch.setattr(ds_skills, "_alist_skills_with_errors", _counting_alist)
    (tmp_path / "skills" / "coll").mkdir(parents=True)
    return tmp_path, model, scans, InMemorySaver()


async def _turn(env, text, script, *, pinned=None, activity_skills=None):
    """서비스의 한 턴: 에이전트를 새로 조립하고 같은 체크포인터·thread 로 실행한다."""
    _, model, _, checkpointer = env
    model.script = script
    model.tool_names, model.prompts = [], []
    tup = await checkpointer.aget_tuple(THREAD)
    checkpoint_values = dict(tup.checkpoint.get("channel_values") or {}) if tup else {}
    inputs, _ = build_graph_inputs(
        text, human_answer_stripped="", run_state={}, checkpointer=checkpointer, checkpoint_thread_id="room-1",
        recent_messages=[], has_checkpoint=tup is not None, has_pending_interrupt=False, pinned_skills=pinned,
    )
    skill_sources = await agent_mod.resolve_skill_sources("t-skill-reload")
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t-skill-reload",
        extra_tools=[count_words],
        checkpointer=checkpointer,
        is_chat=True,
        pinned_skills=pinned,
        activity_skills=activity_skills,
        skill_sources=skill_sources,
    )
    inputs.update(skill_reload_inputs(checkpoint_values, *skill_sources))
    return await graph.ainvoke(inputs, config=THREAD)


def _tool_result(state, name: str) -> ToolMessage:
    ids = {c["id"] for m in state["messages"] if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] == name}
    return [m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id in ids][-1]


async def test_skill_added_mid_conversation_is_listed_on_the_next_turn(env):
    root = env[0]
    _write_skill(root, "alpha", "알파 업무 절차")
    await _turn(env, "안녕", [[]])
    assert "beta" not in env[1].prompts[0]

    _write_skill(root, "beta", "베타 보고서 작성 절차")
    await _turn(env, "베타 보고서 써줘", [[]])

    prompt = env[1].prompts[0]
    assert "베타 보고서 작성 절차" in prompt, "대화 도중 추가한 스킬이 같은 대화의 다음 턴 목록에 없다"
    assert "알파 업무 절차" in prompt


async def test_skill_edited_mid_conversation_shows_new_description_on_the_next_turn(env):
    root = env[0]
    _write_skill(root, "alpha", "예전 설명")
    await _turn(env, "안녕", [[]])

    _write_skill(root, "alpha", "바뀐 설명")
    await _turn(env, "다시", [[]])

    prompt = env[1].prompts[0]
    assert "바뀐 설명" in prompt, "대화 도중 수정한 스킬 설명이 같은 대화에 반영되지 않았다"
    assert "예전 설명" not in prompt


async def test_skill_deleted_mid_conversation_disappears_on_the_next_turn(env):
    root = env[0]
    _write_skill(root, "alpha", "알파 업무 절차")
    _write_skill(root, "gamma", "감마 정산 절차")
    await _turn(env, "안녕", [[]])
    assert "감마 정산 절차" in env[1].prompts[0]

    _delete_skill(root, "gamma")
    await _turn(env, "다시", [[]])

    prompt = env[1].prompts[0]
    assert "감마 정산 절차" not in prompt, "삭제한 스킬이 같은 대화의 목록에 남아 있다"
    assert "알파 업무 절차" in prompt


async def test_unchanged_skills_are_not_rescanned_on_later_turns(env):
    root = env[0]
    _write_skill(root, "alpha", "알파 업무 절차")
    await _turn(env, "안녕", [[]])
    first_turn_scans = len(env[2])
    assert first_turn_scans >= 1, "첫 턴에 스킬을 읽지 않았다"

    await _turn(env, "두 번째", [[]])
    await _turn(env, "세 번째", [[]])
    assert len(env[2]) == first_turn_scans, "스킬이 바뀌지 않았는데 다시 스캔했다"
    assert "알파 업무 절차" in env[1].prompts[0]

    _write_skill(root, "alpha", "알파 업무 절차 v2")
    await _turn(env, "네 번째", [[]])
    assert len(env[2]) > first_turn_scans, "스킬이 바뀌었는데 다시 스캔하지 않았다"


async def test_skill_added_mid_conversation_opens_its_skill_only_tool_after_read(env):
    root = env[0]
    SKILL_ONLY.add("count_words")
    _write_skill(root, "alpha", "알파 업무 절차")
    await _turn(env, "안녕", [[]])
    assert "count_words" not in env[1].tool_names[0]

    _write_skill(root, "word-counter", "단어 수 세기 절차", include_tools="count_words")
    state = await _turn(env, "단어 세어줘", [
        [("read_file", {"file_path": "/skills/coll/word-counter/SKILL.md"})],
        [("count_words", {"text": "하나 둘 셋"})],
    ])

    before, after = env[1].tool_names[0], env[1].tool_names[1]
    assert "count_words" not in before, "새 스킬을 읽기 전에 스킬 전용 도구가 보인다"
    assert "count_words" in after, "대화 도중 추가한 스킬을 읽었는데 스킬 전용 도구가 열리지 않았다"
    assert _tool_result(state, "count_words").content == "WORDS=3"


async def test_include_tools_added_to_existing_skill_mid_conversation_opens_the_tool(env):
    root = env[0]
    SKILL_ONLY.add("count_words")
    _write_skill(root, "word-counter", "단어 수 세기 절차")
    state = await _turn(env, "단어 세어줘", [
        [("read_file", {"file_path": "/skills/coll/word-counter/SKILL.md"})],
        [("count_words", {"text": "하나"})],
    ])
    assert "count_words" not in env[1].tool_names[1], "include_tools 가 없는데 도구가 열렸다"
    assert _tool_result(state, "count_words").status == "error"

    _write_skill(root, "word-counter", "단어 수 세기 절차", include_tools="count_words")
    state = await _turn(env, "다시 세어줘", [
        [("read_file", {"file_path": "/skills/coll/word-counter/SKILL.md"})],
        [("count_words", {"text": "하나 둘"})],
    ])
    assert "count_words" in env[1].tool_names[1], "include_tools 를 바꾼 스킬을 읽었는데 도구가 열리지 않았다"
    assert _tool_result(state, "count_words").content == "WORDS=2"


async def test_skill_deleted_mid_conversation_no_longer_opens_its_tool(env):
    root = env[0]
    SKILL_ONLY.add("count_words")
    _write_skill(root, "word-counter", "단어 수 세기 절차", include_tools="count_words")
    await _turn(env, "단어 세어줘", [
        [("read_file", {"file_path": "/skills/coll/word-counter/SKILL.md"})],
        [("count_words", {"text": "하나"})],
    ])
    assert "count_words" in env[1].tool_names[1]

    _delete_skill(root, "word-counter")
    state = await _turn(env, "또 세어줘", [[("count_words", {"text": "하나 둘"})]])
    assert "count_words" not in env[1].tool_names[0], "삭제한 스킬이 열어 둔 도구가 계속 보인다"
    assert _tool_result(state, "count_words").status == "error"


async def test_skill_added_mid_conversation_can_be_pinned(env):
    root = env[0]
    SKILL_ONLY.add("count_words")
    _write_skill(root, "alpha", "알파 업무 절차")
    await _turn(env, "안녕", [[]])

    _write_skill(root, "word-counter", "단어 수 세기 절차", include_tools="count_words")
    state = await _turn(env, "/word-counter 하나 둘 셋", [[("count_words", {"text": "하나 둘 셋"})]], pinned=["word-counter"])

    pins = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("lc_source") == "pinned_skill"
    ]
    assert pins and pins[-1].additional_kwargs["skill"]["name"] == "word-counter", "대화 도중 추가한 스킬이 고정되지 않았다"
    assert "count_words" in env[1].tool_names[0]
    assert _tool_result(state, "count_words").content == "WORDS=3"


async def test_activity_filtered_list_reloads_too(env):
    root = env[0]
    _write_skill(root, "alpha", "알파 업무 절차")
    await _turn(env, "안녕", [[]], activity_skills=["alpha"])

    _write_skill(root, "alpha", "알파 업무 절차 개정")
    _write_skill(root, "beta", "베타 보고서 작성 절차")
    await _turn(env, "다시", [[]], activity_skills=["alpha"])

    prompt = env[1].prompts[0]
    assert "알파 업무 절차 개정" in prompt, "액티비티로 좁힌 목록에서 수정이 반영되지 않았다"
    assert "베타 보고서 작성 절차" not in prompt, "액티비티 밖 스킬이 재로드로 들어왔다"


def test_executor_is_wired_to_skill_reload_inputs():
    """executor 전체를 띄우려면 DB·MCP 가 필요하다. 재로드 판단은 위에서 실제 그래프로 검증했고,
    여기서는 실행이 그 판단을 실제로 거치는지만 지킨다 — 에이전트와 같은 스킬 소스로, 체크포인트
    값을 보고, 그래프 실행 전에 입력에 더한다."""
    import inspect

    import executor

    src = inspect.getsource(executor.DeepAgentExecutor.execute)
    assert "skill_sources = await resolve_skill_sources(tenant_id)" in src
    assert "skill_sources=skill_sources" in src
    assert "inputs.update(skill_reload_inputs(checkpoint_values, *skill_sources))" in src
    assert src.index("skill_reload_inputs(") < src.index("graph.astream(")
