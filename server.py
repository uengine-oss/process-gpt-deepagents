import asyncio
import logging
import os

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
