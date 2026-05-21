"""Emit A2A events on the subagent's own adhoc workitem after it completes.

Adhoc subagents are derived from subprocess activity workitems. After the deep
agent's task_completed fires, this module records the same completion against
the subagent's own workitem (proc_inst_id=서브, task_id=서브).

Added as step 4-1 only — does not alter the existing deep-agent event flow.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .a2a_events import enqueue_artifact_update, enqueue_status_update

logger = logging.getLogger(__name__)


def emit_subagent_workitem_events(
    event_queue: Any,
    *,
    sub_task_id: str,
    sub_proc_inst_id: str,
    subagent_name: str,
    exec_ctx: Any | None,
    result: Any,
    exc_caught: Exception | None,
    form_fields_json: list[dict[str, Any]] | None = None,
) -> None:
    """Step 4-1: emit task_started + task_completed + artifact_update on the subagent's own workitem.

    Only fires when sub_task_id, sub_proc_inst_id, and form_fields_json are all present.
    """
    if not sub_task_id or not sub_proc_inst_id:
        return
    if not (isinstance(form_fields_json, list) and form_fields_json):
        return

    job_id = str(uuid.uuid4())
    result_text = _extract_result_text(result)

    # task_started
    try:
        enqueue_status_update(
            event_queue,
            "task_started",
            {"message": f"{subagent_name} 작업을 시작했습니다."},
            sub_proc_inst_id,
            sub_task_id,
            "result",
            job_id=job_id,
            exec_ctx=exec_ctx,
            agent_name=subagent_name,
        )
    except Exception:
        logger.exception(
            "Failed to emit subagent workitem task_started | sub_task_id=%s", sub_task_id, exc_info=True
        )

    # task_completed
    try:
        if exc_caught is not None:
            completed_data: Any = {
                "message": f"{subagent_name} 작업 실행 중 오류가 발생했습니다.",
                "error": str(exc_caught),
            }
        else:
            completed_data = _try_parse_json(result_text) or {
                "result": result_text,
            }
        enqueue_status_update(
            event_queue,
            event_type="task_completed",
            data=completed_data,
            proc_inst_id=sub_proc_inst_id,
            task_id=sub_task_id,
            crew_type="result",
            job_id=job_id,
            exec_ctx=exec_ctx,
            agent_name=subagent_name,
        )
    except Exception:
        logger.exception(
            "Failed to emit subagent workitem task_completed | sub_task_id=%s", sub_task_id, exc_info=True
        )

    # artifact_update
    if exc_caught is None:
        try:
            enqueue_artifact_update(
                event_queue,
                name=subagent_name,
                description="",
                text=result_text,
                proc_inst_id=sub_proc_inst_id,
                task_id=sub_task_id,
                exec_ctx=exec_ctx,
            )
        except Exception:
            logger.exception(
                "Failed to emit subagent workitem artifact_update | sub_task_id=%s", sub_task_id, exc_info=True
            )


def _extract_result_text(result: Any) -> str:
    # LangGraph Command(update={'messages': [ToolMessage(content=...), ...]})
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        messages = update.get("messages") or []
        for msg in reversed(messages):
            content = getattr(msg, "content", None)
            if content is not None:
                return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    raw = getattr(result, "content", None)
    if raw is None and isinstance(result, str):
        raw = result
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except Exception:
        return str(raw)


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
