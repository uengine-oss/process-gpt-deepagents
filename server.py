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
from executor import DeepAgentExecutor

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if os.getenv('ENV') == "local":
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_ENDPOINT"] = "https://api.smith.langchain.com"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8888"))
AGENT_TYPE = "deepagents"

async def main() -> None:
    server = ProcessGPTAgentServer(
        agent_executor=DeepAgentExecutor(),
        agent_type=AGENT_TYPE,
    )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/health", health), *skills_routes])
    server.mount_chat_sse(app, path="/chat/stream")

    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    )

    logger.info("서버 시작: http://%s:%d/chat/stream", HOST, PORT)

    await asyncio.gather(
        server.run(),
        uvicorn_server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
