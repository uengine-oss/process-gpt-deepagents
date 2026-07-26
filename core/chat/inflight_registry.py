"""채팅 turn 동시 실행 방지: chat_room_id(=LangGraph thread_id)별 in-flight task 추적.

프론트가 로딩 중 새 메시지를 보내면 자기 쪽 fetch만 끊고 새 요청을 보내며,
서버에는 취소 신호를 주지 않는다. 이 레지스트리가 없으면 같은 thread_id에
대해 두 개의 graph.astream() 실행이 동시에 체크포인트를 쓰는 레이스가 발생한다.

checkpointer.py의 "프로세스 전역 공유 상태" 패턴을 그대로 따른다 — asyncio는
await 지점 사이에서만 컨텍스트 스위치하므로 별도 락이 필요 없다. 단일 uvicorn
워커 가정(checkpointer.py의 InMemorySaver와 동일한 전제)이며, 다중 워커 배포는
스코프 밖이다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_INFLIGHT: dict[str, tuple[asyncio.Task, Any]] = {}


def get_inflight(thread_id: str) -> tuple[asyncio.Task, Any] | None:
    """thread_id에 대해 아직 끝나지 않은 in-flight (task, event_queue)를 반환한다."""
    entry = _INFLIGHT.get(thread_id)
    if entry is not None and entry[0].done():
        # done-callback이 아직 안 돌았어도 done()이면 in-flight 아님으로 취급.
        return None
    return entry


def set_inflight(thread_id: str, task: asyncio.Task, event_queue: Any) -> None:
    _INFLIGHT[thread_id] = (task, event_queue)


def clear_inflight_if_current(thread_id: str, task: asyncio.Task) -> None:
    """thread_id의 등록된 task가 여전히 이 task일 때만 제거한다.

    (오래된 task의 done-callback이 늦게 돌아 이미 등록된 새 task를 지우는 것을 방지)
    """
    entry = _INFLIGHT.get(thread_id)
    if entry is not None and entry[0] is task:
        _INFLIGHT.pop(thread_id, None)


async def supersede_previous_turn(thread_id: str) -> None:
    """같은 thread_id에 아직 끝나지 않은 turn이 있으면 취소하고 끝날 때까지 기다린다.

    프론트는 로딩 중 새 메시지를 보낼 때 자기 쪽 fetch만 끊고 서버에는 취소 신호를
    주지 않는다. 이걸 막지 않으면 같은 thread_id에 대해 graph.astream() 두 개가
    동시에 체크포인트를 쓰는 레이스가 생긴다.
    """
    prev = get_inflight(thread_id)
    if prev is None:
        return
    prev_task, prev_queue = prev
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
        await asyncio.wait_for(prev_task, timeout=10)
    except asyncio.CancelledError:
        pass  # 우리가 취소했으니 예상된 결과
    except asyncio.TimeoutError:
        logger.warning("이전 turn 취소 대기 시간 초과 | room_id=%s (계속 진행)", thread_id)
    except Exception:
        logger.exception("이전 turn 종료 중 오류(무시하고 진행)")
