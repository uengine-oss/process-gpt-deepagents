"""A2A TaskStatusUpdateEvent / TaskArtifactUpdateEvent helpers."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from typing import Any

from a2a.helpers import new_text_artifact_update_event, new_text_status_update_event
from a2a.server.events import EventQueue
from a2a.types import TaskState
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct

from .context import ExecutionContext, build_event_payload

logger = logging.getLogger(__name__)

# Bound in DeepAgentsExecutor.execute while the graph runs. Tooling may call
# EventQueue.enqueue_event from sync worker threads (no running loop); we then
# submit the coroutine onto this loop via run_coroutine_threadsafe.
_executor_asyncio_loop: contextvars.ContextVar[asyncio.AbstractEventLoop | None] = contextvars.ContextVar(
    "executor_asyncio_loop_for_a2a_enqueue",
    default=None,
)


def bind_executor_asyncio_loop_for_enqueue(loop: asyncio.AbstractEventLoop) -> contextvars.Token[asyncio.AbstractEventLoop | None]:
    return _executor_asyncio_loop.set(loop)


def reset_executor_asyncio_loop_for_enqueue(token: contextvars.Token[asyncio.AbstractEventLoop | None]) -> None:
    _executor_asyncio_loop.reset(token)


def schedule_async_event_queue_coroutine(coro: Any) -> None:
    """Schedule ``EventQueue.enqueue_event`` coroutine from sync tool threads or async contexts."""
    if not asyncio.iscoroutine(coro):
        return
    try:
        asyncio.get_running_loop().create_task(coro)
        return
    except RuntimeError:
        pass
    outer = _executor_asyncio_loop.get()
    if outer is not None and outer.is_running():
        try:
            asyncio.run_coroutine_threadsafe(coro, outer)
        except Exception:
            logger.exception("Failed to schedule async enqueue_event on executor loop", exc_info=True)
        return
    logger.warning(
        "Dropping async enqueue_event coroutine: no running loop and no executor loop bound | coro=%r",
        coro,
    )
    try:
        coro.close()
    except Exception:
        pass


def enqueue_event(eq: Any, event: Any) -> None:
    """Call enqueue_event in both sync/async EventQueue implementations.

    - a2a-sdk>=1.0: enqueue_event is async
    - legacy / some adapters: enqueue_event may be sync
    """
    try:
        ret = eq.enqueue_event(event)
        if asyncio.iscoroutine(ret):
            schedule_async_event_queue_coroutine(ret)
    except Exception:
        logger.exception("Failed to enqueue event", exc_info=True)


def _ensure_required_root_task_started_fields(payload: dict[str, Any], *, event_type: str) -> None:
    """Depth 1 task_started payload must always include role/goal/agent_profile."""
    if event_type != "task_started":
        return
    if payload.get("depth") != 1:
        return

    agent_name = str(payload.get("agent_name") or "").strip()

    if not payload.get("role"):
        if agent_name == "작업 실행 기획자":
            payload["role"] = "작업 실행 기획자"
        elif agent_name == "최종 결과 작성자":
            payload["role"] = "최종 결과 작성자"
        else:
            payload["role"] = agent_name or "agent"

    if not payload.get("goal"):
        if agent_name == "작업 실행 기획자":
            payload["goal"] = str(payload.get("message") or "요청된 작업을 수행합니다.")
        elif agent_name == "최종 결과 작성자":
            payload["goal"] = str(payload.get("message") or "최종 응답을 작성합니다.")
        else:
            payload["goal"] = str(payload.get("message") or "작업을 시작합니다.")

    if not payload.get("agent_profile"):
        payload["agent_profile"] = "/images/chat-icon.png"


def new_status_update_event(
    event_type: str,
    data: dict[str, Any],
    proc_inst_id: str,
    task_id: str,
    crew_type: str,
    job_id: str | None = None,
    *,
    exec_ctx: ExecutionContext | None = None,
    logical_event_type: str | None = None,
    phase: str | None = None,
    agent_name: str | None = None,
    status: str | None = None,
) -> Any:
    """Create a TaskStatusUpdateEvent (A2A standard) but do not enqueue it yet."""
    effective_job_id = job_id or str(uuid.uuid4())
    logical = logical_event_type or ("agent_started" if event_type == "task_started" else "agent_finished")
    effective_phase = phase or "reasoning"

    payload = build_event_payload(
        exec_ctx,
        logical_event_type=logical,
        phase=effective_phase,
        agent_name=agent_name,
        extra_data=data,
    )
    _ensure_required_root_task_started_fields(payload, event_type=event_type)

    if event_type == "human_asked":
        state = TaskState.TASK_STATE_INPUT_REQUIRED
    elif (
        event_type == "task_completed"
        and effective_phase == "final"
        and crew_type == "result"
    ):
        state = TaskState.TASK_STATE_COMPLETED
    else:
        state = TaskState.TASK_STATE_WORKING

    metadata: dict[str, Any] = {
        "crew_type": crew_type,
        "event_type": event_type,
        "status": status,
        "job_id": effective_job_id,
    }
    metadata["payload"] = payload

    md = Struct()
    ParseDict(metadata, md)
    evt = new_text_status_update_event(
        task_id=str(task_id or ""),
        context_id=str(proc_inst_id or ""),
        state=state,
        text=json.dumps(payload, ensure_ascii=False),
    )
    evt.metadata.CopyFrom(md)
    return evt


def enqueue_status_update(
    eq: EventQueue,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Create and enqueue a TaskStatusUpdateEvent."""
    try:
        evt = new_status_update_event(*args, **kwargs)
        enqueue_event(eq, evt)
    except Exception:
        logger.exception("Failed to enqueue TaskStatusUpdateEvent", exc_info=True)


def new_artifact_update_event(
    name: str,
    description: str,
    text: Any,
    proc_inst_id: str,
    task_id: str,
    *,
    exec_ctx: ExecutionContext | None = None,
) -> Any:
    """Create a TaskArtifactUpdateEvent (A2A standard) but do not enqueue it yet."""
    del exec_ctx  # unused
    if not isinstance(text, str):
        artifact_text = json.dumps(text, ensure_ascii=False)
    else:
        artifact_text = text
    # a2a-sdk: new_text_artifact_update_event(task_id, context_id, name, text, ...)
    # (no ``description`` kwarg; keep parameter for call-site compatibility.)
    _ = description
    return new_text_artifact_update_event(
        task_id=str(task_id or ""),
        context_id=str(proc_inst_id or ""),
        name=name,
        text=artifact_text,
        last_chunk=True,
    )


def enqueue_artifact_update(
    eq: EventQueue,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Create and enqueue a TaskArtifactUpdateEvent."""
    try:
        evt = new_artifact_update_event(*args, **kwargs)
        enqueue_event(eq, evt)
    except Exception:
        logger.exception("Failed to enqueue TaskArtifactUpdateEvent", exc_info=True)

