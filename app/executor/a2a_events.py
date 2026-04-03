"""A2A TaskStatusUpdateEvent / TaskArtifactUpdateEvent helpers."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from a2a.server.events import EventQueue
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from a2a.utils import new_agent_text_message, new_text_artifact

from .context import ExecutionContext, build_event_payload

logger = logging.getLogger(__name__)


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


def _enqueue_status(
    eq: EventQueue,
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
) -> None:
    """Enqueue a TaskStatusUpdateEvent so that processgpt_agent_sdk handles persistence."""
    effective_job_id = job_id or str(uuid.uuid4())
    logical = logical_event_type or ("agent_started" if event_type == "task_started" else "agent_finished")
    effective_phase = phase or "reasoning"

    # 1) 우리가 원하는 평탄화 payload(메타+도메인)를 먼저 만든다.
    payload = build_event_payload(
        exec_ctx,
        logical_event_type=logical,
        phase=effective_phase,
        agent_name=agent_name,
        extra_data=data,
    )
    _ensure_required_root_task_started_fields(payload, event_type=event_type)

    # 2) a2a 메시지 객체 생성 (텍스트는 payload JSON)
    try:
        message = new_agent_text_message(
            json.dumps(payload, ensure_ascii=False),
            proc_inst_id,
            task_id,
        )
    except Exception:
        logger.exception("Failed to build agent text message for status event", exc_info=True)
        return

    # 3) TaskState 매핑
    if event_type == "human_asked":
        state = TaskState.input_required
        final_flag = True
    elif (
        event_type == "task_completed"
        and effective_phase == "final"
        and crew_type == "result"
    ):
        state = TaskState.completed
        final_flag = True
    else:
        state = TaskState.working
        final_flag = False

    metadata = {
        "crew_type": crew_type,
        "event_type": event_type,
        "status": status,
        "job_id": effective_job_id,
    }

    try:
        eq.enqueue_event(
            TaskStatusUpdateEvent(
                status={
                    "state": state,
                    "message": message,
                },
                final=final_flag,
                contextId=proc_inst_id,
                taskId=task_id,
                metadata=metadata,
            )
        )
    except Exception:
        logger.exception("Failed to enqueue TaskStatusUpdateEvent", exc_info=True)


def _enqueue_artifact(
    eq: EventQueue,
    name: str,
    description: str,
    text: Any,
    proc_inst_id: str,
    task_id: str,
    *,
    exec_ctx: ExecutionContext | None = None,  # exec_ctx는 현재 사용하지 않지만 시그니처 호환용
) -> None:
    """최종 결과를 TaskArtifactUpdateEvent 로 발행한다."""
    del exec_ctx  # unused
    try:
        if not isinstance(text, str):
            artifact_text = json.dumps(text, ensure_ascii=False)
        else:
            artifact_text = text
        artifact = new_text_artifact(
            name=name,
            description=description,
            text=artifact_text,
        )
        eq.enqueue_event(
            TaskArtifactUpdateEvent(
                artifact=artifact,
                lastChunk=True,
                contextId=proc_inst_id,
                taskId=task_id,
            )
        )
    except Exception:
        logger.exception("Failed to enqueue TaskArtifactUpdateEvent", exc_info=True)
