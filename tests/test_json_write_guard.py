"""`.json` 파일에 JSON 이 아닌 내용을 쓰는 write_file 을 쓰기 전에 돌려보낸다.

VM take B(2026-10-29 15:19 UTC, 방 e3fd49bc…)의 체크포인트에 남은 실제 호출: 한 응답에 write_file 두 개,
`finish_reason=tool_calls`, 두 번째 `agents/quotation-agent.json` 내용만 끝 `}` 가 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from core.agents.json_write_guard import JsonWriteGuardMiddleware, invalid_json_write

ROOM = "/workspace/.bpmn/e3fd49bc-22b9-43cb-a481-854f54058afb"

# take B 체크포인트의 두 호출 내용 그대로(첫 파일은 온전, 둘째는 끝 `}` 누락)
FIRST = (
    '{\n  "id": "inventory-agent",\n  "name": "재고 확인 에이전트",\n'
    '  "role": "재고 확인 및 납기 가능성 자동 조회",\n'
    '  "goal": "주문 요청 시 각 제품의 재고와 납기를 1분 이내 정확하게 조회하여 기록한다.",\n'
    '  "persona": "논리적이고 빠른 분석가.",\n'
    '  "description": "재고 시스템에서 제품별 재고 현황을 확인합니다."\n}'
)
SECOND_CUT = (
    '{\n  "id": "quotation-agent",\n  "name": "견적 산출 에이전트",\n'
    '  "role": "제품별 견적과 할인·설치비 기준 자동 계산",\n'
    '  "goal": "견적 요청 시 5분 이내 전체 제품 금액을 계산해 견적서 초안을 완성한다.",\n'
    '  "persona": "꼼꼼하고 계산에 오류가 없는 자동화 전문가.",\n'
    '  "description": "제품별 단가·수량 할인·설치비를 자동 산출하고, 견적서 초안을 작성합니다."\n'
)


def _call(path: str, content, name: str = "write_file", cid: str = "c1") -> dict:
    return {"name": name, "args": {"file_path": path, "content": content}, "id": cid}


def test_take_b_second_agent_file_is_refused():
    err = invalid_json_write(_call(f"{ROOM}/agents/quotation-agent.json", SECOND_CUT))
    assert err is not None
    assert "agents/quotation-agent.json" in err
    assert "Expecting ',' delimiter, 8행 1열" in err  # 턴 끝 검증이 보류 글에 적던 것과 같은 자리
    assert "다시 쓰" in err


def test_valid_json_and_other_files_pass():
    assert invalid_json_write(_call(f"{ROOM}/agents/inventory-agent.json", FIRST)) is None
    assert invalid_json_write(_call(f"{ROOM}/agents/quotation-agent.json", SECOND_CUT + "}")) is None
    # 폼은 HTML, 문서는 자유 형식 — 보지 않는다
    assert invalid_json_write(_call(f"{ROOM}/forms/receive_request.form", "<section>")) is None
    assert invalid_json_write(_call("/workspace/notes.md", "{ not json")) is None
    # edit_file 은 조각만 바꾸므로 보지 않는다
    assert invalid_json_write({"name": "edit_file", "args": {"file_path": f"{ROOM}/manifest.json",
                                                             "old_string": "a", "new_string": "{"}, "id": "e"}) is None


class _ParallelAgentFilesModel(BaseChatModel):
    """take B 의 모델처럼 두 에이전트 파일을 한 응답에 쓰고, 오류를 받으면 그 파일만 다시 쓴다."""

    requests: list = []

    @property
    def _llm_type(self) -> str:
        return "parallel-agent-files"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.requests.append(list(messages))
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        if not tool_results:
            msg = AIMessage(content="", tool_calls=[
                _call(f"{ROOM}/agents/inventory-agent.json", FIRST, cid="w1"),
                _call(f"{ROOM}/agents/quotation-agent.json", SECOND_CUT, cid="w2"),
            ])
        elif tool_results[-1].status == "error" and len(self.requests) == 2:
            msg = AIMessage(content="", tool_calls=[
                _call(f"{ROOM}/agents/quotation-agent.json", SECOND_CUT + "}", cid="w3"),
            ])
        else:
            msg = AIMessage(content="에이전트 두 명을 만들었어요.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


async def test_refused_file_is_not_written_and_model_rewrites_it_in_the_same_turn(tmp_path: Path):
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    model = _ParallelAgentFilesModel(requests=[])
    graph = create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        checkpointer=InMemorySaver(),
        system_prompt="sys",
        middleware=[JsonWriteGuardMiddleware()],
    )
    state = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "[에이전트] 재고 확인 에이전트, [에이전트] 견적 산출 에이전트"}]},
        config={"configurable": {"thread_id": "t"}},
    )

    results = {m.tool_call_id: m for m in state["messages"] if isinstance(m, ToolMessage)}
    assert results["w1"].status != "error"
    assert results["w2"].status == "error" and "올바른 JSON 이 아니어서" in str(results["w2"].content)
    # 두 번째 모델 요청은 거절 글을 보고, 그 파일 하나만 다시 쓴다
    assert len(model.requests) == 3
    assert results["w3"].status != "error"
    agents_dir = tmp_path / "workspace/.bpmn/e3fd49bc-22b9-43cb-a481-854f54058afb/agents"
    for name in ("inventory-agent.json", "quotation-agent.json"):
        assert json.loads((agents_dir / name).read_text(encoding="utf-8"))["id"] == name[:-5]
