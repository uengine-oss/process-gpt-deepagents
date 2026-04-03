"""DeepAgents A2A 서버: process-gpt-agent-sdk로 폴링 및 실행 (ProcessGPTAgentServer + DeepAgentsExecutor)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from processgpt_agent_sdk.processgpt_agent_framework import (
    ProcessGPTAgentServer,
    ProcessGPTRequestContext,
    ProcessGPTEventQueue,
    ContextPreparationError,
)
from processgpt_agent_sdk.database import (
    record_event,
    update_task_error,
    initialize_db,
    polling_pending_todos,
    get_consumer_id,
)
from processgpt_agent_sdk.utils import summarize_error_to_user

from app.database import claim_resumable_hitl_task, set_task_waiting_hitl
from deepagents_executor import (
    DeepAgentsExecutor,
    MissingSubagentsConfigurationError,
)
from app.health_server import start_health_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
logging.getLogger("deepagents_executor").setLevel(logging.INFO)
logging.getLogger("app.database").setLevel(logging.INFO)


def _log_skills_volume_inventory(
    *,
    max_tenants: int = 64,
    max_skills_per_tenant: int = 256,
) -> None:
    """서버 기동 시 마운트된 스킬 볼륨 경로와 ``<tenant>/<skill>`` 디렉터리 목록을 로그로 남긴다."""
    root = (os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")
    base = Path(root)
    logger.info(
        "📂 스킬 볼륨 SKILLS_ROOT_DIR=%s (resolved=%s, is_dir=%s)",
        root,
        str(base.resolve()),
        base.is_dir(),
    )
    if not base.is_dir():
        logger.info(
            "📂 스킬 볼륨 디렉터리가 없습니다. Docker 마운트 또는 SKILLS_ROOT_DIR 설정을 확인하세요."
        )
        return
    try:
        tenant_paths = sorted([p for p in base.iterdir() if p.is_dir()])
    except OSError as e:
        logger.warning("📂 스킬 볼륨 루트 목록 읽기 실패: %s", e)
        return
    total_tenants = len(tenant_paths)
    truncated_tenants = tenant_paths[:max_tenants]
    if total_tenants > len(truncated_tenants):
        logger.info(
            "📂 테넌트 디렉터리 %d개 중 처음 %d개만 로그합니다.",
            total_tenants,
            max_tenants,
        )
    for tenant_dir in truncated_tenants:
        tid = tenant_dir.name
        try:
            skill_names = sorted([p.name for p in tenant_dir.iterdir() if p.is_dir()])
        except OSError as e:
            logger.info("📂   tenant_id=%r — 하위 목록 읽기 실패: %s", tid, e)
            continue
        n = len(skill_names)
        if n > max_skills_per_tenant:
            shown = skill_names[:max_skills_per_tenant]
            logger.info(
                "📂   tenant_id=%r — skill_dirs=%d (처음 %d개만) %s",
                tid,
                n,
                max_skills_per_tenant,
                shown,
            )
        else:
            logger.info("📂   tenant_id=%r — skill_dirs(%d)=%s", tid, n, skill_names)
    if total_tenants == 0:
        logger.info("📂 테넌트 하위 디렉터리가 없습니다 (볼륨 루트는 존재).")


def _truncate_for_file_log(val: str, *, max_chars: int = 60000) -> str:
    if not val:
        return ""
    if len(val) <= max_chars:
        return val
    head = val[: max_chars // 2]
    tail = val[-max_chars // 3 :]
    return head + "\n…[truncated]…\n" + tail


def _safe_json_for_log(obj: Any) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        try:
            s = str(obj)
        except Exception:
            s = "<unserializable>"
    return _truncate_for_file_log(s)


def _install_db_event_file_logging_hooks() -> None:
    """DB에 기록되는 이벤트 payload를 작업별 파일 로그에도 남기기 위한 훅."""
    try:
        import processgpt_agent_sdk.database as sdk_db
        import processgpt_agent_sdk.processgpt_agent_framework as sdk_fw
    except Exception:
        logger.exception("processgpt_agent_sdk import 실패로 이벤트 로그 훅 설치 불가", exc_info=True)
        return

    # 1) bulk 기록 경로: enqueue_ui_event_coalesced -> record_events_bulk(record_events_bulk RPC)
    try:
        orig_enqueue = sdk_fw.enqueue_ui_event_coalesced

        async def _enqueue_wrapped(payload: Dict[str, Any]):  # type: ignore[no-redef]
            try:
                logger.info("🧾 [DB 이벤트 payload] %s", _safe_json_for_log(payload))
            except Exception:
                logger.exception("DB 이벤트 payload 파일 기록 실패", exc_info=True)
            return await orig_enqueue(payload)

        sdk_fw.enqueue_ui_event_coalesced = _enqueue_wrapped  # type: ignore[assignment]
    except Exception:
        logger.exception("enqueue_ui_event_coalesced 훅 설치 실패", exc_info=True)

    # 2) 단건 기록 경로: record_event(events table insert)
    try:
        orig_record_event = sdk_db.record_event

        async def _record_event_wrapped(payload: Dict[str, Any]):  # type: ignore[no-redef]
            try:
                logger.info("🧾 [DB 이벤트 payload(record_event)] %s", _safe_json_for_log(payload))
            except Exception:
                logger.exception("record_event payload 파일 기록 실패", exc_info=True)
            return await orig_record_event(payload)

        sdk_db.record_event = _record_event_wrapped  # type: ignore[assignment]
    except Exception:
        logger.exception("record_event 훅 설치 실패", exc_info=True)

    # 3) 다건 기록 경로(직접 호출될 수 있음): record_events_bulk
    try:
        orig_record_events_bulk = sdk_db.record_events_bulk

        async def _record_events_bulk_wrapped(payloads: list[Dict[str, Any]]):  # type: ignore[no-redef]
            try:
                logger.info(
                    "🧾 [DB 이벤트 payload(record_events_bulk)] count=%d first=%s",
                    len(payloads or []),
                    _safe_json_for_log((payloads or [None])[0]),
                )
            except Exception:
                logger.exception("record_events_bulk payload 파일 기록 실패", exc_info=True)
            return await orig_record_events_bulk(payloads)

        sdk_db.record_events_bulk = _record_events_bulk_wrapped  # type: ignore[assignment]
    except Exception:
        logger.exception("record_events_bulk 훅 설치 실패", exc_info=True)


_install_db_event_file_logging_hooks()


class _TaskFileLogSession(contextlib.AbstractContextManager["_TaskFileLogSession"]):
    """Attach a per-task file handler under workspace root."""

    def __init__(self, *, task_id: Any, proc_inst_id: Any) -> None:
        self.task_id = str(task_id or "unknown-task")
        self.proc_inst_id = str(proc_inst_id or "unknown-proc")
        self.log_path: Path | None = None
        self._handler: logging.Handler | None = None

    def __enter__(self) -> "_TaskFileLogSession":
        now = dt.datetime.now()
        ts = now.strftime("%Y%m%d-%H%M%S")
        date_dir = now.strftime("%Y-%m-%d")
        safe_task_id = self.task_id.replace("/", "_")
        safe_proc_id = self.proc_inst_id.replace("/", "_")
        log_dir = Path.cwd() / "logs" / date_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"task-{safe_task_id}-{safe_proc_id}-{ts}.log"

        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logging.getLogger().addHandler(handler)
        self._handler = handler
        logger.info("📝 작업별 로그 파일 생성: %s", str(self.log_path))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            try:
                self._handler.close()
            except Exception:
                logger.exception("작업별 로그 핸들러 종료 실패", exc_info=True)
        if self.log_path is not None:
            logger.info("🧾 작업별 로그 파일 저장 완료: %s", str(self.log_path))
        return None


class _ThreadSafeEventQueueProxy:
    """Proxy that marshals enqueue_event calls onto the main asyncio loop.

    SDK's ProcessGPTEventQueue.enqueue_event uses asyncio.create_task() internally.
    When tools are executed via sync invoke() in a worker thread, enqueue_event may
    be called from that thread, which has no running event loop → RuntimeError.
    This proxy ensures enqueue_event always runs on the loop thread.
    """

    def __init__(self, inner: ProcessGPTEventQueue, loop: asyncio.AbstractEventLoop) -> None:
        self._inner = inner
        self._loop = loop

    def enqueue_event(self, event: Any) -> None:
        # If we're already on the loop thread, call directly.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self._loop:
            self._inner.enqueue_event(event)
            return

        # Cross-thread: marshal onto the main loop thread.
        def _call() -> None:
            try:
                self._inner.enqueue_event(event)
            except Exception:
                logger.exception("❌ 이벤트 enqueue 실패(루프 전달 후)", exc_info=True)

        try:
            self._loop.call_soon_threadsafe(_call)
        except Exception:
            logger.exception("❌ call_soon_threadsafe 실패", exc_info=True)

    def task_done(self) -> None:
        # task_done is called from the main async flow; keep it direct.
        self._inner.task_done()


class DeepAgentsServer(ProcessGPTAgentServer):
    """HITL 시 crew_completed 미발행, run_state 저장으로 재개 가능하도록 하는 서버."""

    async def run(self) -> None:
        """SDK 기본 폴링 + HITL 재개 보조 폴링을 함께 수행."""
        self.is_running = True
        logger.info("\n\n🚀 DeepAgents Action Server START (agent=%s)\n", self.agent_orch)
        initialize_db()
        await self._install_signal_handlers()

        while self.is_running and not self._shutdown_event.is_set():
            try:
                consumer = get_consumer_id()

                # 1) 기본 pending 폴링 (SDK RPC fetch_pending_task 기반)
                row = await polling_pending_todos(self.agent_orch, consumer)

                # 2) 보조 경로: HITL 대기(HUMAN_ASKED) + 답변(APPROVED) 감지 시 claim 후 재개
                if not row:
                    try:
                        row = claim_resumable_hitl_task(agent_orch=self.agent_orch, consumer=consumer)
                    except Exception:
                        logger.exception("HITL resume polling failed", exc_info=True)
                        row = None

                if row:
                    logger.info("✅ [새 작업 발견] Task ID: %s", row.get("id"))
                    logger.info(
                        "• Activity: %s | Tool: %s | Tenant: %s | draft_status=%s",
                        row.get("activity_name"),
                        row.get("tool"),
                        row.get("tenant_id"),
                        row.get("draft_status"),
                    )
                    try:
                        self._current_todo_id = str(row.get("id"))
                        await self.process_todolist_item(row)
                    except Exception as e:
                        logger.exception("process_todolist_item failed: %s", str(e))
                    finally:
                        self._current_todo_id = None
                    continue

                await asyncio.sleep(10)

            except Exception as e:
                logger.exception("run loop error: %s", str(e))
                await asyncio.sleep(10)

    async def process_todolist_item(self, row: Dict[str, Any]) -> None:
        task_id = row.get("id")
        proc_inst_id = row.get("proc_inst_id")
        with _TaskFileLogSession(task_id=task_id, proc_inst_id=proc_inst_id):
            logger.info("\n🎯 [작업 처리 시작] Task ID: %s", task_id)
            logger.info(
                "📥 폴링으로 작업 감지됨 | task_id=%s proc_inst_id=%s agent_orch=%s",
                task_id,
                proc_inst_id,
                self.agent_orch,
            )
            friendly_text: Optional[str] = None

            try:
                context = ProcessGPTRequestContext(row)
                await context.prepare_context()
                logger.info("✅ RequestContext 준비 완료 | task_id=%s", task_id)

                logger.info("\n\n🤖 [Agent Orchestrator 실행]")
                base_event_queue = ProcessGPTEventQueue(
                    str(task_id), self.agent_orch, row.get("proc_inst_id")
                )
                # Wrap with thread-safe proxy so tool events from worker threads
                # don't crash with "no running event loop".
                loop = asyncio.get_running_loop()
                event_queue = _ThreadSafeEventQueueProxy(base_event_queue, loop)
                logger.info("🧵 EventQueue 생성 완료 | task_id=%s", task_id)
                result = await self.agent_executor.execute(context, event_queue)
                logger.info(
                    "📤 AgentExecutor 반환 | task_id=%s result_type=%s",
                    task_id,
                    type(result).__name__,
                )

                if isinstance(result, dict) and result.get("status") == "waiting_for_human":
                    run_state = result.get("run_state") or {}
                    set_task_waiting_hitl(str(task_id), run_state)
                    logger.info(
                        "⏸️ HITL 대기 상태로 저장 (crew_completed 미발행), todo_id=%s",
                        task_id,
                    )
                    return

                event_queue.task_done()
                logger.info("✅ event_queue.task_done 호출 완료 | task_id=%s", task_id)
                logger.info("\n\n🎉 [Agent Orchestrator 완료] Task ID: %s", task_id)

            except Exception as e:
                logger.error("❌ 작업 처리 중 오류 발생: %s", str(e))
                try:
                    if isinstance(e, MissingSubagentsConfigurationError):
                        friendly_text = e.friendly
                    elif isinstance(e, ContextPreparationError) and e.friendly:
                        friendly_text = e.friendly
                    else:
                        friendly_text = await summarize_error_to_user(
                            e.original if isinstance(e, ContextPreparationError) else e,
                            {
                                "task_id": task_id,
                                "proc_inst_id": row.get("proc_inst_id"),
                                "agent_orch": self.agent_orch,
                                "tool": row.get("tool"),
                            },
                        )
                except Exception:
                    friendly_text = None

                payload: Dict[str, Any] = {
                    "id": str(uuid.uuid4()),
                    "job_id": "TASK_ERROR",
                    "todo_id": str(task_id),
                    "proc_inst_id": row.get("proc_inst_id"),
                    "crew_type": "agent",
                    "event_type": "error",
                    "data": {
                        "name": "시스템 오류 알림",
                        "goal": "오류 원인과 대처 안내를 전달합니다.",
                        "agent_profile": "/images/chat-icon.png",
                        "friendly": friendly_text or "처리 중 오류가 발생했습니다. 로그를 확인해 주세요.",
                        "raw_error": f"{type(e).__name__}: {str(e)}"
                        if not isinstance(e, ContextPreparationError)
                        else f"{type(e.original).__name__}: {str(e.original)}",
                    },
                }
                try:
                    asyncio.create_task(record_event(payload))
                    logger.info("🧾 오류 이벤트 기록 태스크 생성 완료 | task_id=%s", task_id)
                except Exception:
                    logger.exception("❌ 오류 이벤트 기록 실패")
                try:
                    await update_task_error(str(task_id))
                    logger.info("🚨 작업 상태 FAILED 업데이트 완료 | task_id=%s", task_id)
                except Exception:
                    logger.exception("❌ 작업 상태 FAILED 마킹 실패")


async def main() -> None:
    """메인 서버 실행 (FastAPI + SDK 폴링)."""
    try:
        logger.info("🚀 DeepAgents Server 시작 중...")
        _log_skills_volume_inventory()
        start_health_server(host="0.0.0.0", port=8888)

        executor = DeepAgentsExecutor()
        server = DeepAgentsServer(
            agent_executor=executor,
            agent_type="deepagents",
        )
        server.polling_interval = 5

        logger.info("✅ 서버 설정 완료, 폴링 시작...")
        await server.run()

    except Exception as e:
        logger.error("❌ 서버 실행 중 오류 발생: %s", e, exc_info=True)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 서버 종료 요청됨")
    except Exception as e:
        logger.error("💥 치명적 오류: %s", e, exc_info=True)
        exit(1)
