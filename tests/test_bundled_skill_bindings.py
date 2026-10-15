"""특정 작업에만 쓰는 지침과 도구를 번들 시스템 스킬로 옮긴 것(work-report 2026-10-13 6절 바인딩 후보).

- ② 프로세스 실행 절차 → `process-execution` 스킬, `execute_process` 를 연결
- ① 스킬 생성 도구 4개 → 전역 `skill-creator` 스킬이 연결(6단계 지침은 기본 프롬프트에 그대로, 먼저 그 스킬을 읽게 함)
- ③ BSC 전략 저장 도구 4개 → 전략 서비스가 있을 때만 붙이고 `bsc-strategy-interview` 가 연결
- ④ 웹 조회 원칙 → 도구(`fetch_webpage`)가 붙을 때만 싣는다

관리자가 그 도구를 스킬 전용으로 지정하면 실행과 무관한 대화에는 지침도 도구 스키마도 실리지 않고,
스킬을 읽은 뒤에만 도구가 보인다. 실제 `build_agent` 와 저장소의 번들 스킬 원본을 쓴다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import core.agents.agent as agent_mod
from core.agents.skill_eval_gate import SKILL_CREATOR_PATH, SkillEvalGateMiddleware
from core.skills import tool_catalog

REPO = Path(__file__).resolve().parent.parent
BUNDLED = REPO / "system-skills" / "process-gpt-system"
SKILL_CREATOR = REPO / "skills" / "anthropics-skills" / "skill-creator"
EXECUTION_TOOLS = {"execute_process"}
BUILDER_TOOLS = {"plan_skill_eval", "complete_skill_creation", "publish_skill", "register_skill_as_tenant"}
STRATEGY_TOOLS = {"save_strategy_objective", "save_strategy_kpi", "save_canvas_block", "link_kpi_to_process"}


def _frontmatter(name: str, root: Path = BUNDLED) -> dict:
    text = (root / name / "SKILL.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])


class _Model(BaseChatModel):
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
        self.tool_names.append(set(self.bound))
        self.prompts.append("\n".join(str(m.content) for m in messages if isinstance(m, SystemMessage)))
        step = len(self.tool_names)
        calls = self.script[step - 1] if step <= len(self.script) else []
        if not calls:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="끝"))])
        tool_calls = [{"name": n, "args": a, "id": f"c{step}-{i}"} for i, (n, a) in enumerate(calls)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from deepagents.backends.filesystem import FilesystemBackend

    # 서버가 기동 때 하는 것처럼 번들 스킬을 스킬 디렉터리의 process-gpt-system 으로 옮긴다.
    shutil.copytree(BUNDLED, tmp_path / "skills" / "process-gpt-system")
    shutil.copytree(SKILL_CREATOR, tmp_path / "skills" / "anthropics-skills" / "skill-creator")
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    model = _Model(script=[], tool_names=[], prompts=[])
    designated: set[str] = set()

    def _no_git(_tenant_id):
        raise RuntimeError("no git provider in tests")

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda *a, **k: backend)
    monkeypatch.setattr(agent_mod, "_build_model", lambda: model)
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_mod, "_get_skills", lambda *a, **k: [str(tmp_path / "skills" / "anthropics-skills"), str(tmp_path / "skills" / "process-gpt-system")])
    monkeypatch.setattr(agent_mod, "_get_git_provider", _no_git)
    monkeypatch.setattr(tool_catalog, "load_skill_only_tools", lambda *_a, **_k: frozenset(designated))
    return model, designated


async def _run(model, script, *, root_profile=None, extra_tools=None):
    model.script = script
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t-bundled", checkpointer=InMemorySaver(), is_chat=True, root_profile=root_profile, extra_tools=extra_tools
    )
    return await graph.ainvoke({"messages": [HumanMessage("요청")]}, config={"configurable": {"thread_id": "t"}})


def test_bundled_skills_connect_only_their_own_tools():
    """번들 스킬이 연결한 도구는 실제 루트 도구 이름과 같고, 범용 조회 도구는 연결하지 않는다."""
    root = {t.name for t in agent_mod.root_service_tools(is_chat=True)}
    execution = set(_frontmatter("process-execution")["metadata"]["include_tools"].split())
    builder = set(_frontmatter("skill-creator", SKILL_CREATOR.parent)["metadata"]["include_tools"].split())
    assert execution == EXECUTION_TOOLS
    assert builder == BUILDER_TOOLS
    assert (execution | builder) <= root, "연결한 이름이 실제 도구 이름과 달라 열리지 않는다"
    assert not {"get_process_list", "get_process_detail", "get_form_fields", "get_organization"} & (execution | builder)


async def test_designated_tools_open_only_after_reading_their_bundled_skill(env):
    model, designated = env
    designated |= EXECUTION_TOOLS | BUILDER_TOOLS
    await _run(model, [[("read_file", {"file_path": "/skills/process-gpt-system/process-execution/SKILL.md"})], []])
    before, after = model.tool_names
    assert not (EXECUTION_TOOLS | BUILDER_TOOLS) & before, "읽기 전 요청에 스킬 전용 도구가 실렸다"
    assert EXECUTION_TOOLS <= after, "process-execution 을 읽었는데 execute_process 가 열리지 않았다"
    assert not BUILDER_TOOLS & after, "다른 스킬을 읽었는데 스킬 생성 도구가 열렸다"


async def test_without_designation_the_tools_stay_visible(env):
    """지정하지 않은 테넌트는 지금처럼 도구가 처음부터 보인다(지침은 스킬에서 읽는다)."""
    model, _ = env
    await _run(model, [[]])
    assert (EXECUTION_TOOLS | BUILDER_TOOLS) <= model.tool_names[0]


@pytest.mark.parametrize("root_profile", [None, {"name": "process-gpt-agent"}])
async def test_execution_and_web_fetch_rules_left_the_system_prompt(env, root_profile):
    model, _ = env
    await _run(model, [[]], root_profile=root_profile)
    prompt = model.prompts[0]
    assert "누구로 설정하시겠습니까" not in prompt, "프로세스 실행 절차가 다시 시스템 프롬프트에 실렸다"
    assert "fetch_webpage" not in prompt, "도구가 없는데 웹 조회 원칙이 실렸다"
    assert "process-execution" in prompt, "스킬 목록에 실행 스킬이 없다"
    assert "누구로 설정하시겠습니까" in (BUNDLED / "process-execution" / "SKILL.md").read_text(encoding="utf-8")


async def test_web_fetch_rules_come_with_the_tool(env):
    model, _ = env

    @tool
    def fetch_webpage(url: str) -> str:
        """웹페이지 본문을 가져온다."""
        return url

    await _run(model, [[]], extra_tools=[fetch_webpage])
    assert "fetch_webpage" in model.prompts[0]


def test_skill_creation_gate_tells_how_to_open_its_tools():
    """스킬 전용으로 지정되면 complete_skill_creation 은 skill-creator 를 읽어야 보인다 — 게이트가 그 길을 알려야 한다."""
    assert (SKILL_CREATOR / "SKILL.md").exists()
    assert SKILL_CREATOR_PATH == "/skills/anthropics-skills/skill-creator/SKILL.md"
    mw = SkillEvalGateMiddleware()
    out = mw.after_model({"messages": [
        HumanMessage(content="회의록 요약 스킬 만들어줘"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/workspace/.bpmn/r/skills/x/SKILL.md", "content": "---"}, "id": "w1"}]),
        ToolMessage(content="ok", name="write_file", tool_call_id="w1"),
        AIMessage(content="스킬을 생성했어요."),
    ]}, None)
    assert out and SKILL_CREATOR_PATH in out["messages"][0].content


def test_strategy_save_tools_come_only_with_the_service_and_open_with_the_bsc_skill(monkeypatch):
    monkeypatch.delenv("STRATEGY_SERVICE_URL", raising=False)
    assert not STRATEGY_TOOLS & {t.name for t in agent_mod.root_service_tools(is_chat=True)}, "전략 서비스가 없는데 저장 도구가 붙었다"
    monkeypatch.setenv("STRATEGY_SERVICE_URL", "http://strategy:8000")
    assert STRATEGY_TOOLS <= {t.name for t in agent_mod.root_service_tools(is_chat=True)}
    assert set(_frontmatter("bsc-strategy-interview")["metadata"]["include_tools"].split()) == STRATEGY_TOOLS


async def test_designated_strategy_tools_open_after_reading_the_bsc_skill(env, monkeypatch):
    monkeypatch.setenv("STRATEGY_SERVICE_URL", "http://strategy:8000")
    model, designated = env
    designated |= STRATEGY_TOOLS
    await _run(model, [[("read_file", {"file_path": "/skills/process-gpt-system/bsc-strategy-interview/SKILL.md"})], []])
    before, after = model.tool_names
    assert not STRATEGY_TOOLS & before
    assert STRATEGY_TOOLS <= after


async def test_skill_creation_tools_open_after_reading_skill_creator(env):
    model, designated = env
    designated |= BUILDER_TOOLS
    await _run(model, [[("read_file", {"file_path": "/skills/anthropics-skills/skill-creator/SKILL.md"})], []])
    before, after = model.tool_names
    assert not BUILDER_TOOLS & before
    assert BUILDER_TOOLS <= after, "skill-creator 를 읽었는데 스킬 생성 도구가 열리지 않았다"
    # 6단계 지침은 기본 프롬프트에 남고, 먼저 skill-creator 를 읽으라고 한다.
    assert "skill-creator 스킬의 SKILL.md 를 read_file 로 읽는다" in agent_mod.DEFAULT_SYSTEM_PROMPT
