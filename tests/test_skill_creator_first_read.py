"""스킬 생성 요청이면 첫 도구 호출이 skill-creator SKILL.md 읽기다.

프롬프트의 "먼저 skill-creator 를 읽는다" 만으로는 gpt-5 가 서브에이전트 위임부터 했다
(bind-skill-tools verification 2026-10-16 gpt-5 절). 그래서 SkillCreatorFirstReadMiddleware 가
스킬 생성 요청인 턴의 첫 모델 호출을 read_file 호출로 대신한다.

실제 `build_agent` 조립을 쓴다. 모델은 gpt-5 처럼 첫 호출부터 다른 도구를 부르는 대본 모델이다 —
미들웨어를 빼면 첫 도구 호출이 그 도구가 되어 아래 테스트가 실패한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import core.agents.agent as agent_mod
from core.agents.skill_creator_first_read import is_skill_creation_request
from core.skills import tool_catalog

CREATOR = "/skills/coll/skill-creator/SKILL.md"


@tool
def plan_skill_eval(skill_path: str) -> str:
    """스킬 평가 계획을 세운다."""
    return "PLANNED"


class _ScriptedModel(BaseChatModel):
    """대본대로 도구를 부르고, 매 요청에 보인 도구 이름을 기록한다."""

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


def _write_skill(root: Path, name: str, include_tools: str = "") -> None:
    d = root / "skills" / "coll" / name
    d.mkdir(parents=True, exist_ok=True)
    meta = f"metadata:\n  include_tools: {include_tools}\n" if include_tools else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 스킬\n{meta}---\n\n# {name}\n절차대로 한다.\n",
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    # 실제 운영 지정과 같이 스킬 생성 도구를 스킬 전용으로 둔다(skill-creator 를 읽어야 보인다).
    monkeypatch.setattr(tool_catalog, "load_skill_only_tools", lambda *_a, **_k: frozenset({"plan_skill_eval"}))
    _write_skill(tmp_path, "skill-creator", "plan_skill_eval")
    _write_skill(tmp_path, "nda-review")
    return tmp_path, model


# gpt-5 처럼 첫 호출부터 다른 도구(여기서는 ls)를 부르는 모델.
_EAGER = [[("ls", {"path": "/"})], []]


async def _turns(env, texts, *, script=_EAGER):
    _, model = env
    model.script = script
    model.tool_names = []
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t-first-read", extra_tools=[plan_skill_eval], checkpointer=InMemorySaver(), is_chat=True,
    )
    state = None
    for text in texts:
        state = await graph.ainvoke({"messages": [HumanMessage(text)]}, config={"configurable": {"thread_id": "t"}})
    return state, model


def _tool_calls(state) -> list[tuple[str, dict]]:
    return [(c["name"], c["args"]) for m in state["messages"] if isinstance(m, AIMessage) for c in m.tool_calls]


def _creator_reads(state) -> int:
    return sum(1 for n, a in _tool_calls(state) if n == "read_file" and a.get("file_path") == CREATOR)


async def test_skill_creation_request_reads_skill_creator_first(env):
    state, model = await _turns(env, ["회의록에서 액션 아이템만 뽑는 스킬 만들어줘"])

    calls = _tool_calls(state)
    assert calls[0] == ("read_file", {"file_path": CREATOR, "limit": 1000})
    assert calls[1][0] == "ls"
    read_result = next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "read_file")
    assert read_result.status != "error" and "skill-creator" in read_result.content
    # 읽기가 진짜 스킬 읽기로 인정되어 첫 모델 요청부터 스킬 전용 도구가 보인다.
    assert "plan_skill_eval" in model.tool_names[0]


@pytest.mark.parametrize("run", range(3))
async def test_same_request_keeps_order(env, run):
    state, _ = await _turns(env, ["회의록에서 액션 아이템만 뽑는 스킬 만들어줘"])
    assert _tool_calls(state)[0] == ("read_file", {"file_path": CREATOR, "limit": 1000})


@pytest.mark.parametrize("text", [
    "우리 회사 프로세스 목록 알려줘",
    "nda-review 스킬로 이 계약서 검토해서 메일 작성해줘",
    "스킬 목록 보여줘",
    "구매 요청 프로세스 만들어줘",
])
async def test_other_requests_do_not_read_skill_creator(env, text):
    state, model = await _turns(env, [text])
    assert _creator_reads(state) == 0
    assert _tool_calls(state)[0][0] == "ls"
    assert "plan_skill_eval" not in model.tool_names[0]


async def test_not_read_again_in_same_conversation(env):
    state, _ = await _turns(env, ["회의록 정리 스킬 만들어줘", "같은 방식으로 이메일 요약 스킬도 만들어줘"])
    assert _creator_reads(state) == 1


async def test_model_read_counts_as_read(env):
    """모델이 스스로 먼저 읽은 대화에서는 다음 턴에 다시 끼워 넣지 않는다."""
    script = [[("read_file", {"file_path": CREATOR})], [], [("ls", {"path": "/"})], []]
    state, _ = await _turns(env, ["안녕", "회의록 정리 스킬 만들어줘"], script=script)
    assert _creator_reads(state) == 1


async def test_skill_creator_not_listed(env):
    root, _ = env
    (root / "skills" / "coll" / "skill-creator" / "SKILL.md").unlink()
    state, _ = await _turns(env, ["회의록 정리 스킬 만들어줘"])
    assert _tool_calls(state)[0][0] == "ls"


@pytest.mark.parametrize("text", [
    "회의록에서 액션 아이템만 뽑는 스킬 만들어줘",
    # 2026-10-16 gpt-5 측정에서 보낸 요청 원문
    "회의록 텍스트를 받아 결정사항과 액션 아이템(담당자·기한)을 표로 정리하는 스킬을 만들어줘. "
    "스킬 이름은 meeting-action-items 로 하고, 평가 예시는 네가 알아서 2개 정도 잡아서 확인 질문 없이 끝까지 진행해줘.",
    "NDA 검토 스킬을 새로 생성해 줘",
    "이 업무 절차를 스킬로 만들어줘",
    "보고서 작성 skill 만들어줘",
    "이 절차 스킬화해줘",
    "신규 스킬 작성 부탁해",
    "Create a skill that summarizes meeting notes",
    "Please make me a new reusable skill for invoices",
])
def test_detects_creation(text):
    assert is_skill_creation_request(text)


@pytest.mark.parametrize("text", [
    "우리 회사 프로세스 목록 알려줘",
    "nda-review 스킬로 이 계약서 검토해서 메일 작성해줘",
    "스킬 목록 보여줘",
    "어제 생성된 스킬 보여줘",
    "내가 만들었던 스킬 삭제해줘",
    "구매 요청 프로세스 만들어줘",
    "Use the nda skill to review this",
])
def test_ignores_non_creation(text):
    assert not is_skill_creation_request(text)
