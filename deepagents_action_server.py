"""DeepAgents A2A 서버: process-gpt-agent-sdk로 폴링 및 실행 (ProcessGPTAgentServer + DeepAgentsActionExecutor)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

from processgpt_agent_sdk.processgpt_agent_framework import (
    ProcessGPTAgentServer,
    ProcessGPTRequestContext,
    ProcessGPTEventQueue,
    ContextPreparationError,
)
from processgpt_agent_sdk.database import record_event, update_task_error
from processgpt_agent_sdk.utils import summarize_error_to_user

from database import set_task_waiting_hitl
from deepagents_action_executor import DeepAgentsActionExecutor
from health_server import start_health_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
logging.getLogger("deepagents_executor").setLevel(logging.INFO)
logging.getLogger("database").setLevel(logging.INFO)


class DeepAgentsActionServer(ProcessGPTAgentServer):
    """HITL 시 crew_completed 미발행, run_state 저장으로 재개 가능하도록 하는 서버."""

    async def process_todolist_item(self, row: Dict[str, Any]) -> None:
        task_id = row.get("id")
        logger.info("\n🎯 [작업 처리 시작] Task ID: %s", task_id)
        friendly_text: Optional[str] = None

        try:
            context = ProcessGPTRequestContext(row)
            await context.prepare_context()

            logger.info("\n\n🤖 [Agent Orchestrator 실행]")
            event_queue = ProcessGPTEventQueue(
                str(task_id), self.agent_orch, row.get("proc_inst_id")
            )
            result = await self.agent_executor.execute(context, event_queue)

            if isinstance(result, dict) and result.get("status") == "waiting_for_human":
                run_state = result.get("run_state") or {}
                set_task_waiting_hitl(str(task_id), run_state)
                logger.info(
                    "⏸️ HITL 대기 상태로 저장 (crew_completed 미발행), todo_id=%s",
                    task_id,
                )
                return

            event_queue.task_done()
            logger.info("\n\n🎉 [Agent Orchestrator 완료] Task ID: %s", task_id)

        except Exception as e:
            logger.error("❌ 작업 처리 중 오류 발생: %s", str(e))
            try:
                if isinstance(e, ContextPreparationError) and e.friendly:
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
            except Exception:
                logger.exception("❌ 오류 이벤트 기록 실패")
            try:
                await update_task_error(str(task_id))
            except Exception:
                logger.exception("❌ 작업 상태 FAILED 마킹 실패")


async def main() -> None:
    """메인 서버 실행 (FastAPI + SDK 폴링)."""
    try:
        logger.info("🚀 DeepAgents Server 시작 중...")
        start_health_server(host="0.0.0.0", port=8000)

        executor = DeepAgentsActionExecutor()
        server = DeepAgentsActionServer(
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
