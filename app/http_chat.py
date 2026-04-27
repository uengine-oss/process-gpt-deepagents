"""HTTP /chat 및 /chat/stream — 폴링(A2A)과 별도의 동기 채팅 진입점."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
import uuid
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.subagents import load_agents_from_db, load_tenant_agents_for_chat
from app.runtime_config import normalize_litellm_slash_model_name
from app.inputdata_parser import extract_inputdata_from_message, merge_inputdata_into_chat_payload
from app.executor.workitem_text import strip_leading_workitem_echo_for_sse
from app.output_utils import ensure_trailing_newline, unwrap_output_wrapper_result_text
from app.database import (
    a_insert_chat_attachments,
    a_insert_chat_message,
    a_update_chat_message,
    a_update_chat_room_context,
    a_update_chat_room_last_message,
    a_upsert_chat_room_start,
    build_initial_chat_room_context,
    get_tenant_mcp_config,
)
from deepagents_executor import DeepAgentsExecutor, MissingSubagentsConfigurationError
from processgpt_agent_sdk.utils import set_agent_model
from processgpt_agent_utils.utils.context_manager import set_context

logger = logging.getLogger(__name__)

_ROOM_RECENT_HISTORY_LINE_RE = re.compile(
    r"^\[(?P<role>user|assistant)\]"
    r"(?:\[[^\]]*\])*"
    r"\s*:\s*(?P<content>.*)$",
    re.IGNORECASE,
)


def parse_room_recent_history_to_messages(
    history_text: str,
    *,
    max_messages: int = 24,
    max_total_chars: int = 12000,
) -> list[dict[str, str]]:
    """Parse frontend-formatted room history string into LangChain-like message dicts.

    Expected line format (examples):
    - [user][홍길동]: 다시 시도
    - [assistant][Process GPT Agent][agentId=...]: ...text...

    Returns a list like: [{"role": "user"|"assistant", "content": "..."}]
    """
    raw = (history_text or "").strip()
    if not raw:
        return []

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    out: list[dict[str, str]] = []
    total_chars = 0
    last_role = ""
    last_content = ""

    for ln in lines:
        m = _ROOM_RECENT_HISTORY_LINE_RE.match(ln)
        if not m:
            # Best-effort fallback: treat unknown lines as assistant context
            role = "assistant"
            content = ln
        else:
            role = (m.group("role") or "").strip().lower()
            role = "assistant" if role != "user" else "user"
            content = (m.group("content") or "").strip()

        if not content:
            continue

        # Collapse exact duplicates (common in streaming DB flushes).
        if role == last_role and content == last_content:
            continue

        piece_len = len(content)
        if max_total_chars > 0 and total_chars + piece_len > max_total_chars:
            remaining = max_total_chars - total_chars
            if remaining <= 0:
                break
            content = content[:remaining].rstrip()
            if not content:
                break
            piece_len = len(content)

        out.append({"role": role, "content": content})
        total_chars += piece_len
        last_role, last_content = role, content

        if max_messages > 0 and len(out) >= max_messages:
            break

    return out


class ChatRequest(BaseModel):
    message: str = Field(..., description="사용자 메시지")
    tenant_id: str = Field(default="", description="테넌트 ID")
    user_uid: str = Field(default="", description="사용자 UID")
    user_email: str = Field(default="", description="사용자 이메일")
    user_name: str = Field(default="", description="사용자 이름")
    user_jwt: str = Field(default="", description="사용자 JWT (MCP/도구 연동용)")
    conversation_id: Optional[str] = Field(default=None, description="대화 ID")
    message_uuid: Optional[str] = Field(default=None, description="(멱등키) 사용자 메시지 UUID. 중복 전송 시 같은 값 재사용")
    file: Optional[Dict[str, Any]] = Field(default=None, description="첨부 파일(단일)")
    files: List[Dict[str, Any]] = Field(default_factory=list, description="첨부 파일(다중)")
    file_count: int = Field(default=0, description="첨부 파일 개수")
    stream: bool = Field(default=True, description="(호환 필드) 스트리밍은 /chat/stream 사용")
    agent_ids: Optional[List[str]] = Field(
        default=None,
        description="(선택) 서브에이전트로 쓸 에이전트 id(UUID) 목록. 생략·null이면 테넌트 채팅용 에이전트 전체",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict, description="추가 메타데이터")


class ChatResponse(BaseModel):
    response: str = Field(default="", description="에이전트 응답 텍스트(또는 폼 JSON 문자열)")
    conversation_id: str = Field(..., description="대화 ID")
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list, description="서브에이전트별 도구 사용 요약")
    skill_calls: List[Dict[str, Any]] = Field(default_factory=list, description="서브에이전트별 스킬 사용 요약")
    status: str = Field(default="completed", description="completed | input_required")
    question: str = Field(default="", description="HITL 시 질문 텍스트")
    run_state: Optional[Dict[str, Any]] = Field(default=None, description="HITL 재개용 run_state")
    openui_lang: Optional[str] = Field(
        default=None,
        description="HITL(input_required)일 때 사용할 OpenUI Lang 전문",
    )


from app.hitl_events import (
    resolve_cached_run_state_for_question_id,
    resolve_context_text,
)
from app.openui_hitl_events import (
    build_openui_lang_for_hitl,
    expand_input_required_to_openui_hitl_events,
    run_state_without_openui_lang,
)


def _build_effective_metadata(request: ChatRequest) -> Dict[str, Any]:
    # NOTE: HTTP streaming에서는 내부 이벤트 emitter(callable)를 metadata에 임시 주입한다.
    # 이 값이 [InputData] JSON 직렬화 경로로 흘러들면 TypeError가 나므로,
    # InputData용 메타데이터에서는 내부/비직렬화 값을 제거한다.
    meta: Dict[str, Any] = {}
    for k, v in (request.metadata or {}).items():
        # 내부 키는 InputData로 보내지 않는다.
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
    # NOTE: Attachment handling is enforced via runtime policy flags (routing/guardrails),
    # not by injecting hardcoded instruction text into the model prompt here.
    msg0 = (message or "").strip()
    return (
        "[Description]\nHTTP 채팅 요청\n\n"
        f"[Instruction]\n{msg0}\n\n"
        f"[InputData]\n{json.dumps(input_obj, ensure_ascii=False)}"
    )


def _iso_now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _thread_id_from_request_metadata(request: ChatRequest) -> str | None:
    md = request.metadata if isinstance(request.metadata, dict) else {}
    v = md.get("thread_id")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _message_uuid_from_request_metadata(request: ChatRequest) -> str | None:
    # Prefer explicit top-level field (recommended).
    v0 = getattr(request, "message_uuid", None)
    if v0 is not None:
        s0 = str(v0).strip()
        if s0:
            return s0
    md = request.metadata if isinstance(request.metadata, dict) else {}
    v = md.get("message_uuid") or md.get("messageUuid") or md.get("messageUUID")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _json_safe(x: Any) -> Any:
    try:
        return json.loads(json.dumps(x, ensure_ascii=False, default=str))
    except Exception:
        return str(x)


def _build_user_chat_message_obj(request: ChatRequest, *, message_uuid: str) -> dict[str, Any]:
    file_obj = request.file if isinstance(request.file, dict) else None
    files = request.files if isinstance(request.files, list) else []
    return {
        "name": (request.user_name or "").strip() or "",
        "role": "user",
        "uuid": message_uuid,
        "email": (request.user_email or "").strip() or None,
        "images": [],
        "content": (request.message or "").strip(),
        "pdfFile": file_obj,
        "pdfFiles": files,
        "userName": (request.user_name or "").strip() or "",
        "timeStamp": _iso_now_z(),
    }


def _assistant_message_content_from_hitl(resp: "ChatResponse") -> str:
    rs = resp.run_state if isinstance(resp.run_state, dict) else {}
    return str(rs.get("last_question") or resp.question or "").strip()


def _build_assistant_chat_message_obj_full(
    *,
    done_evt: dict[str, Any] | None,
    run_meta: dict[str, Any],
    message_uuid: str,
    hitl_response: Optional["ChatResponse"] = None,
) -> dict[str, Any]:
    if hitl_response is not None and str(hitl_response.status or "") == "input_required":
        content = _assistant_message_content_from_hitl(hitl_response)
        done_evt = None
    else:
        content = ""
        if isinstance(done_evt, dict) and isinstance(done_evt.get("content"), str):
            content = str(done_evt.get("content") or "")
    tool_calls = run_meta.get("tool_calls") if isinstance(run_meta, dict) else None
    streamed_tool_events = run_meta.get("streamed_tool_events") if isinstance(run_meta, dict) else None
    agent_logs: list[dict[str, Any]] = []
    if isinstance(streamed_tool_events, list):
        for e in streamed_tool_events:
            if not isinstance(e, dict):
                continue
            agent_logs.append(
                {
                    "ts": _iso_now_z(),
                    "level": "info",
                    "detail": _json_safe(e),
                    "message": f"{e.get('type')}: {e.get('tool')}",
                    "category": "tool",
                    "namespace": [],
                }
            )
    plan_steps: list[str] = []
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                nm = str(tc.get("tool") or tc.get("name") or "").strip()
                if nm:
                    plan_steps.append(f"tool: {nm}")
    agent_plan = {"steps": plan_steps, "summary": plan_steps[0] if plan_steps else ""}

    row: dict[str, Any] = {
        "name": "Process GPT Agent",
        "role": "assistant",
        "uuid": message_uuid,
        "email": "agent:process-gpt-agent",
        "agentId": "process-gpt-agent",
        "content": content,
        "profile": "/images/chat-icon.png",
        "userName": "Process GPT Agent",
        "agentLogs": agent_logs,
        "agentPlan": agent_plan,
        "isLoading": False,
        "timeStamp": _iso_now_z(),
        "toolCalls": tool_calls if isinstance(tool_calls, list) else [],
    }
    if hitl_response is not None and str(hitl_response.status or "") == "input_required":
        ol = (hitl_response.openui_lang or "").strip()
        if ol:
            row["openuiLang"] = ol
        rs_hitl = hitl_response.run_state if isinstance(hitl_response.run_state, dict) else None
        if rs_hitl:
            row["runState"] = _json_safe(rs_hitl)
    return row


def _build_assistant_placeholder_message_obj(*, message_uuid: str) -> dict[str, Any]:
    return {
        "name": "Process GPT Agent",
        "role": "assistant",
        "uuid": message_uuid,
        "email": "agent:process-gpt-agent",
        "agentId": "process-gpt-agent",
        "content": "",
        "profile": "/images/chat-icon.png",
        "userName": "Process GPT Agent",
        "agentLogs": [],
        "agentPlan": {"steps": [], "summary": ""},
        "isLoading": True,
        "timeStamp": _iso_now_z(),
        "toolCalls": [],
    }


async def await_http_chat_stream_runner(task: asyncio.Task[Any]) -> None:
    """SSE 소비 측 취소가 있어도 ``runner``(에이전트 실행·터미널 이벤트 큐 적재)가 끝까지 수행되게 한다."""
    await asyncio.shield(task)


def _build_http_stream_terminal_events(
    *,
    conv_id: str,
    request: ChatRequest,
    outcome: dict[str, Any],
    token_count: int,
    tool_event_count: int,
    streamed_tool_events: list[dict[str, Any]],
    streamed_token_parts: list[str],
    token_preview_parts: list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``run_meta`` + ``done`` 페이로드(SSE ``data`` JSON)를 생성한다. ``/chat/stream`` runner·제너레이터가 공유."""
    meta_payload: dict[str, Any] = {
        "type": "run_meta",
        "conversation_id": conv_id,
        "counts": {
            "tokens": token_count,
            "tool_events": tool_event_count,
        },
        "streamed_tool_events": list(streamed_tool_events),
        "tool_calls": [],
        "skill_calls": [],
        "todos": [],
    }
    try:
        md0 = request.metadata or {}
        todos0 = md0.get("todos") if isinstance(md0, dict) else None
        if isinstance(todos0, list):
            meta_payload["todos"] = todos0
    except Exception:
        pass
    try:
        if "response" in outcome:
            r = outcome["response"]
            tool_calls = getattr(r, "tool_calls", None)
            skill_calls = getattr(r, "skill_calls", None)
            if isinstance(tool_calls, list):
                meta_payload["tool_calls"] = tool_calls
            if isinstance(skill_calls, list):
                meta_payload["skill_calls"] = skill_calls
    except Exception:
        logger.exception("run_meta extraction failed", exc_info=True)

    done_evt: dict[str, Any] | None = None
    if "http_error" in outcome:
        done_err = json.dumps(outcome["http_error"], ensure_ascii=False)
        done_evt = {
            "type": "done",
            "content": ensure_trailing_newline(done_err),
        }
    elif "error" in outcome:
        done_evt = {
            "type": "done",
            "content": ensure_trailing_newline(str(outcome["error"])),
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
            streamed_full = "".join(streamed_token_parts).strip()
            streamed_full = unwrap_output_wrapper_result_text(streamed_full).strip()
            streamed_full = strip_leading_workitem_echo_for_sse(streamed_full).lstrip()
            done_text = streamed_full or "요청 작업을 수행했지만 최종 답변을 생성하지 못했습니다."
        done_text = ensure_trailing_newline(done_text)
        done_evt = {"type": "done", "content": done_text}
        logger.info(
            "[http_chat] stream_done | conv_id=%s tenant_id=%s tokens=%d tool_events=%d preview=%r",
            conv_id,
            (request.tenant_id or "").strip(),
            token_count,
            tool_event_count,
            "".join(token_preview_parts)[:240],
        )

    return meta_payload, done_evt


def _normalized_agent_ids(request: ChatRequest) -> list[str]:
    out: list[str] = []
    for x in request.agent_ids or []:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


async def _resolve_agents(tenant_id: str, request: ChatRequest) -> list[dict[str, Any]]:
    tid = (tenant_id or "").strip()
    if not tid:
        return []
    ids = _normalized_agent_ids(request)
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
        stream_event_emit: Any | None = None,
    ) -> ChatResponse:
        t0 = time.time()
        tenant_id = (request.tenant_id or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id가 필요합니다.")
        conv_id = (request.conversation_id or "").strip() or str(uuid.uuid4())
        # Support clients that embed `[InputData] {json...}` inside `message`.
        # Never fail the request due to parse errors: if parsing fails we keep the original message.
        cleaned_msg, embedded_input = extract_inputdata_from_message(request.message or "")
        if embedded_input is not None:
            merged_payload = merge_inputdata_into_chat_payload(request.model_dump(), embedded_input)
            try:
                request = ChatRequest(**merged_payload)
            except Exception:
                # Should not happen often; never break chat due to InputData.
                logger.exception("InputData payload rebuild failed (ignored)", exc_info=True)
        # Keep the user-facing instruction clean (without the big JSON blob).
        request = request.model_copy(update={"message": cleaned_msg})
        msg = (request.message or "").strip()
        logger.info(
            "[http_chat] request received | conv_id=%s tenant_id=%s stream=%s msg_len=%d",
            conv_id,
            tenant_id,
            bool(stream_emit),
            len(msg),
        )

        agents = await _resolve_agents(tenant_id, request)
        if not agents:
            raise HTTPException(
                status_code=400,
                detail="테넌트에 사용 가능한 에이전트(is_agent)가 없습니다. 요청의 agent_ids로 지정하세요.",
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
            extras["model"] = normalize_litellm_slash_model_name(meta["model"].strip()) or meta["model"].strip()
        # HITL 재개: 클라이언트가 직전 `input_required` 응답의 run_state·답변을 metadata로 되돌려준다.
        if isinstance(meta.get("run_state"), dict):
            extras["run_state"] = dict(meta["run_state"])
        hr_in = meta.get("human_response_answer")
        if isinstance(hr_in, str) and hr_in.strip():
            extras["human_response_answer"] = hr_in.strip()
        # UI ask_user 응답(권장):
        # - question_id(or correlation_id) 로 원 요청 매칭 가능해야 함
        # - answer 는 최소 포함
        # - approve_reject_with_edit 에서는 reason(optional) 허용
        try:
            ask_resp = meta.get("ask_user_response")
            if not isinstance(ask_resp, dict):
                ou = meta.get("openui_hitl_response")
                ask_resp = ou if isinstance(ou, dict) else None
            if not isinstance(ask_resp, dict):
                # Also allow flat metadata {question_id, answer, reason}
                if any(k in meta for k in ("question_id", "answer", "reason", "correlation_id")):
                    ask_resp = meta
            if isinstance(ask_resp, dict):
                qid = str(ask_resp.get("question_id") or ask_resp.get("correlation_id") or "").strip()
                ans = ask_resp.get("answer")
                rs0 = ask_resp.get("run_state") if isinstance(ask_resp.get("run_state"), dict) else None
                if rs0 is not None and "run_state" not in extras:
                    extras["run_state"] = dict(rs0)
                if qid and "run_state" not in extras:
                    rs_cached = resolve_cached_run_state_for_question_id(qid)
                    if isinstance(rs_cached, dict):
                        extras["run_state"] = dict(rs_cached)
                if isinstance(ans, str) and ans.strip() and "human_response_answer" not in extras:
                    reason = ask_resp.get("reason")
                    if isinstance(reason, str) and reason.strip():
                        extras["human_response_answer"] = json.dumps(
                            {"answer": ans.strip(), "reason": reason.strip()},
                            ensure_ascii=False,
                        )
                    else:
                        extras["human_response_answer"] = ans.strip()
        except Exception:
            # Never fail chat due to optional UI metadata.
            logger.exception("HTTP chat: failed to parse ask_user_response metadata (ignored)", exc_info=True)
        # 대화 맥락: 같은 방의 최근 대화 이력(문자열)을 messages[]로 파싱해 전달한다.
        # (DeepAgentsExecutor가 이를 state_input["messages"]에 합칠 수 있도록 extras에 저장)
        rrh = meta.get("room_recent_history")
        if isinstance(rrh, str) and rrh.strip():
            extras["room_recent_messages"] = parse_room_recent_history_to_messages(rrh)

        # HTTP chat token hygiene: provide a best-effort work-assistant tool allowlist hint.
        # This reduces root tool schema exposure without adding extra model calls.
        try:
            norm = re.sub(r"\s+", " ", (msg or "").strip().lower())
            allow: list[str] = []
            if any(k in norm for k in ("조직도", "조직", "organization", "org tree", "orgtree")):
                allow.append("get_organization")
            if any(k in norm for k in ("프로세스 목록", "프로세스 리스트", "프로세스 list", "process list")):
                allow.append("get_process_list")
            if any(k in norm for k in ("인스턴스", "실행 목록", "todolist", "할 일", "todo")):
                allow.extend(["get_instance_list", "get_todolist"])
            if any(k in norm for k in ("프로세스 상세", "정의", "definition", "detail")):
                allow.append("get_process_detail")
            if allow:
                extras["http_chat_work_assistant_allowlist"] = list(dict.fromkeys(allow))
        except Exception:
            logger.exception("http_chat allowlist hint generation failed (ignored)", exc_info=True)

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
        if stream_event_emit is not None:
            # tool/plan 이벤트를 SSE로 보내기 위한 emitter (sync callable)
            # - DeepAgentsExecutor 내부에서 호출되며, http_chat이 루프에 안전하게 전달한다.
            extras["_http_chat_stream_event_emit"] = stream_event_emit

        set_context(
            task_id=conv_id,
            proc_inst_id=conv_id,
            crew_type="deepagents",
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
            # openui_lang은 딥 에이전트가 HITL 도구 호출 시 생성해 run_state로 내려주는 것이 기본이다.
            # (없으면 최소 템플릿으로 fallback)
            ctx_text = resolve_context_text(run_state=rs if isinstance(rs, dict) else None)
            openui_lang = build_openui_lang_for_hitl(
                question=qtext,
                context=ctx_text,
                run_state=rs if isinstance(rs, dict) else None,
            )
            rs_out = run_state_without_openui_lang(rs if isinstance(rs, dict) else None)
            return ChatResponse(
                response="",
                conversation_id=conv_id,
                tool_calls=[],
                skill_calls=[],
                status="input_required",
                question=qtext,
                run_state=rs_out,
                openui_lang=openui_lang,
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
            skill_calls=list(result_box.get("skill_calls") or []),
            status="completed",
        )

    @app.post("/chat", response_model=ChatResponse)
    async def chat(http_request: Request, request: ChatRequest) -> ChatResponse:
        # 헤더 JWT를 바디 user_jwt로 보강(이미 있으면 그대로 둠)
        if not (request.user_jwt or "").strip():
            hj = _extract_bearer_jwt(http_request)
            if hj:
                request = request.model_copy(update={"user_jwt": hj})
        conv_id = (request.conversation_id or "").strip() or str(uuid.uuid4())
        tenant_id = (request.tenant_id or "").strip()
        has_file = isinstance(request.file, dict) or (isinstance(request.files, list) and len(request.files) > 0)
        msg_uuid0 = _message_uuid_from_request_metadata(request)
        try:
            await a_upsert_chat_room_start(
                tenant_id=tenant_id,
                room_id=conv_id,
                participants=[
                    {
                        "id": (request.user_uid or "").strip() or None,
                        "email": (request.user_email or "").strip() or None,
                        "profile": "/images/defaultUser.png",
                        "is_agent": None,
                        "username": (request.user_name or "").strip() or None,
                        "agent_type": None,
                    },
                    {
                        "id": "process-gpt-agent",
                        "email": None,
                        "profile": "/images/chat-icon.png",
                        "is_agent": True,
                        "username": "Process GPT Agent",
                        "agent_type": "agent",
                    },
                ],
                room_name=(request.message or "").strip() or None,
                primary_agent_id=None,
                context=build_initial_chat_room_context(has_file=has_file),
            )
            await a_insert_chat_attachments(
                tenant_id=tenant_id,
                room_id=conv_id,
                user_name=(request.user_name or "").strip(),
                file=request.file if isinstance(request.file, dict) else None,
                files=request.files if isinstance(request.files, list) else None,
            )
            user_msg_uuid = msg_uuid0 or str(uuid.uuid4())
            await a_insert_chat_message(
                tenant_id=tenant_id,
                room_id=conv_id,
                thread_id=_thread_id_from_request_metadata(request),
                message_obj=_build_user_chat_message_obj(request, message_uuid=user_msg_uuid),
                message_uuid=user_msg_uuid,
            )
        except Exception:
            logger.exception("HTTP chat(/chat): start DB writes failed (continuing)", exc_info=True)

        resp = await _run_chat(request.model_copy(update={"conversation_id": conv_id}), stream_emit=None, stream_event_emit=None)
        try:
            asst_uuid = str(uuid.uuid4())
            run_meta = {
                "type": "run_meta",
                "conversation_id": conv_id,
                "tool_calls": resp.tool_calls,
                "skill_calls": resp.skill_calls,
                "streamed_tool_events": [],
            }
            if str(resp.status or "") == "input_required":
                await a_insert_chat_message(
                    tenant_id=tenant_id,
                    room_id=conv_id,
                    thread_id=_thread_id_from_request_metadata(request),
                    message_obj=_build_assistant_chat_message_obj_full(
                        done_evt=None,
                        run_meta=run_meta,
                        message_uuid=asst_uuid,
                        hitl_response=resp,
                    ),
                    message_uuid=asst_uuid,
                )
                last_preview = _assistant_message_content_from_hitl(resp) or "입력이 필요합니다."
                await a_update_chat_room_last_message(
                    tenant_id=tenant_id,
                    room_id=conv_id,
                    message={"msg": last_preview, "type": "text", "createdAt": _iso_now_z()},
                )
            else:
                done_evt = {"type": "done", "content": ensure_trailing_newline(str(resp.response or ""))}
                await a_insert_chat_message(
                    tenant_id=tenant_id,
                    room_id=conv_id,
                    thread_id=_thread_id_from_request_metadata(request),
                    message_obj=_build_assistant_chat_message_obj_full(
                        done_evt=done_evt, run_meta=run_meta, message_uuid=asst_uuid
                    ),
                    message_uuid=asst_uuid,
                )
                await a_update_chat_room_last_message(
                    tenant_id=tenant_id,
                    room_id=conv_id,
                    message={"msg": str(resp.response or ""), "type": "text", "createdAt": _iso_now_z()},
                )
        except Exception:
            logger.exception("HTTP chat(/chat): completion DB writes failed (ignored)", exc_info=True)
        return resp

    @app.post("/chat/stream")
    async def chat_stream(http_request: Request, request: ChatRequest) -> EventSourceResponse:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        conv_id = (request.conversation_id or "").strip() or str(uuid.uuid4())
        outcome: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        token_count = 0
        tool_event_count = 0
        token_preview_parts: list[str] = []
        streamed_token_parts: list[str] = []
        # runner의 run_meta·DB persist와 동기화(큐 소비 전에도 스냅샷 가능)
        captured_tool_events: list[dict[str, Any]] = []

        tenant_id = (request.tenant_id or "").strip()
        has_file = isinstance(request.file, dict) or (isinstance(request.files, list) and len(request.files) > 0)
        # 요청 시작 시점 DB 반영(방 보장 → 첨부 → user 메시지). 실패해도 채팅 실행은 계속.
        msg_uuid0 = _message_uuid_from_request_metadata(request)
        assistant_row_uuid: str | None = None
        assistant_msg_obj: dict[str, Any] | None = None
        last_assistant_db_flush_t = 0.0
        try:
            await a_upsert_chat_room_start(
                tenant_id=tenant_id,
                room_id=conv_id,
                participants=[
                    {
                        "id": (request.user_uid or "").strip() or None,
                        "email": (request.user_email or "").strip() or None,
                        "profile": "/images/defaultUser.png",
                        "is_agent": None,
                        "username": (request.user_name or "").strip() or None,
                        "agent_type": None,
                    },
                    {
                        "id": "process-gpt-agent",
                        "email": None,
                        "profile": "/images/chat-icon.png",
                        "is_agent": True,
                        "username": "Process GPT Agent",
                        "agent_type": "agent",
                    },
                ],
                room_name=(request.message or "").strip() or None,
                primary_agent_id=None,
                context=build_initial_chat_room_context(has_file=has_file),
            )
            await a_insert_chat_attachments(
                tenant_id=tenant_id,
                room_id=conv_id,
                user_name=(request.user_name or "").strip(),
                file=request.file if isinstance(request.file, dict) else None,
                files=request.files if isinstance(request.files, list) else None,
            )
            user_msg_uuid = msg_uuid0 or str(uuid.uuid4())
            await a_insert_chat_message(
                tenant_id=tenant_id,
                room_id=conv_id,
                thread_id=_thread_id_from_request_metadata(request),
                message_obj=_build_user_chat_message_obj(request, message_uuid=user_msg_uuid),
                message_uuid=user_msg_uuid,
            )
            # Assistant placeholder row: inserted first, then updated progressively.
            assistant_row_uuid = str(uuid.uuid4())
            assistant_msg_obj = _build_assistant_placeholder_message_obj(message_uuid=assistant_row_uuid)
            await a_insert_chat_message(
                tenant_id=tenant_id,
                room_id=conv_id,
                thread_id=_thread_id_from_request_metadata(request),
                message_obj=assistant_msg_obj,
                message_uuid=assistant_row_uuid,
            )
        except Exception:
            logger.exception("HTTP chat: start DB writes failed (continuing)", exc_info=True)

        async def emit_token(delta: str) -> None:
            nonlocal token_count
            nonlocal last_assistant_db_flush_t
            if not delta:
                return
            token_count += 1
            if len(token_preview_parts) < 12:
                token_preview_parts.append(delta)
            # Keep the full streamed token sequence so we can reconstruct the final answer
            # even when the graph fails to produce a usable ChatResponse.response.
            streamed_token_parts.append(delta)
            await q.put({"type": "token", "content": delta})
            # Progressive DB update for assistant placeholder (best-effort, throttled).
            if assistant_row_uuid and assistant_msg_obj is not None:
                now_t = time.time()
                if now_t - last_assistant_db_flush_t >= 0.4:
                    last_assistant_db_flush_t = now_t
                    try:
                        assistant_msg_obj["content"] = "".join(streamed_token_parts)
                        assistant_msg_obj["isLoading"] = True
                        asyncio.create_task(
                            a_update_chat_message(
                                tenant_id=tenant_id,
                                message_uuid=assistant_row_uuid,
                                message_obj=assistant_msg_obj,
                            )
                        )
                    except Exception:
                        # Never break streaming due to DB update.
                        pass

        def emit_tool_event(evt: dict[str, Any]) -> None:
            """tool_* 이벤트는 tool 스레드에서 호출될 수 있으므로 루프에 안전하게 전달."""
            nonlocal tool_event_count
            if not isinstance(evt, dict):
                return
            et = str(evt.get("type") or "")
            if et in ("tool_start", "tool_end"):
                tool_event_count += 1
                try:
                    captured_tool_events.append(dict(evt))
                except Exception:
                    pass
            # Cross-thread 안전: run_coroutine_threadsafe로 큐 put 보장.
            try:
                asyncio.run_coroutine_threadsafe(q.put(evt), loop)
            except Exception:
                # 마지막 수단: 같은 루프/스레드면 직접 await 불가하므로 task로 스케줄.
                try:
                    asyncio.create_task(q.put(evt))
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
                # token/tool 이벤트를 DeepAgentsExecutor로 전달
                # - token: async (그래프 values 스트림에서 await)
                # - tool: sync (도구 호출 가드/훅에서 call_soon_threadsafe)
                outcome["response"] = await _run_chat(
                    req2,
                    stream_emit=emit_token,
                    stream_event_emit=emit_tool_event,
                )
            except HTTPException as he:
                outcome["http_error"] = {"status_code": he.status_code, "detail": he.detail}
            except Exception as e:
                outcome["error"] = str(e)
                logger.exception("HTTP chat stream runner failed")
            finally:
                # 클라이언트가 먼저 끊겨도(SSE 취소) 터미널 이벤트를 큐에 넣어 runner 쪽에서 일관되게 마친다.
                try:
                    hitl_input_required = False
                    # HITL: streaming 경로에서도 input_required 상태를 노출한다.
                    # - /chat(비스트리밍)은 ChatResponse(status/question/run_state)를 그대로 반환하지만
                    #   /chat/stream은 done 텍스트만 내보내기 때문에 HITL이 가려질 수 있다.
                    # - 따라서 별도 SSE 이벤트를 추가로 발행한다(호환성: 기존 클라이언트는 무시 가능).
                    try:
                        r0 = outcome.get("response")
                        if isinstance(r0, ChatResponse) and str(r0.status or "") == "input_required":
                            hitl_input_required = True
                            ou_canonical = (r0.openui_lang or "").strip()
                            await q.put(
                                {
                                    "type": "input_required",
                                    "conversation_id": conv_id,
                                    "question": str(r0.question or ""),
                                    "run_state": run_state_without_openui_lang(
                                        r0.run_state if isinstance(r0.run_state, dict) else None
                                    ),
                                    # expand_input_required_to_openui_hitl_events: run_state에 원문이 없을 때
                                    # DB/ChatResponse와 동일한 Lang으로 SSE end를 맞추기 위함
                                    **({"openui_lang": ou_canonical} if ou_canonical else {}),
                                }
                            )
                    except Exception:
                        logger.exception("HTTP chat stream: failed to emit input_required event", exc_info=True)

                    meta_payload, done_evt = _build_http_stream_terminal_events(
                        conv_id=conv_id,
                        request=request,
                        outcome=outcome,
                        token_count=token_count,
                        tool_event_count=tool_event_count,
                        streamed_tool_events=captured_tool_events,
                        streamed_token_parts=streamed_token_parts,
                        token_preview_parts=token_preview_parts,
                    )
                    # HITL(input_required) 케이스에서는 스트림 종료는 유지하되 done 이벤트는 발행하지 않는다.
                    # (OpenUI HITL 이벤트 스트림이 별도로 내려가며, done은 클라이언트에서 "완료"로 오해될 수 있음)
                    if hitl_input_required:
                        done_evt = None
                    await q.put(meta_payload)
                    if done_evt is not None:
                        await q.put(done_evt)
                    # 완료 시점 DB 반영(assistant 메시지 + room last message).
                    try:
                        r_db = outcome.get("response")
                        # Finalize the existing assistant placeholder row when available.
                        if assistant_row_uuid:
                            if hitl_input_required and isinstance(r_db, ChatResponse):
                                final_obj = _build_assistant_chat_message_obj_full(
                                    done_evt=None,
                                    run_meta=meta_payload,
                                    message_uuid=assistant_row_uuid,
                                    hitl_response=r_db,
                                )
                            else:
                                final_obj = _build_assistant_chat_message_obj_full(
                                    done_evt=done_evt,
                                    run_meta=meta_payload,
                                    message_uuid=assistant_row_uuid,
                                )
                            # Preserve streamed partial text only for normal completion (not HITL).
                            if not hitl_input_required:
                                if (
                                    (not isinstance(done_evt, dict) or not str(done_evt.get("content") or "").strip())
                                    and assistant_msg_obj is not None
                                    and isinstance(assistant_msg_obj.get("content"), str)
                                ):
                                    final_obj["content"] = assistant_msg_obj.get("content") or ""
                            final_obj["isLoading"] = False
                            await a_update_chat_message(
                                tenant_id=(request.tenant_id or "").strip(),
                                message_uuid=assistant_row_uuid,
                                message_obj=final_obj,
                            )
                        else:
                            asst_uuid = str(uuid.uuid4())
                            if hitl_input_required and isinstance(r_db, ChatResponse):
                                msg_obj = _build_assistant_chat_message_obj_full(
                                    done_evt=None,
                                    run_meta=meta_payload,
                                    message_uuid=asst_uuid,
                                    hitl_response=r_db,
                                )
                            else:
                                msg_obj = _build_assistant_chat_message_obj_full(
                                    done_evt=done_evt,
                                    run_meta=meta_payload,
                                    message_uuid=asst_uuid,
                                )
                            await a_insert_chat_message(
                                tenant_id=(request.tenant_id or "").strip(),
                                room_id=conv_id,
                                thread_id=_thread_id_from_request_metadata(request),
                                message_obj=msg_obj,
                                message_uuid=asst_uuid,
                            )
                        if hitl_input_required and isinstance(r_db, ChatResponse):
                            last_preview = _assistant_message_content_from_hitl(r_db) or "입력이 필요합니다."
                            await a_update_chat_room_last_message(
                                tenant_id=(request.tenant_id or "").strip(),
                                room_id=conv_id,
                                message={
                                    "msg": last_preview,
                                    "type": "text",
                                    "createdAt": _iso_now_z(),
                                },
                            )
                        elif isinstance(done_evt, dict) and isinstance(done_evt.get("content"), str):
                            await a_update_chat_room_last_message(
                                tenant_id=(request.tenant_id or "").strip(),
                                room_id=conv_id,
                                message={
                                    "msg": str(done_evt.get("content") or ""),
                                    "type": "text",
                                    "createdAt": _iso_now_z(),
                                },
                            )
                    except Exception:
                        logger.exception("HTTP chat: completion DB writes failed (continuing)", exc_info=True)
                except Exception:
                    logger.exception("HTTP chat stream terminal enqueue failed", exc_info=True)
                await q.put({"type": "_internal_done"})

        async def event_generator():
            # Save SSE events to workspace logs for easier debugging.
            # Format: one JSON object per line (jsonl), matching the SSE `data` payloads.
            sse_log_fp: Any = None
            sse_log_path = ""
            try:
                day = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
                base_dir = os.path.join(os.getcwd(), "logs", day)
                os.makedirs(base_dir, exist_ok=True)
                sse_log_path = os.path.join(base_dir, f"chat-stream-{conv_id}.jsonl")
                sse_log_fp = open(sse_log_path, "a", encoding="utf-8")
            except Exception:
                sse_log_fp = None
                sse_log_path = ""

            def _log_evt(evt: dict[str, Any]) -> None:
                if sse_log_fp is None:
                    return
                try:
                    sse_log_fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
                    sse_log_fp.flush()
                except Exception:
                    # Never break streaming due to logging issues.
                    pass

            # 표준 SSE 형식 (요구사항):
            # - event 필드 없이 data만 사용 (event: ... 라인 제거)
            # - 매 메시지는 data: {JSON 한 줄} 1개만 전송
            # - 프롬프트/프리플라이트 텍스트는 보내지 않음
            # - 토큰·예정(plan_*)·실행(tool_*)·run_meta·done 순 (plan_* 는 호출 직전 예고)
            meta_evt = {"type": "meta", "conversation_id": conv_id}
            _log_evt(meta_evt)
            yield {"data": json.dumps(meta_evt, ensure_ascii=False)}
            task = asyncio.create_task(runner())
            try:
                while True:
                    evt = await q.get()
                    if evt.get("type") == "_internal_done":
                        break
                    if str(evt.get("type") or "") == "input_required":
                        for e in expand_input_required_to_openui_hitl_events(evt) or []:
                            try:
                                await a_update_chat_room_context(
                                    tenant_id=tenant_id,
                                    room_id=conv_id,
                                    evt=e,
                                    has_file=has_file,
                                )
                            except Exception:
                                logger.exception("HTTP chat: context DB update failed (ignored)", exc_info=True)
                            _log_evt(e)
                            yield {"data": json.dumps(e, ensure_ascii=False)}
                        continue
                    try:
                        await a_update_chat_room_context(
                            tenant_id=tenant_id,
                            room_id=conv_id,
                            evt=evt,
                            has_file=has_file,
                        )
                    except Exception:
                        logger.exception("HTTP chat: context DB update failed (ignored)", exc_info=True)
                    _log_evt(evt)
                    yield {"data": json.dumps(evt, ensure_ascii=False)}
            finally:
                await await_http_chat_stream_runner(task)
                # 연결이 먼저 끊기면 위 while이 돌지 않아, runner가 finally에서 넣은 run_meta/done이 큐에만 남는다.
                while True:
                    try:
                        pending = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if pending.get("type") == "_internal_done":
                        break
                    if str(pending.get("type") or "") == "input_required":
                        for e in expand_input_required_to_openui_hitl_events(pending) or []:
                            try:
                                await a_update_chat_room_context(
                                    tenant_id=tenant_id,
                                    room_id=conv_id,
                                    evt=e,
                                    has_file=has_file,
                                )
                            except Exception:
                                logger.exception(
                                    "HTTP chat: context DB update (flush) failed (ignored)", exc_info=True
                                )
                            _log_evt(e)
                        continue
                    try:
                        await a_update_chat_room_context(
                            tenant_id=tenant_id,
                            room_id=conv_id,
                            evt=pending,
                            has_file=has_file,
                        )
                    except Exception:
                        logger.exception("HTTP chat: context DB update (flush) failed (ignored)", exc_info=True)
                    _log_evt(pending)
            try:
                if sse_log_fp is not None:
                    sse_log_fp.close()
                    if sse_log_path:
                        logger.info("[http_chat] sse_log_saved | conv_id=%s path=%s", conv_id, sse_log_path)
            except Exception:
                pass

        # ping은 ": ping ..." 주석 라인으로 출력되어 data-only 파서에 방해가 될 수 있어 사실상 비활성화한다.
        # (sse-starlette 구현은 ping이 falsy면 기본값으로 되돌릴 수 있어, 매우 큰 값으로 설정)
        return EventSourceResponse(event_generator(), ping=10**9)

    return app
