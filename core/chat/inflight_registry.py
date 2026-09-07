"""채팅 turn 동시 실행 방지: chat_room_id(=LangGraph thread_id)별 in-flight task 추적.

프론트가 로딩 중 새 메시지를 보내면 자기 쪽 fetch만 끊고 새 요청을 보내며,
서버에는 취소 신호를 주지 않는다. 이 레지스트리가 없으면 같은 thread_id에
대해 두 개의 graph.astream() 실행이 동시에 체크포인트를 쓰는 레이스가 발생한다.

실제 asyncio.Task.cancel()은 그 Task를 만든 프로세스 안에서만 가능하다 — 다른
파드가 Redis에 뭘 쓴다고 남의 파드 메모리 속 Task를 직접 취소할 수는 없다.
그래서 취소 자체는 항상 로컬(같은 파드)에서 일어나고, REDIS_URL 이 설정돼
있으면 Redis는 오직 (a) "이 thread_id는 어느 파드가 갖고 있는지" 소유권 기록과
(b) "취소해줘"/"끝났어" 신호 전달용으로만 쓴다:

  - 소유 파드는 set_inflight_and_claim() 으로 로컬 dict + Redis 소유권 키를 같이 기록.
  - 다른 파드가 supersede_previous_turn() 을 호출했는데 로컬엔 없고 Redis에 다른 파드가
    소유자로 남아있으면, cancel 채널에 발행하고 turn-done 채널을 기다린다.
  - 소유 파드는 start_cancel_listener() 백그라운드 태스크로 cancel 채널을 구독하다가,
    자기 로컬 dict에 있는 thread_id면 지금과 동일한 인프로세스 취소 로직을 수행한다.
  - 취소된 task의 기존 done-callback(clear_inflight_and_release)이 로컬 dict 정리와
    Redis 소유권 해제 + turn-done 발행을 함께 처리한다 — 로컬/원격 트리거 모두 동일 경로.

REDIS_URL 이 없으면 전부 지금까지와 동일한 순수 로컬 dict 동작으로 폴백한다
(단일 uvicorn 워커 전제, 로컬 개발 환경 변경 없음).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.storage.redis_client import get_pod_id, get_redis

logger = logging.getLogger(__name__)

_INFLIGHT: dict[str, tuple[asyncio.Task, Any]] = {}

_OWNER_TTL_SECONDS = 600  # 소유 파드가 죽었을 때의 안전장치(채팅 턴은 보통 이보다 훨씬 짧게 끝남)
_REMOTE_WAIT_TIMEOUT_SECONDS = 10  # 기존 로컬 취소 대기(asyncio.wait_for timeout=10)와 동일하게 맞춤
# HITL 응답이 이전 turn 을 기다리는 시간. 프론트는 request_human_input **도구 호출 이벤트**
# 시점에 이미 패널을 그리므로, 사용자가 곧바로 답하면 이전 turn 은 아직 LLM/도구를 돌리는
# 중일 수 있다. 그 turn 이 interrupt 를 체크포인트에 남겨야 재개가 되므로 넉넉히 기다린다
# (10초는 원격 pubsub 대기용 값이라 LLM turn 기준으로는 너무 짧다).
_HITL_WAIT_TIMEOUT_SECONDS = 180


def _await_chain(task: asyncio.Task, limit: int = 40) -> str:
    """task 가 지금 매달려 있는 await 체인을 `파일:줄 함수` 목록으로 만든다.

    `Task.get_stack()`/`print_stack()` 은 최상위 코루틴 프레임만 준다 — 우리가 알고 싶은
    "실제로 어디서 멈췄나"(graph.astream 인지, DB 저장인지, to_thread 인지)는 `cr_await`
    체인을 직접 따라가야 보인다.
    """
    lines: list[str] = []
    seen: set[int] = set()
    node: Any = task.get_coro()
    while node is not None and len(lines) < limit and id(node) not in seen:
        seen.add(id(node))
        frame = getattr(node, "cr_frame", None) or getattr(node, "ag_frame", None) or getattr(node, "gi_frame", None)
        if frame is not None:
            code = frame.f_code
            lines.append(f"  {code.co_filename}:{frame.f_lineno} in {code.co_name}")
        nxt = (
            getattr(node, "cr_await", None)
            or getattr(node, "ag_await", None)
            or getattr(node, "gi_yieldfrom", None)
        )
        if nxt is None and isinstance(node, asyncio.Future):
            lines.append(f"  <awaiting {type(node).__name__}>")
        node = nxt
    return "\n".join(lines)


def _log_stuck_task(thread_id: str, task: asyncio.Task, *, reason: str) -> None:
    """끝나지 않는 turn 이 지금 무엇을 await 중인지 스택으로 남긴다.

    한 번 등록된 뒤 영영 끝나지 않는 task 가 있으면 이후 모든 turn 이 대기 → 취소를
    반복하며 HITL 재개가 깨진다. 원인을 추정하지 않고 실제 프레임을 남겨 바로 짚는다.
    """
    try:
        dump = _await_chain(task) or "(no frames — task not suspended in Python code)"
        logger.warning(
            "끝나지 않는 turn 스택 | room_id=%s reason=%s done=%s cancelled=%s\n%s",
            thread_id, reason, task.done(), task.cancelled(), dump,
        )
    except Exception:
        logger.warning("끝나지 않는 turn 스택 수집 실패 | room_id=%s", thread_id, exc_info=True)


def _owner_key(thread_id: str) -> str:
    return f"chat:inflight:{thread_id}"


def _cancel_channel(thread_id: str) -> str:
    return f"chat:cancel:{thread_id}"


def _done_channel(thread_id: str) -> str:
    return f"chat:turn-done:{thread_id}"


def get_inflight(thread_id: str) -> tuple[asyncio.Task, Any] | None:
    """thread_id에 대해 아직 끝나지 않은 in-flight (task, event_queue)를 반환한다(로컬 전용)."""
    entry = _INFLIGHT.get(thread_id)
    if entry is not None and entry[0].done():
        # done-callback이 아직 안 돌았어도 done()이면 in-flight 아님으로 취급.
        return None
    return entry


def set_inflight(thread_id: str, task: asyncio.Task, event_queue: Any) -> None:
    _INFLIGHT[thread_id] = (task, event_queue)


def clear_inflight_if_current(thread_id: str, task: asyncio.Task) -> None:
    """thread_id의 등록된 task가 여전히 이 task일 때만 제거한다(로컬 전용).

    (오래된 task의 done-callback이 늦게 돌아 이미 등록된 새 task를 지우는 것을 방지)
    """
    entry = _INFLIGHT.get(thread_id)
    if entry is not None and entry[0] is task:
        _INFLIGHT.pop(thread_id, None)


async def set_inflight_and_claim(thread_id: str, task: asyncio.Task, event_queue: Any) -> None:
    """로컬 등록 + (Redis 설정 시) 다른 파드가 볼 수 있도록 소유권 기록."""
    set_inflight(thread_id, task, event_queue)
    r = get_redis()
    if r is None:
        return
    try:
        await r.set(_owner_key(thread_id), get_pod_id(), ex=_OWNER_TTL_SECONDS)
    except Exception:
        logger.warning("inflight_registry(redis): 소유권 기록 실패 | thread_id=%s", thread_id, exc_info=True)


async def clear_inflight_and_release(thread_id: str, task: asyncio.Task) -> None:
    """로컬 정리 + (Redis 설정 시) 소유권 해제 및 대기 중인 다른 파드에 완료 알림."""
    entry = _INFLIGHT.get(thread_id)
    was_current = entry is not None and entry[0] is task
    clear_inflight_if_current(thread_id, task)
    if not was_current:
        return
    r = get_redis()
    if r is None:
        return
    try:
        await r.delete(_owner_key(thread_id))
        await r.publish(_done_channel(thread_id), "done")
    except Exception:
        logger.warning("inflight_registry(redis): 소유권 해제 실패 | thread_id=%s", thread_id, exc_info=True)


async def _cancel_and_wait(thread_id: str, prev_task: asyncio.Task, prev_queue: Any) -> None:
    """실행 중인 이전 turn(같은 파드)을 취소하고 끝날 때까지 기다린다."""
    logger.info("이전 turn을 새 메시지로 대체 — 취소함 | room_id=%s", thread_id)

    # SSE 스트림을 정식 a2a 이벤트 경로(TaskArtifactUpdateEvent)로 닫으면 ChatEventQueue.
    # _finalize()가 돌아 chats 테이블에 취소된 턴이 정상 응답인 것처럼 저장돼 버린다.
    # 그래서 ChatEventQueue가 감싸고 있는 raw out_queue에 직접 써서 그 경로를 우회한다.
    # private 속성이라 향후 SDK 내부 변경에 대비해 guard한다(실패해도 취소 자체는 진행).
    out_q = getattr(prev_queue, "_out_queue", None)
    if out_q is not None:
        try:
            out_q.put_nowait(
                {
                    "event": "message",
                    "data": {
                        "type": "error",
                        "error": "새 메시지로 대체되어 이전 응답이 취소되었습니다.",
                    },
                }
            )
        except Exception:
            logger.warning("이전 turn SSE 종료 이벤트 전송 실패(무시)", exc_info=True)

    prev_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(prev_task), timeout=_REMOTE_WAIT_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        pass  # 우리가 취소했으니 예상된 결과
    except asyncio.TimeoutError:
        # 취소에도 응답하지 않는 turn 이다(예: asyncio.to_thread 안에서 블로킹 중 — 스레드는
        # 취소되지 않는다). 그대로 두면 _INFLIGHT 에 좀비로 남아 이후 모든 turn 이 여기서
        # 대기했다가 다시 취소를 시도한다. 스택을 남기고 레지스트리에서 걷어낸다.
        logger.warning("이전 turn 취소 대기 시간 초과 | room_id=%s (좀비로 간주하고 해제)", thread_id)
        _log_stuck_task(thread_id, prev_task, reason="cancel_timeout")
        clear_inflight_if_current(thread_id, prev_task)
    except Exception:
        logger.exception("이전 turn 종료 중 오류(무시하고 진행)")


async def _wait_for_previous_turn(thread_id: str, prev_task: asyncio.Task) -> bool:
    """이전 turn을 **취소하지 않고** 끝날 때까지 기다린다. 끝났으면 True.

    HITL 응답(승인/후보 선택)으로 들어온 turn 에 쓴다. 프론트는 백엔드가 interrupt 를
    다 처리하기 전(도구 호출 이벤트 시점)에 이미 HITL 패널을 그리므로, 사용자가 곧바로
    답하면 이전 turn 이 아직 실행 중일 수 있다. 이때 이전 turn 을 취소하면
    (a) interrupt 체크포인트가 남지 않아 재개가 불가능해지고
    (b) "새 메시지로 대체되어 이전 응답이 취소되었습니다" 오류가 화면에 뜬다.
    답변은 그 turn 을 이어가려는 것이므로 취소가 아니라 완주를 기다리는 게 맞다.
    """
    logger.info(
        "HITL 응답 — 이전 turn 완료 대기(취소하지 않음) | room_id=%s timeout=%ss",
        thread_id, _HITL_WAIT_TIMEOUT_SECONDS,
    )
    try:
        await asyncio.wait_for(asyncio.shield(prev_task), timeout=_HITL_WAIT_TIMEOUT_SECONDS)
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "이전 turn 완료 대기 시간 초과(%ss) | room_id=%s", _HITL_WAIT_TIMEOUT_SECONDS, thread_id,
        )
        _log_stuck_task(thread_id, prev_task, reason="hitl_wait_timeout")
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        # 이전 turn 이 예외로 끝난 것뿐이다 — 이 turn 은 그대로 진행한다.
        return True


async def _wait_for_remote_turn_done(thread_id: str, owner: str, *, request_cancel: bool = True) -> None:
    """다른 파드(owner)가 소유한 turn의 완료를 기다린다(필요하면 취소 요청도 함).

    `request_cancel=False` 는 HITL 응답 turn 용이다 — 이어갈 turn 을 취소하면 안 된다.
    """
    r = get_redis()
    logger.info(
        "이전 turn 대기(다른 파드) | room_id=%s owner=%s cancel=%s",
        thread_id, owner, request_cancel,
    )
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe(_done_channel(thread_id))
        if request_cancel:
            await r.publish(_cancel_channel(thread_id), get_pod_id())
        # get_message(ignore_subscribe_messages=True)는 첫 호출이 subscribe 확인 메시지를
        # 걸러내는 것뿐이어도 그 자리에서 None을 반환한다(다음 메시지까지 자동으로 기다려
        # 주지 않음) — 그래서 실제 turn-done 메시지가 올 때까지 데드라인 안에서 직접 반복한다.
        deadline = asyncio.get_event_loop().time() + _REMOTE_WAIT_TIMEOUT_SECONDS
        got_message = False
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if msg is not None:
                got_message = True
                break
        if not got_message:
            logger.warning("이전 turn(다른 파드) 취소 대기 시간 초과 | room_id=%s (계속 진행)", thread_id)
    except Exception:
        logger.warning("inflight_registry(redis): 원격 turn 취소 대기 실패 | thread_id=%s", thread_id, exc_info=True)
    finally:
        try:
            await pubsub.unsubscribe(_done_channel(thread_id))
            await pubsub.close()
        except Exception:
            pass


async def supersede_previous_turn(thread_id: str, *, cancel: bool = True) -> None:
    """같은 thread_id에 아직 끝나지 않은 turn이 있으면 정리하고 끝날 때까지 기다린다.

    프론트는 로딩 중 새 메시지를 보낼 때 자기 쪽 fetch만 끊고 서버에는 취소 신호를
    주지 않는다. 이걸 막지 않으면 같은 thread_id에 대해 graph.astream() 두 개가
    동시에 체크포인트를 쓰는 레이스가 생긴다.

    Args:
        cancel: 기본 True — 새 요청이 이전 응답을 **대체**하므로 취소한다.
            HITL 응답 turn 은 이전 turn 을 대체하는 게 아니라 **이어가는** 것이므로
            False 로 호출해 취소 없이 완료만 기다린다(취소하면 interrupt 체크포인트가
            사라져 재개가 불가능해지고 사용자에겐 취소 오류만 남는다).
            대기 시간을 넘기면 레이스 방지를 위해 그때는 취소로 폴백한다.
    """
    prev = get_inflight(thread_id)
    if prev is not None:
        prev_task, prev_queue = prev
        if not cancel:
            if await _wait_for_previous_turn(thread_id, prev_task):
                return
            # 시간 내에 안 끝났다 — 동시 실행 레이스를 막기 위해 결국 취소한다.
        await _cancel_and_wait(thread_id, prev_task, prev_queue)
        return

    r = get_redis()
    if r is None:
        return
    try:
        owner = await r.get(_owner_key(thread_id))
    except Exception:
        logger.warning("inflight_registry(redis): 소유권 조회 실패 | thread_id=%s", thread_id, exc_info=True)
        return
    if not owner:
        return
    await _wait_for_remote_turn_done(thread_id, owner, request_cancel=cancel)


async def _handle_remote_cancel(thread_id: str) -> None:
    entry = get_inflight(thread_id)
    if entry is None:
        return
    prev_task, prev_queue = entry
    await _cancel_and_wait(thread_id, prev_task, prev_queue)


async def start_cancel_listener() -> None:
    """파드 시작 시 1회 호출: 다른 파드가 보낸 취소 요청을 구독해 로컬 turn을 취소한다.

    REDIS_URL 미설정이면 즉시 반환(단일 파드 동작에서는 불필요).
    """
    r = get_redis()
    if r is None:
        return
    pubsub = r.pubsub()
    await pubsub.psubscribe("chat:cancel:*")
    logger.info("inflight_registry(redis): cancel listener 시작")
    try:
        async for message in pubsub.listen():
            if not isinstance(message, dict) or message.get("type") != "pmessage":
                continue
            channel = str(message.get("channel") or "")
            prefix = "chat:cancel:"
            if not channel.startswith(prefix):
                continue
            thread_id = channel[len(prefix):]
            if thread_id:
                asyncio.create_task(_handle_remote_cancel(thread_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inflight_registry(redis): cancel listener 오류 종료")
    finally:
        try:
            await pubsub.punsubscribe("chat:cancel:*")
            await pubsub.close()
        except Exception:
            pass
