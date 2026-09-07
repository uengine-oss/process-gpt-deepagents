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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from processgpt_agent_sdk import ProcessGPTAgentServer

from core.api.skills_router import routes as skills_routes
from core.api.file_download import routes as file_download_routes
from core.api.chat_stream_attach import routes as chat_stream_attach_routes
from core.api.chat_stop import routes as chat_stop_routes
from core.api.sse_heartbeat import install_sse_heartbeat
from core.api.tenant_auth import ChatTenantGuardMiddleware
from core.chat.inflight_registry import start_cancel_listener
from core.skills.local_system_skills import seed_local_system_skills
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

async def main() -> None:
    try:
        synced = await asyncio.to_thread(seed_local_system_skills)
        logger.info("local-system-skills: 기동 시 %d개 스킬 시딩", synced)
    except Exception:
        logger.exception("local-system-skills: 기동 시 시딩 실패, 번들 시스템 스킬 없이 계속 진행")

    server = ProcessGPTAgentServer(
        agent_executor=DeepAgentExecutor(),
        agent_type=AGENT_TYPE,
    )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[
        Route("/health", health),
        *skills_routes,
        *file_download_routes,
        *chat_stream_attach_routes,
        *chat_stop_routes,
    ])
    server.mount_chat_sse(app, path="/chat/stream")
    # 유휴 SSE 커넥션이 프록시에서 끊겨 화면이 "생각 중..." 에 멈추는 것을 막는다.
    install_sse_heartbeat(app, "/chat/stream", float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15")))
    # /chat/stream 은 SDK 가 소유한 라우트라 데코레이터를 달 수 없다. 본문 tenant_id 가
    # 그대로 스킬 디렉토리 로드/컨테이너 마운트까지 흘러가므로 앞단에서 검증한다.
    app.add_middleware(ChatTenantGuardMiddleware, paths=("/chat/stream",))

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    )

    logger.info("서버 시작: http://%s:%d/chat/stream", HOST, PORT)

    all_tasks = [
        asyncio.create_task(server.run()),
        asyncio.create_task(uvicorn_server.serve()),
    ]
    # REDIS_URL 설정 시에만 cancel listener를 띄운다. 미설정이면 start_cancel_listener()가
    # 즉시 반환하는데, 아래 FIRST_COMPLETED가 이를 "task 종료"로 보고 나머지(서버 자체)까지
    # 같이 내려버리므로 애초에 task 목록에 넣지 않는다.
    if os.environ.get("REDIS_URL"):
        all_tasks.append(asyncio.create_task(start_cancel_listener()))

    # server.run()이 SIGINT/SIGTERM으로 종료되면 나머지 task도 취소한다.
    done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    logger.info("서버 종료 완료")


if __name__ == "__main__":
    asyncio.run(main())
