"""DeepAgents polling server: todolist polling + create_deep_agent execution (no process-gpt / a2a)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import database as db
from agent_execution import ExecutionContext
from deepagents_action_executor import DeepAgentsActionExecutor
from health_server import start_health_server

# Logging first so it appears in terminal
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
# Ensure executor and DB module logs appear in terminal
logging.getLogger("deepagents_action_executor").setLevel(logging.INFO)
logging.getLogger("database").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Request context (get_user_input, get_context_data)
# ---------------------------------------------------------------------------


class _RequestContext:
    def __init__(self, row: dict[str, Any], extras: dict[str, Any]):
        self._row = row
        self._extras = extras
        self._user_input = (row.get("query") or "").strip()

    def get_user_input(self) -> str:
        return self._user_input

    def get_context_data(self) -> dict[str, Any]:
        return {"row": self._row, "extras": self._extras}


# ---------------------------------------------------------------------------
# Event queue: dict events -> public.events table + save_task_result for artifacts
# ---------------------------------------------------------------------------


class _DBEventQueue:
    """Sync enqueue_event: write to events table from dict events; optionally save_task_result for artifact."""

    def __init__(self, todo_id: str, proc_inst_id: str | None, event_logger: logging.Logger | None = None):
        self.todo_id = todo_id
        self.proc_inst_id = proc_inst_id or ""
        self._event_logger = event_logger or logger

    def enqueue_event(self, event: dict[str, Any]) -> None:
        """Accept event dict: event_type, job_id, crew_type, data; for artifact also text, last_chunk."""
        event_id = str(uuid.uuid4())
        event_type = event.get("event_type", "task_started")
        job_id = event.get("job_id", "")
        crew_type = event.get("crew_type")
        data = event.get("data")
        if data is None:
            data = {}
        db.insert_event(
            id=event_id,
            job_id=job_id,
            todo_id=self.todo_id,
            proc_inst_id=self.proc_inst_id or None,
            event_type=event_type,
            status=event.get("status"),
            crew_type=crew_type,
            data=data,
        )
        self._event_logger.info("[이벤트] event_type=%s crew_type=%s todo_id=%s", event_type, crew_type or "-", self.todo_id)

        if event.get("last_chunk"):
            # 최종 아티팩트는 JSON 객체/배열 그대로 todolist.draft(jsonb)에 저장한다.
            # DeepAgents 실행기는 dict/list payload를 넘기도록 해야 한다.
            text = event.get("text")
            db.save_task_result(self.todo_id, text if text is not None else "", final=True)

    def task_done(self) -> None:
        db.insert_event(
            id=str(uuid.uuid4()),
            job_id="CREW_FINISHED",
            todo_id=self.todo_id,
            proc_inst_id=self.proc_inst_id or None,
            event_type="crew_completed",
            status=None,
            crew_type="crew",
            data={"message": "Task completed successfully"},
        )
        self._event_logger.info("[이벤트] event_type=crew_completed crew_type=crew todo_id=%s", self.todo_id)


# ---------------------------------------------------------------------------
# Context preparation (sync, run in executor to avoid blocking)
# ---------------------------------------------------------------------------


def _prepare_extras(row: dict[str, Any]) -> dict[str, Any]:
    proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id") or ""
    tenant_id = row.get("tenant_id") or ""
    tool_val = row.get("tool") or ""
    user_ids_str = row.get("user_id") or ""
    user_id_list = [u.strip() for u in user_ids_str.split(",") if u.strip()]

    form_id, form_fields, _ = db.fetch_form_def(tool_val, tenant_id)
    agents = db.fetch_agents_by_user_ids(user_id_list, tenant_id)
    notify_emails = db.fetch_email_users_by_proc_inst_id(proc_inst_id)
    sources = db.fetch_proc_inst_sources(proc_inst_id)

    task_id = str(row.get("id", ""))
    draft = row.get("draft") or {}
    run_state = draft.get("run_state") if isinstance(draft, dict) else None
    human_response_answer = None
    if isinstance(run_state, dict):
        human_response_answer = db.get_latest_human_response(task_id)

    return {
        "id": row.get("id"),
        "proc_inst_id": proc_inst_id,
        "root_proc_inst_id": row.get("root_proc_inst_id"),
        "activity_name": row.get("activity_name"),
        "agents": agents,
        "form_fields": form_fields,
        "form_id": form_id,
        "notify_user_emails": notify_emails.split(",") if notify_emails else [],
        "sources": sources,
        "model": None,
        "system_prompt": None,
        "run_state": run_state,
        "human_response_answer": human_response_answer,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _run_polling() -> None:
    agent_orch = "deepagents-action"
    consumer = db.get_consumer_id()
    executor = DeepAgentsActionExecutor()

    logger.info("DeepAgents Action Server starting (polling %s)", agent_orch)
    start_health_server(host="0.0.0.0", port=8000)

    while True:
        try:
            row = await asyncio.to_thread(db.poll_pending_todo, agent_orch, consumer)
            if not row:
                logger.info("폴링 완료 - 대기 중인 작업 없음 (다음 폴링 10초 후)")
                await asyncio.sleep(10)
                continue

            task_id = str(row.get("id", ""))
            logger.info(
                "[폴링] 작업 수신 task_id=%s activity=%s tool=%s tenant_id=%s",
                task_id,
                row.get("activity_name"),
                row.get("tool"),
                row.get("tenant_id"),
            )

            try:
                extras = await asyncio.to_thread(_prepare_extras, row)
                context = _RequestContext(row, extras)
                event_queue = _DBEventQueue(task_id, row.get("proc_inst_id"), event_logger=logger)
                # Create a per-task ExecutionContext; DeepAgentsActionExecutor may
                # refine or reuse this (e.g. for resume flows).
                exec_ctx = ExecutionContext(
                    correlation_id=str(uuid.uuid4()),
                    agent_path=["root_orchestrator"],
                    task_id=task_id,
                    proc_inst_id=row.get("proc_inst_id"),
                )

                logger.info("[작업 처리 시작] task_id=%s", task_id)
                result = await executor.execute(context, event_queue, exec_ctx)
                if isinstance(result, dict) and result.get("status") == "waiting_for_human":
                    db.set_task_waiting_hitl(task_id, result.get("run_state", {}))
                    logger.info("[작업 대기: human 응답 대기] task_id=%s", task_id)
                else:
                    event_queue.task_done()
                    logger.info("[작업 처리 완료] task_id=%s", task_id)
            except Exception as e:
                logger.exception("작업 처리 실패 task_id=%s: %s", task_id, e)
                try:
                    db.update_task_error(task_id)
                except Exception as db_err:
                    logger.warning("update_task_error 실패: %s", db_err)
                # Error event
                try:
                    db.insert_event(
                        id=str(uuid.uuid4()),
                        job_id="TASK_ERROR",
                        todo_id=task_id,
                        proc_inst_id=row.get("proc_inst_id"),
                        event_type="error",
                        status=None,
                        crew_type="agent",
                        data={
                            "name": "시스템 오류 알림",
                            "goal": "오류 원인과 대처 안내를 전달합니다.",
                            "friendly": str(e),
                            "raw_error": f"{type(e).__name__}: {e}",
                        },
                    )
                except Exception as ev_err:
                    logger.warning("error event insert 실패: %s", ev_err)

        except Exception as e:
            logger.exception("폴링 루프 오류: %s", e)
            await asyncio.sleep(10)


def main_cli() -> None:
    try:
        asyncio.run(_run_polling())
    except KeyboardInterrupt:
        logger.info("Server stopped (KeyboardInterrupt)")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        raise


if __name__ == "__main__":
    main_cli()
