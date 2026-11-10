"""결과 전달을 보류한 턴의 사유가 다음 턴의 모델 입력에 들어간다.

VM take B(2026-10-29 15:19 UTC, 방 e3fd49bc…): 턴 끝 검증이 깨진 agents/quotation-agent.json 으로 보류했는데,
체크포인트에는 모델의 「…성공적으로 생성되었습니다」만 남았다. 사용자가 보류에 답하자 다음 턴 모델 입력은
[그 답, 「네, 이 흐름대로 프로세스를 만들어 주세요.」] 뿐이었고, 모델은 도구 없이 「이미 … 생성되어 있습니다」라고
답해 4초 뒤 같은 보류가 다시 나갔다.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from core.agents.input_builder import build_graph_inputs
from core.chat.delivery_hold import HOLD_HEADER, HOLD_SOURCE, hold_note, integration_failures, record_delivery_hold
from core.skills import tools

ROOM = "e3fd49bc-22b9-43cb-a481-854f54058afb"

# take B 의 두 번째 에이전트 파일(끝 `}` 없음)
BROKEN_AGENT = (
    '{\n  "id": "quotation-agent",\n  "name": "견적 산출 에이전트",\n'
    '  "role": "제품별 견적과 할인·설치비 기준 자동 계산",\n'
    '  "goal": "견적 요청 시 5분 이내 전체 제품 금액을 계산해 견적서 초안을 완성한다.",\n'
    '  "persona": "꼼꼼하고 계산에 오류가 없는 자동화 전문가.",\n'
    '  "description": "제품별 단가·수량 할인·설치비를 자동 산출하고, 견적서 초안을 작성합니다."\n'
)
NOTICE = (
    "프로세스 정의가 검증을 통과하지 못해 결과 전달을 보류했어요.\n"
    "- 'agents/quotation-agent.json' 이 올바른 JSON 이 아닙니다(Expecting ',' delimiter, 8행 1열). "
    "내용 전체를 다시 써 주세요(끝의 '}' 누락 등).\n"
    "어떻게 고칠지 알려주시면 이어서 수정할게요."
)
CLAIM = "「한빛오피스 견적 승인 규정」에 따라 대량 주문 견적 프로세스가 성공적으로 생성되었습니다."


def _take_b_failures(tmp_path):
    """take B 의 깨진 에이전트 파일로 실제 검증 결함을 만든다(턴 끝 검증이 쓰는 것과 같은 함수)."""
    work = tmp_path / ROOM
    (work / "agents").mkdir(parents=True)
    (work / "agents" / "quotation-agent.json").write_text(BROKEN_AGENT, encoding="utf-8")
    defects = tools._work_file_defects(work)
    assert defects, "깨진 에이전트 파일이 결함으로 잡혀야 한다"
    return [{"dir": ROOM, "defects": defects}]


def test_note_carries_what_the_user_saw_the_defects_and_what_to_do(tmp_path):
    note = hold_note("validation", NOTICE, _take_b_failures(tmp_path))

    assert isinstance(note, HumanMessage)
    assert note.additional_kwargs["lc_source"] == HOLD_SOURCE
    text = note.content
    assert text.startswith(HOLD_HEADER)
    assert "> 프로세스 정의가 검증을 통과하지 못해 결과 전달을 보류했어요." in text
    assert "agent_file_invalid_json" in text and "agents/quotation-agent.json" in text
    assert "Expecting ',' delimiter, 8행 1열" in text
    assert "전달되지 않았고" in text  # 마지막 답의 완료 문장이 사실이 아님을 알린다
    assert "고치기 전에는 완료·생성·수정했다고 말하지 않는다" in text


def test_integration_error_is_listed():
    failures = integration_failures(
        'ERROR: {"message": "검증 실패", "validation_errors": [{"dir": "process-1", '
        '"defects": [{"severity": "critical", "type": "missing_form", "file": "forms/a.form", "detail": "폼이 없습니다"}]}]}'
    )
    note = hold_note("integration", "프로세스 산출물을 통합하는 중 문제가 생겨 결과 전달을 보류했어요.", failures)
    assert "process-1: [critical · missing_form · forms/a.form] 폼이 없습니다" in note.content
    assert integration_failures("ERROR: 알 수 없는 오류")[0]["defects"][0]["detail"] == "알 수 없는 오류"


class _Model(BaseChatModel):
    """요청을 기록하고 정해진 답을 낸다."""

    requests: list = []
    replies: list = []

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.requests.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.replies.pop(0)))])


def _graph(model, saver, tmp_path):
    from deepagents import create_deep_agent
    from deepagents.backends.filesystem import FilesystemBackend

    from core.agents.skill_eval_gate import SkillEvalGateMiddleware
    from core.agents.steering import SteeringMiddleware

    # 채팅 그래프처럼 모델 뒤에 끝을 판단하는 미들웨어 노드가 붙어 있어야 갱신할 노드를 고르는 경로를 탄다.
    return create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        checkpointer=saver,
        system_prompt="sys",
        middleware=[SteeringMiddleware(conversation_id=ROOM), SkillEvalGateMiddleware()],
    )


def _next_turn(text: str):
    # executor 와 같다 — 체크포인트가 있으면 새 사용자 메시지만 이어 붙인다.
    inputs, resume = build_graph_inputs(
        text, human_answer_stripped="", run_state={}, checkpointer=object(), checkpoint_thread_id=ROOM,
        recent_messages=[], has_checkpoint=True, has_pending_interrupt=False,
    )
    assert resume is False
    return inputs


async def test_next_turn_model_input_contains_the_hold_before_the_user_reply(tmp_path):
    saver = InMemorySaver()
    model = _Model(requests=[], replies=[CLAIM, "깨진 파일부터 고칠게요."])
    graph = _graph(model, saver, tmp_path)
    config = {"configurable": {"thread_id": ROOM}}

    await graph.ainvoke({"messages": [{"role": "user", "content": "[에이전트] 견적 산출 에이전트"}]}, config)
    hold = {"kind": "validation", "failures": _take_b_failures(tmp_path)}
    assert await record_delivery_hold(graph, ROOM, hold, NOTICE) is True
    assert (await graph.aget_state(config)).next == ()  # 다음 턴을 막는 대기 단계가 생기지 않는다

    await graph.ainvoke(_next_turn("네, 이 흐름대로 프로세스를 만들어 주세요."), config)

    seen = model.requests[-1]
    contents = [str(m.content) for m in seen]
    i_claim = contents.index(CLAIM)
    i_note = next(i for i, c in enumerate(contents) if c.startswith(HOLD_HEADER))
    i_user = contents.index("네, 이 흐름대로 프로세스를 만들어 주세요.")
    assert i_claim < i_note < i_user
    assert "agents/quotation-agent.json" in contents[i_note]


async def test_turn_without_hold_is_unchanged(tmp_path):
    saver = InMemorySaver()
    model = _Model(requests=[], replies=["첫 답", "둘째 답"])
    graph = _graph(model, saver, tmp_path)
    config = {"configurable": {"thread_id": ROOM}}

    await graph.ainvoke({"messages": [{"role": "user", "content": "안녕"}]}, config)
    await graph.ainvoke(_next_turn("고마워"), config)

    assert [str(m.content) for m in model.requests[-1] if not m.type == "system"] == ["안녕", "첫 답", "고마워"]


async def test_failure_to_record_does_not_break_the_turn():
    class _Broken:
        async def aupdate_state(self, *args, **kwargs):
            raise RuntimeError("checkpointer down")

    assert await record_delivery_hold(_Broken(), ROOM, {"kind": "validation", "failures": []}, NOTICE) is False
    assert await record_delivery_hold(_Broken(), "", {"kind": "validation"}, NOTICE) is False
