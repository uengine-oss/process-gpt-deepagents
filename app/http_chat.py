"""HTTP /chat 및 /chat/stream — 폴링(A2A)과 별도의 동기 채팅 진입점."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.subagents import load_agents_from_db, load_tenant_agents_for_chat
from app.executor.workitem_text import strip_leading_workitem_echo_for_sse
from app.output_utils import ensure_trailing_newline, unwrap_output_wrapper_result_text
from app.database import get_tenant_mcp_config
from deepagents_executor import DeepAgentsExecutor, MissingSubagentsConfigurationError
from processgpt_agent_sdk.utils import set_agent_model
from processgpt_agent_utils.utils.context_manager import set_context

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str = Field(..., description="사용자 메시지")
    tenant_id: str = Field(default="", description="테넌트 ID")
    user_uid: str = Field(default="", description="사용자 UID")
    user_email: str = Field(default="", description="사용자 이메일")
    user_name: str = Field(default="", description="사용자 이름")
    user_jwt: str = Field(default="", description="사용자 JWT (MCP/도구 연동용)")
    conversation_id: Optional[str] = Field(default=None, description="대화 ID")
    file: Optional[Dict[str, Any]] = Field(default=None, description="첨부 파일(단일)")
    files: List[Dict[str, Any]] = Field(default_factory=list, description="첨부 파일(다중)")
    file_count: int = Field(default=0, description="첨부 파일 개수")
    stream: bool = Field(default=True, description="(호환 필드) 스트리밍은 /chat/stream 사용")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="추가 메타데이터")


class ChatResponse(BaseModel):
    response: str = Field(default="", description="에이전트 응답 텍스트(또는 폼 JSON 문자열)")
    conversation_id: str = Field(..., description="대화 ID")
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list, description="서브에이전트별 도구 사용 요약")
    status: str = Field(default="completed", description="completed | input_required")
    question: str = Field(default="", description="HITL 시 질문 텍스트")
    run_state: Optional[Dict[str, Any]] = Field(default=None, description="HITL 재개용 run_state")


def _build_effective_metadata(request: ChatRequest) -> Dict[str, Any]:
    # NOTE: HTTP streaming에서는 내부 이벤트 emitter(callable)를 metadata에 임시 주입한다.
    # 이 값이 [InputData] JSON 직렬화 경로로 흘러들면 TypeError가 나므로,
    # InputData용 메타데이터에서는 내부/비직렬화 값을 제거한다.
    meta: Dict[str, Any] = {}
    for k, v in (request.metadata or {}).items():
        # 내부 키는 InputData로 내보내지 않는다.
        if isinstance(k, str) and (k.startswith("_") or k.startswith("__")):
            continue
        # callable(함수 등)은 JSON 직렬화 불가.
        if callable(v):
            continue
        meta[k] = v
    files = request.files if isinstance(request.files, list) else []
    file_obj = request.file if isinstance(request.file, dict) else None
    if files or file_obj:
        if not file_obj and files:
            file_obj = files[0]
        if file_obj:
            meta["file"] = file_obj
        if files:
            meta["files"] = files
        meta["file_count"] = request.file_count if request.file_count > 0 else len(files)
        meta["input_data"] = {
            "file": file_obj,
            "files": files,
            "fileCount": request.file_count if request.file_count > 0 else len(files),
        }
    return meta


def _format_workitem_query(message: str, request: ChatRequest) -> str:
    meta = _build_effective_metadata(request)
    # 채팅 요청 바디에 포함된 값은 모두 InputData로 전달 가능해야 한다.
    # 단, message는 [Instruction]로도 사용되므로 InputData에도 함께 담아 "요청 전체" 재현이 가능하게 한다.
    base: Dict[str, Any] = request.model_dump(exclude={"metadata"})
    base["metadata"] = meta
    input_obj: Dict[str, Any] = base
    return (
        "[Description]\nHTTP 채팅 요청\n\n"
        f"[Instruction]\n{message.strip()}\n\n"
        f"[InputData]\n{json.dumps(input_obj, ensure_ascii=False)}"
    )


def _parse_agent_ids(metadata: Dict[str, Any]) -> list[str]:
    raw = metadata.get("agent_ids")
    if raw is None:
        raw = metadata.get("agents")
    if isinstance(raw, str) and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


async def _resolve_agents(tenant_id: str, metadata: Dict[str, Any]) -> list[dict[str, Any]]:
    tid = (tenant_id or "").strip()
    if not tid:
        return []
    ids = _parse_agent_ids(metadata)
    if ids:
        return load_agents_from_db(ids, tid)
    return load_tenant_agents_for_chat(tid)


class NoopEventQueue:
    """ProcessGPTEventQueue 대체: DB/이벤트 영속화 없이 execute만 수행."""

    def enqueue_event(self, event: Any) -> None:
        del event


class SimpleChatContext:
    """ProcessGPTRequestContext 대체( prepare_context 없이 extras 주입)."""

    def __init__(self, *, query: str, row: dict[str, Any], extras: dict[str, Any]) -> None:
        self.row = row
        self._query = query
        self._extras = extras

    def get_user_input(self) -> str:
        return self._query

    def get_context_data(self) -> dict[str, Any]:
        return {"row": self.row, "extras": self._extras}


def create_chat_app(executor: DeepAgentsExecutor) -> FastAPI:
    app = FastAPI(title="DeepAgents HTTP Chat", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def _extract_bearer_jwt(http_request: Request) -> str:
        """Authorization: Bearer <jwt> 를 user_jwt로 주입한다(로그에는 남기지 않음)."""
        try:
            auth = http_request.headers.get("authorization") or http_request.headers.get("Authorization") or ""
        except Exception:
            auth = ""
        s = (auth or "").strip()
        if not s:
            return ""
        lower = s.lower()
        if lower.startswith("bearer "):
            return s.split(" ", 1)[1].strip()
        return ""

    async def _run_chat(
        request: ChatRequest,
        *,
        stream_emit: Any | None = None,
    ) -> ChatResponse:
        t0 = time.time()
        tenant_id = (request.tenant_id or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id가 필요합니다.")
        conv_id = (request.conversation_id or "").strip() or str(uuid.uuid4())
        msg = (request.message or "").strip()
        logger.info(
            "[http_chat] request received | conv_id=%s tenant_id=%s stream=%s msg_len=%d",
            conv_id,
            tenant_id,
            bool(stream_emit),
            len(msg),
        )

        agents = await _resolve_agents(tenant_id, request.metadata or {})
        if not agents:
            raise HTTPException(
                status_code=400,
                detail="테넌트에 사용 가능한 에이전트(is_agent)가 없습니다. metadata.agent_ids로 지정하세요.",
            )

        tenant_mcp = get_tenant_mcp_config(tenant_id) or {}
        set_agent_model(agents[0] if agents else None)

        result_box: dict[str, Any] = {}
        extras: dict[str, Any] = {
            "agents": agents,
            "users": [],
            "tenant_mcp": tenant_mcp,
            "form_fields": None,
            "form_html": None,
            "form_id": "defaultform",
            "notify_user_emails": [request.user_email] if (request.user_email or "").strip() else [],
            "summarized_feedback": "",
            "sources": [],
            "http_chat": True,
            "_http_chat_result_box": result_box,
        }
        meta = request.metadata or {}
        if isinstance(meta.get("model"), str) and meta["model"].strip():
            extras["model"] = meta["model"].strip()

        row: dict[str, Any] = {
            "id": conv_id,
            "query": _format_workitem_query(request.message, request),
            "tenant_id": tenant_id,
            "proc_inst_id": conv_id,
            "root_proc_inst_id": conv_id,
            "user_id": (request.user_uid or "").strip(),
            "tool": "http_chat",
            "agent_mode": "COMPLETE",
        }

        if stream_emit is not None:
            extras["_http_chat_stream_emit"] = stream_emit
            # tool 이벤트를 SSE로 보내기 위한 emitter (sync callable)
            # - DeepAgentsExecutor 내부에서 호출되며, http_chat이 루프에 안전하게 전달한다.
            extras["_http_chat_stream_event_emit"] = request.metadata.get("_http_chat_stream_event_emit") if isinstance(request.metadata, dict) else None

        set_context(
            task_id=conv_id,
            proc_inst_id=conv_id,
            crew_type="action",
            users_email=extras.get("notify_user_emails", []),
        )

        ctx = SimpleChatContext(query=row["query"], row=row, extras=extras)
        queue = NoopEventQueue()

        try:
            out = await executor.execute(ctx, queue)  # type: ignore[arg-type]
        except MissingSubagentsConfigurationError as e:
            logger.warning(
                "[http_chat] missing subagents | conv_id=%s tenant_id=%s elapsed_ms=%d",
                conv_id,
                tenant_id,
                int((time.time() - t0) * 1000),
            )
            raise HTTPException(status_code=400, detail=str(e) or "서브에이전트 구성이 없습니다.") from e
        except Exception as e:
            logger.exception("HTTP chat execute failed")
            logger.error(
                "[http_chat] failed | conv_id=%s tenant_id=%s elapsed_ms=%d err=%s",
                conv_id,
                tenant_id,
                int((time.time() - t0) * 1000),
                type(e).__name__,
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

        if isinstance(out, dict) and out.get("status") == "waiting_for_human":
            rs = out.get("run_state") if isinstance(out.get("run_state"), dict) else {}
            qtext = str(rs.get("last_question") or "") if isinstance(rs, dict) else ""
            logger.info(
                "[http_chat] input_required | conv_id=%s tenant_id=%s elapsed_ms=%d question_len=%d",
                conv_id,
                tenant_id,
                int((time.time() - t0) * 1000),
                len(qtext),
            )
            return ChatResponse(
                response="",
                conversation_id=conv_id,
                tool_calls=[],
                status="input_required",
                question=qtext,
                run_state=rs if isinstance(rs, dict) else None,
            )

        logger.info(
            "[http_chat] completed | conv_id=%s tenant_id=%s elapsed_ms=%d response_len=%d",
            conv_id,
            tenant_id,
            int((time.time() - t0) * 1000),
            len(str(result_box.get("response") or "")),
        )
        return ChatResponse(
            response=str(result_box.get("response") or ""),
            conversation_id=conv_id,
            tool_calls=list(result_box.get("tool_calls") or []),
            status="completed",
        )

    @app.post("/chat", response_model=ChatResponse)
    async def chat(http_request: Request, request: ChatRequest) -> ChatResponse:
        # 헤더 JWT를 바디 user_jwt로 보강(이미 있으면 그대로 둠)
        if not (request.user_jwt or "").strip():
            hj = _extract_bearer_jwt(http_request)
            if hj:
                request = request.model_copy(update={"user_jwt": hj})
        return await _run_chat(request, stream_emit=None)

    @app.post("/chat/stream")
    async def chat_stream(http_request: Request, request: ChatRequest) -> EventSourceResponse:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        conv_id = (request.conversation_id or "").strip() or str(uuid.uuid4())
        outcome: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        token_count = 0
        tool_event_count = 0
        token_preview_parts: list[str] = []

        async def emit_token(delta: str) -> None:
            nonlocal token_count
            if not delta:
                return
            token_count += 1
            if len(token_preview_parts) < 12:
                token_preview_parts.append(delta)
            await q.put({"type": "token", "content": delta})

        def emit_tool_event(evt: dict[str, Any]) -> None:
            """tool_* 이벤트는 tool 스레드에서 호출될 수 있으므로 루프에 안전하게 전달."""
            nonlocal tool_event_count
            if not isinstance(evt, dict):
                return
            et = str(evt.get("type") or "")
            if et in ("tool_start", "tool_end"):
                tool_event_count += 1

            async def _put() -> None:
                await q.put(evt)

            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(_put()))
            except Exception:
                # 마지막 수단: 같은 스레드/루프면 직접 스케줄
                try:
                    asyncio.create_task(_put())
                except Exception:
                    logger.exception("emit_tool_event failed", exc_info=True)

        async def runner() -> None:
            try:
                # 헤더 JWT를 바디 user_jwt로 보강(이미 있으면 그대로 둠)
                req0 = request
                if not (req0.user_jwt or "").strip():
                    hj = _extract_bearer_jwt(http_request)
                    if hj:
                        req0 = req0.model_copy(update={"user_jwt": hj})
                req2 = req0.model_copy(update={"conversation_id": conv_id})
                # tool emitter를 metadata에 임시 주입(요청 객체는 불변이므로 model_copy로 전달)
                md = dict(req2.metadata or {})
                md["_http_chat_stream_event_emit"] = emit_tool_event
                req2 = req2.model_copy(update={"metadata": md})
                # token/tool 이벤트를 DeepAgentsExecutor로 전달
                # - token: async (그래프 values 스트림에서 await)
                # - tool: sync (도구 호출 가드/훅에서 call_soon_threadsafe)
                outcome["response"] = await _run_chat(
                    req2,
                    stream_emit=emit_token,
                )
            except HTTPException as he:
                outcome["http_error"] = {"status_code": he.status_code, "detail": he.detail}
            except Exception as e:
                outcome["error"] = str(e)
                logger.exception("HTTP chat stream runner failed")
            finally:
                await q.put({"type": "_internal_done"})

        async def event_generator():
            # 표준 SSE 형식 (요구사항):
            # - event 필드 없이 data만 사용 (event: ... 라인 제거)
            # - 매 메시지는 data: {JSON 한 줄} 1개만 전송
            # - 프롬프트/프리플라이트 텍스트는 보내지 않음 (토큰/툴/완료만)
            yield {"data": json.dumps({"conversation_id": conv_id}, ensure_ascii=False)}
            task = asyncio.create_task(runner())
            try:
                while True:
                    evt = await q.get()
                    if evt.get("type") == "_internal_done":
                        break
                    yield {"data": json.dumps(evt, ensure_ascii=False)}
            finally:
                await task
            if "http_error" in outcome:
                done_err = json.dumps(outcome["http_error"], ensure_ascii=False)
                yield {
                    "data": json.dumps(
                        {
                            "type": "done",
                            "content": ensure_trailing_newline(done_err),
                        },
                        ensure_ascii=False,
                    )
                }
            elif "error" in outcome:
                yield {
                    "data": json.dumps(
                        {
                            "type": "done",
                            "content": ensure_trailing_newline(str(outcome["error"])),
                        },
                        ensure_ascii=False,
                    )
                }
            elif "response" in outcome:
                r = outcome["response"]
                done_text = ""
                if hasattr(r, "response"):
                    done_text = str(getattr(r, "response") or "")
                else:
                    done_text = str(r)
                done_text = unwrap_output_wrapper_result_text(done_text).strip()
                done_text = strip_leading_workitem_echo_for_sse(done_text).lstrip()
                if not done_text:
                    # 실행은 되었지만 응답 추출이 비었을 때의 안전장치 (클라이언트가 빈 done을 받으면 UX가 깨짐)
                    done_text = "요청 작업을 수행했지만 최종 답변을 생성하지 못했습니다."
                done_text = ensure_trailing_newline(done_text)
                yield {"data": json.dumps({"type": "done", "content": done_text}, ensure_ascii=False)}
                logger.info(
                    "[http_chat] stream_done | conv_id=%s tenant_id=%s tokens=%d tool_events=%d preview=%r",
                    conv_id,
                    (request.tenant_id or "").strip(),
                    token_count,
                    tool_event_count,
                    "".join(token_preview_parts)[:240],
                )

        # ping은 ": ping ..." 주석 라인으로 출력되어 data-only 파서에 방해가 될 수 있어 사실상 비활성화한다.
        # (sse-starlette 구현은 ping이 falsy면 기본값으로 되돌릴 수 있어, 매우 큰 값으로 설정)
        return EventSourceResponse(event_generator(), ping=10**9)

    return app
