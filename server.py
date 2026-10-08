import os
import sys

# --- Windows cp949 회피: 전체 프로세스를 Python UTF-8 모드로 강제 ---
# Windows 기본 로케일(cp949)에서는 MCP stdio 서브프로세스 stdout(UTF-8 JSON-RPC)·
# 파일 IO 등을 디코드할 때 "'cp949' codec can't decode byte 0xe2 ..." 오류가 난다.
# UTF-8 모드(-X utf8)가 아니면, 같은 인자에 -X utf8 을 붙여 자식 프로세스로 재실행하고
# 그 종료코드를 그대로 전파한다. (execv 대신 subprocess: uv run / Windows 에서 안전)
if os.name == "nt" and sys.flags.utf8_mode == 0:
    import subprocess

    os.environ["PYTHONUTF8"] = "1"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

import asyncio
import logging

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette

from processgpt_agent_sdk import ProcessGPTAgentServer

from core.api.skills_router import routes as skills_routes
from core.api.workspace_file import routes as workspace_file_routes
from core.skills.local_system_skills import seed_local_system_skills
from core.storage.checkpointer import ensure_checkpointer_ready
from executor import DeepAgentExecutor

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# if os.getenv('ENV') == "local":
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_ENDPOINT"] = "https://api.smith.langchain.com"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8888"))
AGENT_TYPE = "deepagents"
# todolist 폴링을 이 프로세스가 맡을지.
#
# 세션당 파드 배포에서는 꺼야 한다. 켜 두면 대화마다 뜨는 파드가 전부 워크아이템
# 소비자가 되어, 유휴 TTL 로 회수될 파드가 남의 일을 집어간다 — 그 파드가 사라지면
# 집어간 일도 같이 멎는다. 폴링은 수명이 긴 워크로드 하나가 맡는다
# (codex 는 CODEX_PROCESS_POLLING 과 process-gpt-codex-worker 로 같은 일을 한다).
PROCESS_POLLING = os.environ.get("DEEPAGENTS_PROCESS_POLLING", "true").strip().lower() not in (
    "false", "0", "no",
)

async def main() -> None:
    # DB_* 가 설정됐는데 체크포인트 저장소에 붙지 못하면 기동하지 않는다. 메모리
    # 저장으로 뜨면 재개·사람 답변이 다른 파드에서 반영되지 않는다(조용한 고장).
    await ensure_checkpointer_ready()

    try:
        synced = await asyncio.to_thread(seed_local_system_skills)
        logger.info("local-system-skills: 기동 시 %d개 스킬 시딩", synced)
    except Exception:
        logger.exception("local-system-skills: 기동 시 시딩 실패, 번들 시스템 스킬 없이 계속 진행")

    server = ProcessGPTAgentServer(
        agent_executor=DeepAgentExecutor(),
        agent_type=AGENT_TYPE,
        # 본문 tenant_id 가 executor → _get_skills(tenant_id) → 컨테이너 스킬 마운트까지
        # 그대로 흘러가므로 SDK 가드로 앞단에서 검증한다.
        tenant_auth=True,
    )

    app = Starlette(routes=[
        *skills_routes,
        *workspace_file_routes,
    ])
    # 채팅 인프라 한 벌(/chat/stream · 재접속 · 중지 · /health)을 SDK 가 통째로 건다.
    # 유휴 SSE 하트비트와 테넌트 가드 미들웨어도 여기 포함된다. 이 저장소에 남은
    # 채팅 코드는 에이전트 로직(HITL 판단, 채팅방 컨텍스트 저장)뿐이다.
    #
    # /health 의 busy 필드는 세션 라우터가 유휴 Pod 를 회수해도 되는지 판정하는
    # 근거다 — 턴이 도는 중인 Pod 를 죽이면 대화가 그대로 끊긴다.
    server.mount_chat_routes(
        app,
        health_path="/health",
        health_extra=lambda: {"agent_type": AGENT_TYPE},
    )

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    )

    logger.info(
        "서버 시작: http://%s:%d/chat/stream (todolist 폴링 %s)",
        HOST, PORT, "켬" if PROCESS_POLLING else "끔",
    )

    all_tasks = [asyncio.create_task(uvicorn_server.serve())]
    if PROCESS_POLLING:
        all_tasks.append(asyncio.create_task(server.run()))

    # 어느 한쪽이 SIGINT/SIGTERM 으로 종료되면 나머지 task 도 취소한다.
    done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("서버 종료 완료")


if __name__ == "__main__":
    asyncio.run(main())
