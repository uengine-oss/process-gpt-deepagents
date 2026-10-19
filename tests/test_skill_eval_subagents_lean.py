"""스킬 생성 평가 서브에이전트가 기록 작업과 쓰지 않는 파일 도구로 호출·입력을 늘리지 않는다.

- `plan_skill_eval` 이 실행 폴더를 미리 만든다 → test-runner 가 `mkdir` 로 호출을 쓰지 않는다.
- test-runner 지침에 폴더 생성·시간 측정·`timing.json` 단계가 없다(서비스가 읽지 않고, 공식
  skill-creator 는 메인 에이전트가 기록한다).
- test-runner·evaluator 는 측정에서 쓰지 않은 파일 도구를 받지 않는다. 실제 `build_agent` 조립에서
  서브에이전트가 모델에 보내는 도구 목록으로 확인한다(openspec: skill-eval-subagent-lean).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

import core.agents.agent as agent_mod
from core.agents.skill_creator_subagents import _TEST_RUNNER_SYSTEM, build_skill_creator_subagents
from core.skills import eval_tools
from core.skills import tool_catalog
from core.skills import tools as skill_tools

TENANT = "t-eval-lean"
ROOM = "room-lean"
SKILL = "nda-review"
# 테스트 백엔드(FilesystemBackend)는 셸이 없어 deepagents 가 `execute` 를 붙이지 않는다.
FS_ALL = {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"}


# ---------------------------------------------------------------------------
# plan_skill_eval 이 실행 폴더를 만든다
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", workspace)
    t1 = skill_tools._current_tenant_id.set(TENANT)
    t2 = skill_tools._current_conversation_id.set(ROOM)
    yield workspace / TENANT
    skill_tools._current_tenant_id.reset(t1)
    skill_tools._current_conversation_id.reset(t2)


def _write_skill_with_evals(ws: Path) -> Path:
    d = ws / ".bpmn" / ROOM / "skills" / SKILL
    (d / "evals").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {SKILL}\ndescription: NDA 검토\n---\n\n절차\n", encoding="utf-8")
    cases = [
        {"id": i, "name": f"case-{i}", "prompt": f"NDA {i} 검토", "expected_output": "요약",
         "files": [], "assertions": ["위험도를 적는다", "수정 제안을 적는다"]}
        for i in (1, 2)
    ]
    (d / "evals" / "evals.json").write_text(json.dumps({"skill_name": SKILL, "evals": cases}), encoding="utf-8")
    return d


def test_plan_skill_eval_은_모든_실행_폴더를_미리_만든다(ws):
    skill_dir = _write_skill_with_evals(ws)

    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill_dir)}))

    assert len(out["runs"]) == 4
    for run in out["runs"]:
        host = ws / run["outputs_dir"].removeprefix("/workspace/")
        assert host.is_dir(), f"실행 폴더가 없다 — test-runner 가 mkdir 로 호출을 쓴다: {run['outputs_dir']}"


# ---------------------------------------------------------------------------
# test-runner 지침
# ---------------------------------------------------------------------------

def test_test_runner_지침에_기록_작업이_없고_실행_규칙은_남아_있다():
    prompt = _TEST_RUNNER_SYSTEM
    assert "timing.json" in prompt and "no `timing.json`" in prompt, "시간 기록을 하지 말라는 지시가 없다"
    assert "executor_duration_seconds" not in prompt, "timing.json 을 쓰라는 지시가 남아 있다"
    assert "Create `outputs_dir`" not in prompt, "폴더를 만들라는 지시가 남아 있다"
    assert "outputs_dir` already exists" in prompt
    assert "extract_document_structure" in prompt, "첨부 문서 읽기 규칙이 사라졌다"
    assert "Save every created or transformed output file to `outputs_dir`" in prompt


# ---------------------------------------------------------------------------
# 서브에이전트가 모델에 보내는 도구
# ---------------------------------------------------------------------------

class _RecordingModel(BaseChatModel):
    """대본대로 task 를 부르고, 매 요청의 도구 이름을 기록한다. 서브에이전트도 같은 모델을 쓴다."""

    script: list = []
    tool_names: list = []
    bound: list = []

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(self, tools, **kwargs):
        bound = [getattr(t, "name", None) or t.get("name") or t["function"]["name"] for t in tools]
        clone = self.model_copy()
        clone.bound = bound
        clone.tool_names = self.tool_names
        clone.script = self.script
        return clone

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.tool_names.append(list(self.bound))
        step = len(self.tool_names)
        calls = self.script[step - 1] if step <= len(self.script) else []
        if not calls:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="끝"))])
        tool_calls = [{"name": n, "args": a, "id": f"c{step}-{i}"} for i, (n, a) in enumerate(calls)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))])


@pytest.fixture
def env(tmp_path, monkeypatch):
    from deepagents.backends.filesystem import FilesystemBackend

    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    model = _RecordingModel(script=[], tool_names=[])
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda *a, **k: backend)
    monkeypatch.setattr(agent_mod, "_build_model", lambda: model)
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_mod, "_get_skills", lambda *a, **k: [])
    monkeypatch.setattr(agent_mod, "_get_git_provider", lambda *_a: (_ for _ in ()).throw(RuntimeError("no git")))
    monkeypatch.setattr(tool_catalog, "load_skill_only_tools", lambda *_a, **_k: frozenset())
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path / "workspace")
    token = skill_tools._current_tenant_id.set(TENANT)
    yield model
    skill_tools._current_tenant_id.reset(token)


async def _subagent_tools(model, subagent_type: str) -> set[str]:
    """루트가 그 서브에이전트를 한 번 부르게 하고, 서브에이전트 첫 요청의 도구 이름을 돌려준다."""
    model.script = [[("task", {"subagent_type": subagent_type, "description": "평가 하나 실행"})], [], []]
    graph, _, _ = await agent_mod.build_agent(
        tenant_id=TENANT,
        checkpointer=InMemorySaver(),
        is_chat=True,
        static_subagents=build_skill_creator_subagents(task_id=ROOM),
    )
    await graph.ainvoke({"messages": [HumanMessage("스킬 평가")]}, config={"configurable": {"thread_id": "t"}})
    assert len(model.tool_names) >= 2, "서브에이전트가 모델을 부르지 않았다"
    return set(model.tool_names[1])


async def test_test_runner_는_쓰지_않는_파일_도구를_받지_않는다(env):
    names = await _subagent_tools(env, "test-runner")

    assert not names & {"delete", "glob", "grep"}, f"쓰지 않는 파일 도구가 남아 있다: {names & {'delete', 'glob', 'grep'}}"
    assert {"ls", "read_file", "write_file", "edit_file"} <= names
    assert {"download_file", "extract_document_structure"} <= names, "첨부 문서를 가져오고 읽을 도구가 빠졌다"


async def test_evaluator_는_쓰지_않는_파일_도구를_받지_않는다(env):
    names = await _subagent_tools(env, "evaluator")

    assert not names & {"delete", "edit_file"}
    assert {"ls", "read_file", "write_file", "glob", "grep"} <= names


async def test_다른_서브에이전트는_파일_도구를_모두_받는다(env):
    names = await _subagent_tools(env, "skill-creator")

    assert FS_ALL <= names
