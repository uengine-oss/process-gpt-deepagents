"""수정 지시(스티어링) — SDK 표준 동작의 이쪽(deepagents) 구현.

SDK 는 접수 대기열과 이벤트 두 개(`steer_accepted` / `steer_applied`)까지를
소유하고, "지금 방향을 바꿀 수 있는가" 와 "실제로 언제 갈아타는가" 는 이 어댑터가
정한다. 여기서 보는 것은 그 판정과, 갈아탈 때 그래프에 넣는 입력이다.
"""

import pytest
from processgpt_agent_sdk.steering import (
    REASON_AWAITING_HUMAN_INPUT,
    SteerDirective,
)

import executor as ex
from executor import DeepAgentExecutor


class _FakeTuple:
    def __init__(self, pending_writes):
        self.pending_writes = pending_writes


class _FakeCheckpointer:
    def __init__(self, tup):
        self._tup = tup
        self.asked = []

    async def aget_tuple(self, config):
        self.asked.append(config)
        return self._tup


@pytest.fixture(autouse=True)
def clean_awaiting_marks():
    ex._awaiting_human.clear()
    yield
    ex._awaiting_human.clear()


@pytest.fixture
def no_postgres(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(ex, "get_async_postgres_checkpointer", _none)


def _use_checkpointer(monkeypatch, tup):
    checkpointer = _FakeCheckpointer(tup)
    monkeypatch.setattr(ex, "get_shared_memory_checkpointer", lambda: checkpointer)
    return checkpointer


# ---------------------------------------------------------------------------
# 사람 확인 대기 판정
# ---------------------------------------------------------------------------

def test_재개_대기_중인_사람_확인이_있으면_대기_상태다():
    assert ex._pending_interrupt(_FakeTuple([("task", "__interrupt__", object())]))


def test_다른_pending_write_는_사람_확인_대기가_아니다():
    assert not ex._pending_interrupt(_FakeTuple([("task", "messages", object())]))
    assert not ex._pending_interrupt(_FakeTuple([]))
    assert not ex._pending_interrupt(None)


async def test_사람_확인_대기_중에는_방향을_바꿀_수_없다(monkeypatch, no_postgres):
    """대기 중에는 돌고 있는 실행이 없다 — 그 질문에 답하는 것이 방향 전환이다."""
    _use_checkpointer(monkeypatch, _FakeTuple([("task", "__interrupt__", object())]))

    result = await DeepAgentExecutor().steer(
        SteerDirective(conversation_id="room-1", message="이렇게 바꿔", turn_active=True)
    )

    assert result is not None
    assert not result.accepted
    assert result.reason == REASON_AWAITING_HUMAN_INPUT
    assert result.status_code == 409
    # 사용자에게 무엇을 하면 되는지 알려준다.
    assert "답" in result.detail


async def test_평소에는_SDK_의_표준_판정에_맡긴다(monkeypatch, no_postgres):
    checkpointer = _use_checkpointer(monkeypatch, _FakeTuple([]))

    result = await DeepAgentExecutor().steer(
        SteerDirective(conversation_id="room-1", message="이렇게 바꿔", turn_active=True)
    )

    assert result is None                      # None = 표준 판정 그대로
    assert checkpointer.asked == [{"configurable": {"thread_id": "room-1"}}]


async def test_체크포인트_조회가_실패해도_거절하지_않는다(monkeypatch, no_postgres):
    """판정을 못 했다고 사용자의 수정 지시를 막으면, 고칠 방법이 사라진다."""

    class _Broken:
        async def aget_tuple(self, config):
            raise RuntimeError("체크포인트 저장소 장애")

    monkeypatch.setattr(ex, "get_shared_memory_checkpointer", lambda: _Broken())

    assert await DeepAgentExecutor().steer(
        SteerDirective(conversation_id="room-1", message="이렇게 바꿔", turn_active=True)
    ) is None


async def test_방_id_가_없으면_대기_판정을_건너뛴다(monkeypatch, no_postgres):
    checkpointer = _use_checkpointer(monkeypatch, _FakeTuple([("t", "__interrupt__", 1)]))
    assert await ex._awaiting_human_input("") is False
    assert checkpointer.asked == []


async def test_질문을_내보낸_직후에는_체크포인트를_보지_않고도_대기로_본다(monkeypatch, no_postgres):
    """질문 직후 몇 백 ms 는 interrupt 가 저장소에 아직 안 보인다.

    그 사이에 온 수정 지시가 "사람 확인 대기" 대신 "진행 중인 턴 없음" 으로 거절되면,
    화면은 사용자에게 무엇을 하면 되는지 알려줄 수 없다.
    """
    checkpointer = _use_checkpointer(monkeypatch, _FakeTuple([]))      # 아직 안 보이는 상태
    ex._mark_awaiting_human("room-1")

    result = await DeepAgentExecutor().steer(
        SteerDirective(conversation_id="room-1", message="이렇게 바꿔", turn_active=False)
    )

    assert result is not None and result.reason == REASON_AWAITING_HUMAN_INPUT
    # 표식만으로 판정했으므로 저장소를 볼 필요도 없다.
    assert checkpointer.asked == []


async def test_새_턴이_시작되면_대기_표식이_사라진다(monkeypatch, no_postgres):
    _use_checkpointer(monkeypatch, _FakeTuple([]))
    ex._mark_awaiting_human("room-1")
    ex._clear_awaiting_human("room-1")

    assert await ex._awaiting_human_input("room-1") is False


async def test_다른_방의_대기는_이_방에_영향이_없다(monkeypatch, no_postgres):
    _use_checkpointer(monkeypatch, _FakeTuple([]))
    ex._mark_awaiting_human("room-other")

    assert await ex._awaiting_human_input("room-1") is False
    assert await ex._awaiting_human_input("room-other") is True


# ---------------------------------------------------------------------------
# 실행 중에 지시를 얹는 미들웨어
# ---------------------------------------------------------------------------

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from core.agents.steering import SteeringMiddleware, steering_message  # noqa: E402
from processgpt_agent_sdk.steering import SteeringInbox, get_steering_inbox, set_steering_inbox  # noqa: E402


@pytest.fixture
def inbox():
    box = SteeringInbox()
    set_steering_inbox(box)
    yield box
    set_steering_inbox(SteeringInbox())


def _state(*messages):
    return {"messages": list(messages)}


def _directive(message, cid="room-1"):
    return SteerDirective(conversation_id=cid, message=message)


# -- 모델 호출 직전 ----------------------------------------------------------

async def test_대기_중인_지시를_다음_모델_호출에_얹는다(inbox):
    await inbox.accept(_directive("표 대신 글로 써 줘"))

    update = await SteeringMiddleware("room-1").abefore_model(_state(), None)

    assert update is not None
    message = update["messages"][0]
    assert isinstance(message, HumanMessage)
    assert "표 대신 글로 써 줘" in message.content
    # 집어 갔으므로 다음 호출에 또 얹히지 않는다.
    assert inbox.pending("room-1") == []


async def test_대기_중인_지시가_없으면_아무_것도_하지_않는다(inbox):
    assert await SteeringMiddleware("room-1").abefore_model(_state(), None) is None


async def test_다른_방의_지시는_얹지_않는다(inbox):
    await inbox.accept(_directive("저쪽 방 지시", cid="room-2"))
    assert await SteeringMiddleware("room-1").abefore_model(_state(), None) is None
    assert [d.message for d in inbox.pending("room-2")] == ["저쪽 방 지시"]


async def test_접수가_아니라_얹는_순간에_반영을_알린다(inbox, monkeypatch):
    applied = []

    async def fake_mark_applied(directive):
        applied.append(directive.message)
        return True

    monkeypatch.setattr("core.agents.steering.mark_applied", fake_mark_applied)
    await inbox.accept(_directive("방향을 바꿔"))

    assert applied == []                      # 접수만으로는 반영이 아니다
    await SteeringMiddleware("room-1").abefore_model(_state(), None)
    assert applied == ["방향을 바꿔"]


async def test_닫혔던_대기열도_실행이_이어지면_다시_연다(inbox):
    """다른 미들웨어가 모델로 되돌린 경우 — 그 뒤에도 방향을 바꿀 수 있어야 한다."""
    await inbox.close("room-1")
    await SteeringMiddleware("room-1").abefore_model(_state(), None)
    assert inbox.is_closed("room-1") is False


# -- 턴을 끝내려 할 때 -------------------------------------------------------

async def test_끝내려는_순간에_남은_지시가_있으면_모델로_되돌린다(inbox):
    """이 갈래가 없으면 완료 직전에 접수된 지시는 영영 반영되지 않는다."""
    await inbox.accept(_directive("마지막에 들어온 지시"))
    state = _state(AIMessage(content="다 했습니다"))

    update = await SteeringMiddleware("room-1").aafter_model(state, None)

    assert update["jump_to"] == "model"
    assert "마지막에 들어온 지시" in update["messages"][0].content


async def test_남은_지시가_없으면_대기열을_닫는다(inbox):
    """더 얹을 지점이 없으므로, 이후 접수는 SDK 가 거절해야 한다."""
    state = _state(AIMessage(content="다 했습니다"))

    assert await SteeringMiddleware("room-1").aafter_model(state, None) is None
    assert inbox.is_closed("room-1") is True


def test_모델로_되돌리는_훅이_조건부_엣지를_선언한다():
    """`can_jump_to` 선언이 없으면 langchain 이 `jump_to` 를 조용히 무시한다.

    그러면 지시를 집어 가 놓고 반영은 되지 않는 최악의 결과가 된다 — 실제로 그랬다.
    훅을 직접 부르는 테스트로는 잡히지 않으므로 선언 자체를 확인한다.
    """
    assert getattr(SteeringMiddleware.aafter_model, "__can_jump_to__", None) == ["model"]


async def test_아직_도구를_부르는_중이면_끝내는_것이_아니다(inbox):
    state = _state(AIMessage(content="", tool_calls=[
        {"name": "write_file", "args": {}, "id": "call-1"},
    ]))

    assert await SteeringMiddleware("room-1").aafter_model(state, None) is None
    # 닫지 않는다 — 다음 모델 호출에서 얹을 수 있다.
    assert inbox.is_closed("room-1") is False


async def test_도구_결과로_끝난_상태는_닫지_않는다(inbox):
    state = _state(ToolMessage(content="결과", tool_call_id="call-1"))
    assert await SteeringMiddleware("room-1").aafter_model(state, None) is None
    assert inbox.is_closed("room-1") is False


async def test_방_id_가_없으면_아무_것도_하지_않는다(inbox):
    await inbox.accept(_directive("지시"))
    middleware = SteeringMiddleware("")
    assert await middleware.abefore_model(_state(), None) is None
    assert await middleware.aafter_model(_state(AIMessage(content="끝")), None) is None
    assert [d.message for d in inbox.pending("room-1")] == ["지시"]


async def test_반영_시점에_실행_쪽에_알린다(inbox):
    """그 전까지 쓰던 본문을 버리게 하는 통지다.

    남겨 두면 최종 답변이 "버려진 초안 + 새 답변" 이 되어, 고쳐 달라고 했더니 같은
    말을 두 번 한 것처럼 보인다 — 사용자는 기능이 실패한 것으로 읽는다.
    """
    called = []
    await inbox.accept(_directive("방향을 바꿔"))

    middleware = SteeringMiddleware("room-1", on_applied=lambda: called.append(True))
    await middleware.abefore_model(_state(), None)

    assert called == [True]


async def test_알림이_실패해도_반영은_계속된다(inbox):
    await inbox.accept(_directive("방향을 바꿔"))

    def boom():
        raise RuntimeError("실행 쪽 처리 실패")

    middleware = SteeringMiddleware("room-1", on_applied=boom)
    update = await middleware.abefore_model(_state(), None)

    assert "방향을 바꿔" in update["messages"][0].content


async def test_얹을_지시가_없으면_알리지_않는다(inbox):
    called = []
    middleware = SteeringMiddleware("room-1", on_applied=lambda: called.append(True))

    assert await middleware.abefore_model(_state(), None) is None
    assert called == []


# -- 모델에게 주는 문장 ------------------------------------------------------

def test_수정_지시_문장은_맥락_유지와_우선순위를_못박는다():
    text = steering_message([_directive("표 대신 글로 써 줘")])
    assert "표 대신 글로 써 줘" in text
    assert "맥락은 그대로" in text
    assert "이 지시를 따른다" in text
    # "알겠습니다" 로 끝내는 것을 막는다.
    assert "받았다는 말만 하고 끝내면 안 된다" in text


def test_여러_지시는_받은_순서대로_전달된다():
    text = steering_message([_directive("첫째 지시"), _directive("둘째 지시")])
    assert text.index("첫째 지시") < text.index("둘째 지시")


def test_빈_지시는_문장에_섞이지_않는다():
    text = steering_message([_directive("   "), _directive("실제 지시")])
    assert "실제 지시" in text


def test_어댑터가_SDK_표준_동작을_구현한다():
    from processgpt_agent_sdk import ProcessGPTAgentServer

    server = ProcessGPTAgentServer(
        agent_executor=DeepAgentExecutor(), agent_type="deepagents", tenant_auth=True,
    )
    assert server.supports_steering()
