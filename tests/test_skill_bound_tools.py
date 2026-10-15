"""관리자가 스킬 전용으로 지정한 도구는 그 도구를 `include_tools` 에 적은 스킬을 읽은 뒤에만 보이고 호출된다.

deepagents 의 Add tools to skills(>=0.7.22) 공식 구조를 따른다 — 숨길 도구는 도구 쪽(테넌트 관리자
지정)에서 정하고 SkillsMiddleware 로 넘긴다. `include_tools` 는 그 도구를 열어 줄 스킬만 정한다.
스킬 전용이 아닌 도구는 에이전트 tools 에 남아 `include_tools` 에 적혀도 처음부터 보인다
(openspec: bind-skill-tools). 소스 코드 도구든 테넌트 MCP 서버 도구든 같은 규칙이다.

실제 `build_agent` 조립을 쓴다 — 샌드박스는 파일시스템 백엔드, 모델은 대본대로 움직이며 매
요청에 바인딩된 도구 이름을 기록하는 가짜 모델로만 바꾼다. MCP 도구는 테스트용 stdio MCP
서버를 테넌트 설정으로 띄워 executor 와 같은 `load_chat_root_tools` 로 불러온다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import core.agents.agent as agent_mod
from core.skills import tool_catalog
from core.agents.input_builder import build_graph_inputs
from core.llm.mcp import load_chat_root_tools

SERVER = str(Path(__file__).parent / "fixtures" / "skill_bound_mcp_server.py")
TENANT_MCP = {
    "mcpServers": {
        "orders": {"command": sys.executable, "args": [SERVER, "orders"]},
        "other": {"command": sys.executable, "args": [SERVER, "other"]},
    }
}
ORDER_SKILL = "/skills/coll/order-lookup/SKILL.md"
NOTE_SKILL = "/skills/coll/word-counter/SKILL.md"


@tool
def count_words(text: str) -> str:
    """문장의 단어 수를 센다."""
    return f"WORDS={len(text.split())}"


@tool
def unrelated_source_tool(text: str) -> str:
    """어느 스킬에도 적히지 않은 소스 도구."""
    return text


class _ScriptedModel(BaseChatModel):
    """대본대로 도구를 부르고, 매 요청에 바인딩된 도구 이름을 기록한다."""

    script: list = []
    tool_names: list = []
    bound: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound = [getattr(t, "name", None) or t.get("name") or t["function"]["name"] for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.tool_names.append(list(self.bound))
        step = len(self.tool_names)
        calls = self.script[step - 1] if step <= len(self.script) else []
        if not calls:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="끝"))])
        tool_calls = [{"name": n, "args": a, "id": f"c{step}-{i}"} for i, (n, a) in enumerate(calls)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))])


def _write_skill(root: Path, name: str, include_tools: str) -> None:
    d = root / "skills" / "coll" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = f"metadata:\n  include_tools: {include_tools}\n" if include_tools else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 스킬\n{meta}---\n\n# {name}\n지시대로 도구를 쓴다.\n",
        encoding="utf-8",
    )


SKILL_ONLY: set[str] = set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """build_agent 의 바깥 의존(샌드박스·모델·스킬 위치·git 설정·관리자 지정 저장소)만 바꿔 끼운다."""
    from deepagents.backends.filesystem import FilesystemBackend

    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    model = _ScriptedModel(script=[], tool_names=[])

    def _no_git(_tenant_id):
        raise RuntimeError("no git provider in tests")

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda *a, **k: backend)
    monkeypatch.setattr(agent_mod, "_build_model", lambda: model)
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_mod, "_get_skills", lambda *a, **k: [str(tmp_path / "skills" / "coll")])
    monkeypatch.setattr(agent_mod, "_get_git_provider", _no_git)
    SKILL_ONLY.clear()
    monkeypatch.setattr(tool_catalog, "load_skill_only_tools", lambda *_a, **_k: frozenset(SKILL_ONLY))
    return tmp_path, model


@pytest.fixture(scope="module")
async def root_mcp():
    """executor 와 같은 경로로 테넌트 MCP 서버 도구를 불러온다(실제 stdio MCP)."""
    tools, _servers = await load_chat_root_tools(TENANT_MCP)
    assert {"lookup_order", "cancel_order", "ping_other"} <= {t.name for t in tools}
    return tools


async def _run(env, root_mcp, script, *, activity_skills=None, pinned=None, text="주문 A-1 상태 알려줘"):
    _, model = env
    model.script = script
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t-skill-bound",
        extra_tools=[*root_mcp, count_words, unrelated_source_tool],
        tenant_mcp=TENANT_MCP,
        checkpointer=InMemorySaver(),
        is_chat=True,
        activity_skills=activity_skills,
        pinned_skills=pinned,
    )
    if pinned:
        inputs, _ = build_graph_inputs(
            text, human_answer_stripped="", run_state={}, checkpointer=object(), checkpoint_thread_id="t",
            recent_messages=[], has_checkpoint=False, has_pending_interrupt=False, pinned_skills=pinned,
        )
    else:
        inputs = {"messages": [HumanMessage(text)]}
    return await graph.ainvoke(inputs, config={"configurable": {"thread_id": "t"}})


def _tool_result(state, name: str) -> ToolMessage:
    ids = {c["id"] for m in state["messages"] if isinstance(m, AIMessage) for c in m.tool_calls if c["name"] == name}
    return next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id in ids)


async def test_skill_only_mcp_tool_is_hidden_until_its_skill_is_read_then_called(env, root_mcp):
    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    state = await _run(env, root_mcp, [
        [("read_file", {"file_path": ORDER_SKILL})],
        [("lookup_order", {"order_id": "A-1"})],
    ])

    before, after = env[1].tool_names[0], env[1].tool_names[1]
    assert "lookup_order" not in before, "스킬을 읽기 전에 스킬 전용 MCP 도구가 보인다"
    assert "lookup_order" in after, "스킬을 읽은 뒤에도 스킬 전용 MCP 도구가 없다"
    assert "cancel_order" in before, "같은 서버라도 스킬 전용이 아닌 도구는 처음부터 보여야 한다"
    assert "ping_other" in before, "다른 MCP 서버 도구는 처음부터 보여야 한다"
    result = _tool_result(state, "lookup_order")
    assert result.status != "error"
    assert "ORDER A-1: shipped via MCP" in str(result.content), "MCP 서버의 실제 응답이 아니다"


async def test_skill_only_source_tool_is_hidden_until_its_skill_is_read_then_called(env, root_mcp):
    SKILL_ONLY.add("count_words")
    _write_skill(env[0], "word-counter", "count_words")
    state = await _run(env, root_mcp, [
        [("read_file", {"file_path": NOTE_SKILL})],
        [("count_words", {"text": "하나 둘 셋"})],
    ], text="단어 세어줘")

    assert "count_words" not in env[1].tool_names[0]
    assert "unrelated_source_tool" in env[1].tool_names[0], "스킬 전용이 아닌 소스 도구는 처음부터 보여야 한다"
    assert "count_words" in env[1].tool_names[1]
    assert _tool_result(state, "count_words").content == "WORDS=3"


async def test_tool_in_include_tools_but_not_skill_only_is_visible_from_the_start(env, root_mcp):
    """include_tools 만으로는 숨기지 않는다 — 공식 동작에서 에이전트 tools 의 도구는 처음부터 보인다."""
    _write_skill(env[0], "order-lookup", "lookup_order")
    _write_skill(env[0], "word-counter", "count_words")
    state = await _run(env, root_mcp, [[("lookup_order", {"order_id": "N-1"})]])

    first = env[1].tool_names[0]
    assert "lookup_order" in first, "스킬 전용이 아닌 MCP 도구가 include_tools 때문에 숨겨졌다"
    assert "count_words" in first, "스킬 전용이 아닌 소스 도구가 include_tools 때문에 숨겨졌다"
    assert "ORDER N-1: shipped via MCP" in str(_tool_result(state, "lookup_order").content)


async def test_skill_only_tool_called_before_reading_skill_does_not_run(env, root_mcp):
    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    state = await _run(env, root_mcp, [[("lookup_order", {"order_id": "A-1"})]])

    result = _tool_result(state, "lookup_order")
    assert result.status == "error"
    assert "shipped" not in str(result.content), "스킬을 읽기 전에 MCP 서버가 호출됐다"


async def test_reading_a_skill_that_does_not_list_the_tool_does_not_open_it(env, root_mcp):
    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    _write_skill(env[0], "word-counter", "count_words")
    state = await _run(env, root_mcp, [
        [("read_file", {"file_path": NOTE_SKILL})],
        [("lookup_order", {"order_id": "X-1"})],
    ])

    assert "lookup_order" not in env[1].tool_names[1], "그 도구를 적지 않은 스킬을 읽었는데 도구가 열렸다"
    assert _tool_result(state, "lookup_order").status == "error"


async def test_pinned_skill_discloses_its_skill_only_tools_from_the_first_request(env, root_mcp):
    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    state = await _run(env, root_mcp, [[("lookup_order", {"order_id": "P-9"})]], pinned=["order-lookup"])

    assert "lookup_order" in env[1].tool_names[0], "고정한 스킬의 도구가 첫 요청에 없다"
    assert "ORDER P-9: shipped via MCP" in str(_tool_result(state, "lookup_order").content)


async def test_filtered_skill_list_opens_skill_only_tool_and_leaves_others_visible(env, root_mcp):
    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    _write_skill(env[0], "word-counter", "count_words")
    state = await _run(env, root_mcp, [
        [("read_file", {"file_path": ORDER_SKILL})],
        [("lookup_order", {"order_id": "B-2"})],
    ], activity_skills=["order-lookup"])

    first, second = env[1].tool_names[0], env[1].tool_names[1]
    assert "lookup_order" not in first and "lookup_order" in second, "좁혀진 목록 안 스킬이 스킬 전용 도구를 열지 못했다"
    assert "count_words" in first, "스킬 전용이 아닌 도구는 목록과 상관없이 처음부터 보여야 한다"
    assert "ORDER B-2: shipped via MCP" in str(_tool_result(state, "lookup_order").content)


async def test_without_skill_only_designation_the_tool_set_is_unchanged(env, root_mcp, monkeypatch):
    _write_skill(env[0], "order-lookup", "lookup_order")
    await _run(env, root_mcp, [[]])
    with_feature = env[1].tool_names[0]

    env[1].tool_names = []
    monkeypatch.setattr(agent_mod, "scan_skill_include_tools", lambda *_a, **_k: {})
    await _run(env, root_mcp, [[]])
    without_feature = env[1].tool_names[0]

    assert with_feature == without_feature
    assert {"lookup_order", "cancel_order", "ping_other", "count_words"} <= set(with_feature)


async def test_duplicate_named_tool_binds_the_one_the_agent_would_run(env, root_mcp):
    """같은 이름이 두 번 붙으면 create_agent 는 마지막 것을 쓴다 — 바인딩도 그 도구를 넘겨야 한다."""

    @tool("lookup_order")
    def local_lookup_order(order_id: str) -> str:
        """주문 번호로 배송 상태를 조회한다(로컬 구현)."""
        return f"LOCAL {order_id}"

    SKILL_ONLY.add("lookup_order")
    _write_skill(env[0], "order-lookup", "lookup_order")
    _, model = env
    model.script = [[("read_file", {"file_path": ORDER_SKILL})], [("lookup_order", {"order_id": "D-1"})]]
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t-skill-bound", extra_tools=[*root_mcp, local_lookup_order], tenant_mcp=TENANT_MCP,
        checkpointer=InMemorySaver(), is_chat=True,
    )
    state = await graph.ainvoke({"messages": [HumanMessage("주문 D-1")]}, config={"configurable": {"thread_id": "t"}})

    assert "lookup_order" not in model.tool_names[0]
    assert _tool_result(state, "lookup_order").content == "LOCAL D-1"
