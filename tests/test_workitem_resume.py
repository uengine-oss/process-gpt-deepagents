"""워크아이템 재개 — 크래시 재점유 뒤 마지막 체크포인트부터 이어 간다.

스펙: infra/process-gpt/openspec/changes/workitem-resume-default/specs/deepagents_workitem-checkpoint-resume
  RE-1.1, RE-1.2, RE-3.1 (RE-2.3 은 deepagents_workitem-human-input 의 답 원문 재개)

2026-10-06 실측(E2): 재점유된 실행이 같은 지시를 새 user 메시지로 덧붙여
처음부터 다시 돌아, 이미 끝난 외부 조회(fetch_data)를 3/3 다시 호출하고 LLM 도
한 번 더 불렀다. 입력 None 으로 마지막 체크포인트부터 실행하면 0 이었다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from core.agents.input_builder import (
    build_graph_inputs,
    choose_reclaim_inputs,
    resume_kind_of,
    reclaim_continues_from_checkpoint,
)


# ---------------------------------------------------------------------------
# 결정 규칙
# ---------------------------------------------------------------------------

def test_resume_kind_is_read_from_sdk_extras():
    assert resume_kind_of({"resume": {"kind": "reclaim", "attempt": 2}}) == "reclaim"
    assert resume_kind_of({"resume": {"kind": "human_answer"}}) == "human_answer"
    # RE-3.1 구버전 SDK 는 resume 을 싣지 않는다 → 이전 동작
    assert resume_kind_of({}) == "fresh"
    assert resume_kind_of({"resume": "broken"}) == "fresh"
    assert resume_kind_of(None) == "fresh"


@pytest.mark.parametrize(
    "kind,use_graph_resume,pending,snap_next,expected",
    [
        ("reclaim", False, False, ("tools",), True),   # RE-1.1 이어서
        ("reclaim", False, False, (), False),          # RE-1.2 그래프가 이미 끝남
        ("reclaim", True, True, ("tools",), False),     # RE-2.3 남은 interrupt 가 우선
        ("reclaim", False, True, ("tools",), False),
        ("fresh", False, False, ("tools",), False),     # RE-3.1
        ("revision", False, False, ("tools",), False),
        ("human_answer", False, False, ("tools",), False),
    ],
)
def test_reclaim_continues_only_for_unfinished_graph(kind, use_graph_resume, pending, snap_next, expected):
    assert reclaim_continues_from_checkpoint(
        resume_kind=kind,
        use_graph_resume=use_graph_resume,
        has_pending_interrupt=pending,
        snapshot_next=snap_next,
    ) is expected


# ---------------------------------------------------------------------------
# 실제 deepagents 그래프로 E2 재현 (모델만 대본대로 움직이는 가짜)
# ---------------------------------------------------------------------------

class _Crash(BaseException):
    """kill -9 대역. 도구 오류 처리(Exception)에 잡히지 않고 실행을 끊는다."""


class _ScriptedModel(BaseChatModel):
    """E2 작업 대본: fetch_data → generate_report → 최종 답.

    대화에 이미 있는 도구 결과를 보고 다음 수를 정한다. 실제 모델처럼, 같은
    지시가 다시 들어오면(현재 코드의 재점유 경로) 조회부터 다시 한다.
    """

    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(len(messages))
        humans = [m for m in messages if isinstance(m, HumanMessage)]
        last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        after = messages[last_human + 1:]
        done = {m.name for m in after if isinstance(m, ToolMessage) and "cancelled" not in str(m.content)}
        if len(humans) > 1:
            # 같은 지시를 다시 받으면 처음부터: 조회와 보고서를 한 번에 다시 부른다(E2 로그 그대로)
            if "generate_report" not in done:
                msg = AIMessage(content="", tool_calls=[
                    {"name": "fetch_data", "args": {"region": "서울"}, "id": f"f{len(self.calls)}"},
                    {"name": "generate_report", "args": {"customers": "A사,B사,C사"}, "id": f"g{len(self.calls)}"},
                ])
                return ChatResult(generations=[ChatGeneration(message=msg)])
        elif "fetch_data" not in {m.name for m in messages if isinstance(m, ToolMessage)}:
            msg = AIMessage(content="", tool_calls=[{"name": "fetch_data", "args": {"region": "서울"}, "id": "f1"}])
            return ChatResult(generations=[ChatGeneration(message=msg)])
        elif "generate_report" not in done:
            msg = AIMessage(content="", tool_calls=[
                {"name": "generate_report", "args": {"customers": "A사,B사,C사"}, "id": "g1"},
            ])
            return ChatResult(generations=[ChatGeneration(message=msg)])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="REPORT-7731"))])


PROMPT = "fetch_data 로 서울 고객을 조회하고, 그 결과로 generate_report 를 호출한 뒤 보고서 ID 를 답하라."


def _agent(saver, counter: dict, crash: bool):
    from deepagents import create_deep_agent

    @tool
    def fetch_data(region: str) -> str:
        """지역 고객 목록을 조회한다(외부 CRM, 유료)."""
        counter["fetch_data"] += 1
        return json.dumps({"region": region, "customers": ["A사", "B사", "C사"]}, ensure_ascii=False)

    @tool
    def generate_report(customers: str) -> str:
        """보고서를 생성한다(오래 걸림)."""
        counter["generate_report"] += 1
        if crash:
            raise _Crash()
        return "REPORT-7731"

    model = _ScriptedModel()
    return create_deep_agent(model=model, tools=[fetch_data, generate_report], checkpointer=saver,
                             system_prompt="너는 업무 자동화 에이전트다."), model


async def _crash_then_reclaim(thread: str, *, resume_kind: str) -> dict:
    """1차 실행을 generate_report 중에 끊고, 2차 실행을 서비스 결정 경로로 재개한다."""
    saver = InMemorySaver()  # 운영의 Postgres 대역 — 두 '프로세스' 가 같은 저장소를 본다
    config = {"configurable": {"thread_id": thread}}
    counter = {"fetch_data": 0, "generate_report": 0}

    graph1, _ = _agent(saver, counter, crash=True)
    first_inputs, _ = build_graph_inputs(
        PROMPT, human_answer_stripped="", run_state={}, checkpointer=saver,
        checkpoint_thread_id=thread, recent_messages=[], has_checkpoint=False, has_pending_interrupt=False,
    )
    with pytest.raises(_Crash):
        async for _ in graph1.astream(first_inputs, config):
            pass
    after_crash = dict(counter)

    # ---- 재점유: executor 와 같은 순서 ----
    graph2, model2 = _agent(saver, counter, crash=False)
    model2.calls.clear()
    tup = await saver.aget_tuple(config)
    inputs, use_graph_resume = build_graph_inputs(
        PROMPT, human_answer_stripped="", run_state={}, checkpointer=saver,
        checkpoint_thread_id=thread, recent_messages=[], has_checkpoint=tup is not None, has_pending_interrupt=False,
    )
    inputs = await choose_reclaim_inputs(
        graph2, config, inputs,
        resume_kind=resume_kind, use_graph_resume=use_graph_resume, has_pending_interrupt=False,
    )
    final = None
    async for chunk in graph2.astream(inputs, config, stream_mode="values"):
        final = chunk
    msgs = final["messages"]
    return {
        "fetch_after_resume": counter["fetch_data"] - after_crash["fetch_data"],
        "report_after_resume": counter["generate_report"] - after_crash["generate_report"],
        "llm_after_resume": len(model2.calls),
        "prompt_count": sum(1 for m in msgs if isinstance(m, HumanMessage) and PROMPT in str(m.content)),
        "answer": msgs[-1].content,
    }


def test_re_1_1_reclaim_resumes_from_checkpoint_without_repeating_finished_tools():
    result = asyncio.run(_crash_then_reclaim("todo-reclaim", resume_kind="reclaim"))
    assert result["fetch_after_resume"] == 0          # 완료된 외부 조회 재호출 0
    assert result["report_after_resume"] == 1         # 끊긴 도구만 다시 (at-least-once)
    assert result["llm_after_resume"] == 1            # 기준(중단 없음) 3회 중 남은 1회만
    assert result["prompt_count"] == 1               # 지시가 대화에 두 번 들어가지 않는다
    assert result["answer"] == "REPORT-7731"


def test_re_3_1_without_resume_signal_keeps_previous_behaviour():
    """신호가 없으면 이전과 같다 — 같은 지시를 덧붙여 조회부터 다시 한다(E2 현재 코드)."""
    result = asyncio.run(_crash_then_reclaim("todo-legacy", resume_kind="fresh"))
    assert result["fetch_after_resume"] == 1
    assert result["prompt_count"] == 2


def test_executor_is_wired_to_the_resume_decisions():
    """executor 전체를 띄우려면 DB·MCP 가 필요하다. 결정 함수는 위에서 실제 그래프로
    검증했고, 여기서는 워크아이템 실행이 그 함수들을 실제로 거치는지만 지킨다."""
    import inspect

    import executor

    src = inspect.getsource(executor.DeepAgentExecutor.execute)
    assert "choose_reclaim_inputs(" in src
    assert "resume_kind=resume_kind_of(extras)" in src
    assert "get_workitem_checkpointer()" in src
    assert src.index("choose_reclaim_inputs(") < src.index("graph.astream(")
