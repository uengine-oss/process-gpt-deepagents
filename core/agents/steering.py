"""core/agents/steering.py — 진행 중인 실행에 수정 지시를 얹는 미들웨어.

## 무엇을 하나

사용자가 작업 도중에 방향을 바꾸면, SDK 가 그 지시를 대화별 대기열에 넣어 둔다
(`processgpt_agent_sdk.steering`). 이 미들웨어는 **진행 중인 실행 안에서** 그 대기열을
읽어 다음 모델 호출의 입력에 얹는다. 실행을 취소하거나 다시 시작하지 않으므로,
모델이 세워 둔 계획과 지금까지의 맥락이 그대로 남는다.

## 왜 하필 모델 호출 직전인가

`abefore_model` 은 모델 노드 바로 앞에서만 실행된다. 그래서

  - **도구를 중간에 끊지 않는다.** 도구가 도는 동안에는 이 훅이 불리지 않고, 그
    도구가 끝난 직후에 불린다. 반영이 늦어지는 만큼이 곧 남은 도구 시간이다.
  - **결과 없는 도구 호출이 생기지 않는다.** 도구 호출과 그 결과가 반드시 짝지어진
    지점이라, 정합성을 따로 지킬 필요가 없다. 짝이 깨진 채로 다음 호출을 하면 모델
    API 자체가 그 대화를 거절한다.

## 턴이 끝나려 할 때

모델이 도구를 부르지 않고 답을 내놓으면 그 턴은 끝난다. 그 뒤에는 이 훅이 다시
불리지 않으므로, **마지막 순간에 접수된 지시는 영영 반영되지 않는다.** 받아 두고
아무 일도 하지 않는 것이 제일 나쁘므로, `aafter_model` 에서 한 번 더 확인해
남은 지시가 있으면 모델 노드로 되돌린다(`jump_to`). 남은 것이 없으면 그때 대기열을
닫는다 — 이후의 접수 요청은 SDK 가 "진행 중인 턴 없음" 으로 거절한다.

## 하위 에이전트

이 미들웨어는 루트에만 붙는다(하위 에이전트는 자기 spec 의 미들웨어만 쓴다).
하위 에이전트가 도는 동안 접수된 지시는 제어가 루트로 돌아온 뒤 반영된다 — 늦어질
뿐 유실되지는 않는다. 한 지시가 두 곳에서 따로 해석되는 것보다 낫다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage
from processgpt_agent_sdk.steering import (
    SteerDirective,
    get_steering_inbox,
    mark_applied,
)

logger = logging.getLogger(__name__)


def steering_message(directives: list[SteerDirective]) -> str:
    """집어 온 수정 지시를 모델에게 줄 한 덩어리로 만든다.

    "받았다" 고만 답하고 끝내는 것을 막는 문장이 필요하다 — 사용자는 방향이 바뀐
    결과를 원하지, 바꾸겠다는 대답을 원하는 게 아니다.
    """
    body = "\n\n".join(d.message.strip() for d in directives if d.message.strip())
    return (
        "[작업 중에 사용자가 보낸 수정 지시]\n"
        f"{body}\n\n"
        "지금까지 해 온 작업과 맥락은 그대로 두고, 지금부터는 이 지시를 반영하라. "
        "이전 지시와 어긋나는 부분은 이 지시를 따른다. 이미 만든 결과가 이 지시에 "
        "맞지 않으면 고쳐서 내보내라. 이 지시를 받았다는 말만 하고 끝내면 안 된다."
    )


def _ending_turn(state: Any) -> bool:
    """모델이 도구를 부르지 않고 답을 내놨는가(= 이 턴을 끝내려 하는가)."""
    messages = list((state or {}).get("messages") or [])
    if not messages:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and not getattr(last, "tool_calls", None)


class SteeringMiddleware(AgentMiddleware):
    """대화별 대기열의 수정 지시를 다음 모델 판단에 얹는다."""

    def __init__(
        self, conversation_id: str, on_applied: Callable[[], None] | None = None
    ) -> None:
        super().__init__()
        self._conversation_id = (conversation_id or "").strip()
        # 지시가 실제로 얹히는 순간 실행 쪽에 알린다. 실행은 이 시점에 **지시가 바뀌기
        # 전까지 쓰던 본문을 버린다** — 사용자가 거둬들인 지시로 만든 글이라, 남겨 두면
        # 버려진 초안 뒤에 새 답변이 이어붙어 같은 말을 두 번 한 것처럼 보인다.
        self._on_applied = on_applied

    # -- 정상 경로 --------------------------------------------------------

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        cid = self._conversation_id
        if not cid:
            return None
        inbox = get_steering_inbox()
        # 마무리로 판단해 닫아 뒀는데 실행이 이어진 경우(다른 미들웨어가 모델로
        # 되돌린 경우 등) 다시 받을 수 있게 연다.
        await inbox.reopen(cid)
        taken = await inbox.take(cid)
        if not taken:
            return None
        return {"messages": [HumanMessage(content=await self._apply(taken))]}

    # -- 턴이 끝나려 할 때 -------------------------------------------------

    # `can_jump_to` 가 없으면 langchain 이 이 미들웨어 노드에 조건부 엣지 대신 고정 엣지를
    # 깔아버려서 `jump_to` 를 조용히 무시한다 — 지시를 집어 가 놓고 반영은 안 되는,
    # 제일 나쁜 결과가 된다(실제로 그랬다). 이 데코레이터가 있어야 모델로 되돌아간다.
    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        cid = self._conversation_id
        if not cid or not _ending_turn(state):
            return None

        inbox = get_steering_inbox()
        taken = await inbox.take(cid)
        if not taken:
            # 더 이상 지시를 얹을 지점이 없다. 여기서 닫아야 이후의 접수 요청이
            # "받았다" 고만 답하고 아무 데도 반영되지 않는 일이 생기지 않는다.
            await inbox.close(cid)
            return None

        logger.info(
            "steering: 턴을 끝내려는 시점에 수정 지시 %d건을 반영한다 | room=%s",
            len(taken), cid,
        )
        return {"messages": [HumanMessage(content=await self._apply(taken))], "jump_to": "model"}

    # -- 내부 -------------------------------------------------------------

    async def _apply(self, directives: list[SteerDirective]) -> str:
        """반영을 알리고 모델에게 줄 문장을 만든다.

        반영 이벤트는 지시가 **실제로 다음 판단에 들어가는** 지금 나간다. 접수
        시점에 내보내면 화면은 아직 바뀌지 않은 결과를 바뀐 것으로 표시한다.
        """
        for directive in directives:
            if not await mark_applied(directive):
                # 반영은 했는데 화면에 알리지 못한 상태다. 사용자에게는 지시가
                # 사라진 것처럼 보이므로, 조용히 넘기지 않고 로그로 남긴다.
                logger.warning(
                    "steering: 반영 이벤트를 전달하지 못했다(스트림 없음) | room=%s directive=%s",
                    self._conversation_id, directive.directive_id,
                )
        if self._on_applied is not None:
            try:
                self._on_applied()
            except Exception:
                logger.warning(
                    "steering: 반영 알림 처리 실패(무시) | room=%s",
                    self._conversation_id, exc_info=True,
                )
        logger.info(
            "steering: 수정 지시 %d건 반영 | room=%s", len(directives), self._conversation_id
        )
        return steering_message(directives)
