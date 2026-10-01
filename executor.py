from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
import asyncio
from typing import Any, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.types import TaskState
from google.protobuf.json_format import ParseDict

from processgpt_agent_sdk.artifacts import download_file
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Overwrite
from typing_extensions import override

from core.agents.agent import build_agent
from core.agents.steering import SteeringMiddleware
from core.bpm.tools import set_process_context
from core.storage.checkpointer import (
    get_async_postgres_checkpointer,
    get_shared_memory_checkpointer,
    resolve_checkpoint_thread_id,
)
from core.chat.hitl import (
    extract_interrupt_payload,
    find_interrupt_payload,
    is_duplicate_hitl_request,
    normalize_hitl_payload,
)
from processgpt_agent_sdk.chat_registry import get_run_registry
from processgpt_agent_sdk.chat_mode import persist_chat_to_db
from processgpt_agent_sdk.steering import (
    REASON_AWAITING_HUMAN_INPUT,
    SteerDirective,
    SteerResult,
)

from core.chat.callbacks import ToolTrackingCallback
from core.agents.input_builder import (
    build_graph_inputs,
    build_user_message,
    collect_recent_messages,
    extract_skill_command,
    resolve_chat_room_id,
)
from core.chat.activity import (
    fetch_child_activity_todo,
    load_activity_capabilities,
    load_activity_root_agent,
    resolve_root_agent,
    load_subprocess_subagents,
)
from core.chat.chat_room import (
    CtxAccumulator,
    a_set_chat_room_context,
    a_update_chat_room_context,
    a_upsert_chat_room_start,
    build_initial_chat_room_context,
)
from core.chat.event_tap import EventTapStreamer
from core.chat.tool_arg_stream import ToolArgTextStreamer
from core.llm.mcp import ensure_process_gpt_mcp, load_chat_root_tools
from core.skills.git_skill_fetcher import prefetch_git_skills
from core.skills.local_system_skills import seed_local_system_skills
from core.skills.skills import resolve_slash_skill
from core.skills.tools import _skills_from_tool_args, _current_tenant_id as _skill_tenant_id, _current_user_id as _skill_user_id
from core.utils.output_utils import extract_final_text, extract_json_text
from core.llm.token_hygiene import ToolResultTruncationMiddleware
from core.agents.skill_creator_subagents import build_skill_creator_subagents
from core.deterministic import MODE_UNDO_PENDING, try_deterministic_execution
# 우리 기능(프로세스 생성 HITL 캡처/이름도출)에서 사용하는 contextvar — incoming 에는 없으나 유지.
from core.skills.tools import _current_conversation_id as _skill_conversation_id
from core.skills.tools import _current_user_request as _skill_user_request
from core.skills.tools import _bpmn_work_dir
from core.skills.tools import bpmn_container_base as _bpmn_container_base
from core.skills.tools import checkout_container_base as _checkout_container_base
from core.sandbox.docker_sandbox import set_workspace_scope
from core.storage.storage import (
    download_workspace_directory,
    set_tenant_id as _set_storage_tenant_id,
    upload_workspace_directory,
)

logger = logging.getLogger(__name__)

# deepagent 가 샌드박스 workspace 에 생성/수정하는 산출물 파일을 프론트(Claude Desktop식 파일 UI)로
# 실시간 전달하기 위한 설정. write_file/edit_file 도구가 건드린 /workspace 하위 파일을 file_artifact 로 보낸다.
# 에이전트에게 바인딩된 파일 도구는 deepagents 기본 도구뿐이다(프로세스 정의 전용
# write/update 도구는 더 이상 부착하지 않는다 — core/agents/agent.py 참고).
_FILE_TOOL_NAMES = ("write_file", "edit_file")
_FILE_ARTIFACT_MAX_BYTES = 400_000  # 미리보기에 과한 대용량 파일은 잘라 보냄

# 스킬 평가(test-runner/evaluator) 실행 산출물 디렉터리. 회차마다 outputs/·grading.json 이
# 수십 개씩 쌓이는 내부 작업 파일이라 사용자 산출물 패널에 띄우지 않는다 — 결과 요약은
# complete_skill_creation 이 DB(skill_eval_runs/_results)에 남긴다.
_EVAL_RUN_PATH_MARKER = "/.skill-evals/"

# 채팅 산출물에서 스킬이 만든 hwpx 컨테이너 경로를 잡아내기 위한 패턴.
_HWPX_PATH_RE = re.compile(r"/workspace/[^\s\"']+\.hwpx")

# 폼 출력에서 샌드박스 바이너리 파일(pdf/docx 등) 경로를 감지하기 위한 패턴.
_WS_BINARY_FILE_RE = re.compile(
    r"/workspace/[^\s\"']+\.(?:pdf|docx?|xlsx?|pptx?)",
    re.IGNORECASE,
)


def _validation_failure_summary(failures: list[dict], max_items: int = 3) -> str:
    """검증 실패 목록을 사용자에게 보여줄 짧은 불릿으로 요약한다."""
    lines: list[str] = []
    for failure in failures:
        for defect in (failure.get("defects") or [])[:max_items]:
            if not isinstance(defect, dict):
                continue
            detail = str(defect.get("detail") or defect.get("type") or "").strip()
            if detail:
                lines.append(f"- {detail}")
    if not lines:
        return "- 정의 구조 검사에서 결함이 남아 있습니다."
    return "\n".join(lines[:max_items])


def _ws_file_meta(path: str) -> tuple[str, str]:
    """container 경로 → (파일명, 확장자 소문자)."""
    from pathlib import PurePosixPath

    p = PurePosixPath(str(path or ""))
    return p.name, p.suffix.lower()


async def _read_ws_file_content(tenant_id: str, path: str) -> str | None:
    """샌드박스 backend 로 workspace 파일 내용을 읽어 문자열로 반환(실패 시 None)."""
    try:
        from core.agents.agent import _BASE_DIR as _AGENT_BASE_DIR, _SKILLS_DIR as _AGENT_SKILLS_DIR
        from core.sandbox.docker_sandbox import get_or_create_sandbox

        backend = get_or_create_sandbox(
            tenant_id,
            workspace_host=_AGENT_BASE_DIR / "workspace",
            skills_host=_AGENT_SKILLS_DIR,
        )
        results = await asyncio.to_thread(backend.download_files, [path])
        if results:
            res = results[0]
            content = getattr(res, "content", None)
            if content is not None and not getattr(res, "error", None):
                return content.decode("utf-8", errors="replace")
    except Exception:
        logger.warning("file_artifact: workspace 파일 읽기 실패 path=%s", path, exc_info=True)
    return None


def _office_mcp_url() -> str:
    """office-mcp streamable-http 엔드포인트 URL (deep-research 와 동일 env 사용)."""
    raw = (os.environ.get("PROCESS_GPT_OFFICE_MCP_URL") or "").strip()
    if not raw:
        raw = "http://localhost:1192/mcp/"
    # /mcp 경로 보장 + 트레일링 슬래시(streamable-http)
    if not raw.rstrip("/").endswith("/mcp"):
        raw = raw.rstrip("/") + "/mcp"
    if not raw.endswith("/"):
        raw = raw + "/"
    return raw


async def _call_office_mcp_tool(tool: str, arguments: dict) -> dict | None:
    """office-mcp 도구를 JSON-RPC(tools/call)로 호출하고 결과 dict 를 반환한다(실패 시 None).

    office-mcp(main.py)는 stateless_http + json_response 이므로 프론트처럼 bare POST 로 호출 가능.
    """
    import httpx

    url = _office_mcp_url()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    try:
        # office-mcp(streamable-http)는 /mcp/ → /mcp 로 307 리다이렉트할 수 있어 반드시 따라가야 한다
        # (httpx 는 기본적으로 리다이렉트를 안 따라감 → raise_for_status 에서 터짐).
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            r = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.warning("office-mcp 호출 실패 | tool=%s url=%s", tool, url, exc_info=True)
        return None

    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        logger.warning("office-mcp 응답 형식 이상 | tool=%s data=%.200s", tool, str(data))
        return None
    if isinstance(result.get("structuredContent"), dict):
        sc = result["structuredContent"]
        # FastMCP 는 dict 반환을 {"result": {...}} 로 감싸기도 한다.
        return sc.get("result") if isinstance(sc.get("result"), dict) else sc
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c.get("text") or "")
            except Exception:
                continue
    return None


async def _materialize_hwpx_form_outputs(tenant_id: str, final_text: str) -> str:
    """worklist 폼 출력 JSON 안의 샌드박스 hwpx 경로(/workspace/*.hwpx)를 실제 문서로 실체화한다.

    스킬(hwpx-writer)이 만든 .hwpx 는 Docker 샌드박스 안에만 있고 폼값엔 컨테이너 경로만 담긴다.
    그 파일을 꺼내 base64 로 office-mcp(store_and_render_hwpx)에 넘겨 storage 업로드 + HTML 변환을
    시키고(=deepagents 는 storage 직접 미접근), 폼값을 {path:file_url, name, html_url, ext} 로 바꾼다.
    이러면 프론트 FileField 다운로드 + HwpxViewer 미리보기/편집(html_url·file_url) 이 그대로 동작한다.
    실패는 베스트에포트(원본 경로 유지)."""
    if not final_text or ".hwpx" not in final_text.lower():
        return final_text
    try:
        obj = json.loads(final_text)
    except Exception:
        return final_text
    if not isinstance(obj, (dict, list)):
        return final_text

    try:
        from core.agents.agent import _BASE_DIR as _AGENT_BASE_DIR, _SKILLS_DIR as _AGENT_SKILLS_DIR
        from core.sandbox.docker_sandbox import get_or_create_sandbox
    except Exception:
        logger.warning("hwpx 실체화: sandbox 모듈 로드 실패", exc_info=True)
        return final_text
    from pathlib import PurePosixPath

    backend = get_or_create_sandbox(
        tenant_id,
        workspace_host=_AGENT_BASE_DIR / "workspace",
        skills_host=_AGENT_SKILLS_DIR,
    )
    cache: dict[str, dict] = {}

    async def _resolve(path_val: str) -> dict | None:
        container_path = path_val if path_val.startswith("/") else "/" + path_val
        if not container_path.startswith("/workspace"):
            return None
        if container_path in cache:
            return cache[container_path]
        results = await asyncio.to_thread(backend.download_files, [container_path])
        res = results[0] if results else None
        content = getattr(res, "content", None) if res else None
        if content is None or getattr(res, "error", None):
            logger.warning("hwpx 실체화: 샌드박스에 파일 없음 | path=%s", container_path)
            return None
        name = PurePosixPath(container_path).name
        b64 = base64.b64encode(content).decode("ascii")
        out = await _call_office_mcp_tool(
            "store_and_render_hwpx",
            {"base64_data": b64, "file_name": name, "tenant_id": tenant_id or ""},
        )
        if not out or not out.get("file_url"):
            logger.warning("hwpx 실체화: office-mcp 결과 없음 | path=%s", container_path)
            return None
        val = {
            "path": out.get("file_url"),
            # 이름은 원래 것을 쓴다. 저장소가 돌려주는 이름은 아스키로 추려져
            # '주차장_안전_안내문.hwpx' 가 'edited-_20260918_010111.hwpx' 로 남는다.
            # 사람이 보는 이름은 무엇인지를 말해야 한다.
            "name": name or out.get("file_name"),
            "html_url": out.get("html_url") or "",
            "ext": ".hwpx",
        }
        cache[container_path] = val
        logger.info("hwpx 실체화 완료 | %s → file_url=%s html_url=%s", container_path, val["path"], val["html_url"])
        return val

    async def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                node[k] = await _walk(v)
            return node
        if isinstance(node, list):
            return [await _walk(x) for x in node]
        if isinstance(node, str):
            s = node.strip()
            if s.lower().endswith(".hwpx") and ("workspace/" in s or s.startswith("/workspace")):
                r = await _resolve(s)
                return r if r is not None else node
        return node

    try:
        obj = await _walk(obj)
    except Exception:
        logger.warning("hwpx 실체화: JSON 순회 실패", exc_info=True)
        return final_text
    return json.dumps(obj, ensure_ascii=False)


async def _materialize_ws_file_form_outputs(
    tenant_id: str, final_text: str, conversation_id: str = "",
) -> str:
    """worklist 폼 출력 JSON 안의 샌드박스 바이너리 파일 경로를 받을 수 있는 주소로 바꾼다.

    예전에는 이 서버 메모리(또는 Redis)에 담고 한 시간짜리 토큰 주소를 폼값에 박았다.
    그 시간이 지나거나 파드가 바뀌면 끝이었다 — 결재선을 타고 며칠 뒤에 열어 본 사람은
    빈손이었다. 채팅 산출물에서 이미 겪은 것과 같은 일이다.

    지금은 채팅과 **같은 규약**을 탄다(`core/chat/artifacts.py`). 비공개 버킷에 보관하고
    폼값에 서명 주소와 함께 `file_id` 를 싣는다 — 주소가 죽어도 파일은 남아 있고,
    화면이 그 열쇠로 새 주소를 받아 간다.

    폼값은 {path, name, ext, file_id, url_expires_at} 가 된다. `path` 는 그대로라
    프론트 FileField 다운로드는 손대지 않아도 동작한다.
    실패는 베스트에포트(원본 경로 유지)."""
    if not final_text or not _WS_BINARY_FILE_RE.search(final_text):
        return final_text
    try:
        obj = json.loads(final_text)
    except Exception:
        return final_text
    if not isinstance(obj, (dict, list)):
        return final_text

    try:
        from core.agents.agent import _BASE_DIR as _AGENT_BASE_DIR, _SKILLS_DIR as _AGENT_SKILLS_DIR
        from core.sandbox.docker_sandbox import get_or_create_sandbox
        from core.chat.artifacts import publish_one
    except Exception:
        logger.warning("ws 파일 실체화: 모듈 로드 실패", exc_info=True)
        return final_text
    from pathlib import PurePosixPath

    backend = get_or_create_sandbox(
        tenant_id,
        workspace_host=_AGENT_BASE_DIR / "workspace",
        skills_host=_AGENT_SKILLS_DIR,
    )
    cache: dict[str, dict] = {}

    async def _resolve(path_val: str) -> dict | None:
        container_path = path_val if path_val.startswith("/") else "/" + path_val
        if not container_path.startswith("/workspace"):
            return None
        if container_path in cache:
            return cache[container_path]
        results = await asyncio.to_thread(backend.download_files, [container_path])
        res = results[0] if results else None
        content = getattr(res, "content", None) if res else None
        if content is None or getattr(res, "error", None):
            logger.warning("ws 파일 실체화: 샌드박스에 파일 없음 | path=%s", container_path)
            return None
        p = PurePosixPath(container_path)
        name = p.name
        ext = p.suffix.lower()
        record = await publish_one(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            file_name=name,
            content=content,
        )
        if not record or not record.get("file_url"):
            # 지어낸 주소로 폼값을 채우지 않는다. 경로가 남는 편이 낫다.
            logger.warning("ws 파일 실체화: 주소를 받지 못했다 | path=%s", container_path)
            return None
        val = {
            "path": record["file_url"],
            "name": name,
            "ext": ext,
            # 주소는 만료된다. 이 열쇠로 다시 받는다.
            "file_id": str(record.get("file_id") or ""),
            "url_expires_at": str(record.get("url_expires_at") or ""),
        }
        cache[container_path] = val
        logger.info("ws 파일 실체화 완료 | %s → 서명 주소", container_path)
        return val

    async def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                node[k] = await _walk(v)
            return node
        if isinstance(node, list):
            return [await _walk(x) for x in node]
        if isinstance(node, str):
            s = node.strip()
            if s.startswith("/workspace") and _WS_BINARY_FILE_RE.search(s):
                r = await _resolve(s)
                return r if r is not None else node
        return node

    try:
        obj = await _walk(obj)
    except Exception:
        logger.warning("ws 파일 실체화: JSON 순회 실패", exc_info=True)
        return final_text
    return json.dumps(obj, ensure_ascii=False)


async def _publish_chat_artifacts(
    tenant_id: str, conversation_id: str, text: str, extra_paths: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """채팅 답변에 남은 샌드박스 산출물을 비공개 버킷에 보관하고, 본문을 받을 수 있는 주소로 바꾼다.

    무엇을 어디에 두고 어떤 주소로 내주는지는 SDK 가 모든 에이전트 공용으로 정한다
    (`core/chat/artifacts.py`). 여기서는 샌드박스에서 파일을 꺼내 주는 일만 한다.

    실패는 베스트에포트다. 산출물을 못 올려도 답변 자체는 그대로 보인다 —
    경로가 남는 것이 답변이 통째로 사라지는 것보다 낫다.
    """
    from core.chat.artifacts import publish_chat_artifacts
    from core.chat.file_links import find_document_paths

    if not find_document_paths(text) and not extra_paths:
        return text, []

    try:
        from core.agents.agent import _BASE_DIR as _AGENT_BASE_DIR, _SKILLS_DIR as _AGENT_SKILLS_DIR
        from core.sandbox.docker_sandbox import get_or_create_sandbox
    except Exception:
        logger.warning("채팅 산출물: 모듈 로드 실패", exc_info=True)
        return text, []

    backend = get_or_create_sandbox(
        tenant_id,
        workspace_host=_AGENT_BASE_DIR / "workspace",
        skills_host=_AGENT_SKILLS_DIR,
    )

    async def _fetch(container_path: str):
        results = await asyncio.to_thread(backend.download_files, [container_path])
        res = results[0] if results else None
        content = getattr(res, "content", None) if res else None
        if content is None or getattr(res, "error", None):
            return None
        return content

    return await publish_chat_artifacts(
        tenant_id=tenant_id, conversation_id=conversation_id, text=text, fetch=_fetch,
        extra_paths=extra_paths or [],
    )


async def _emit_chat_hwpx_artifacts(streamer, tenant_id: str, texts: list[str], collected: set[str] | None = None) -> None:
    """채팅 경로: 스킬이 샌드박스에 만든 hwpx 를 office-mcp 로 실체화(storage 업로드 + HTML 변환)하고,
    기존 채팅 파이프라인(onToolEnd → pushHwpxArtifact)이 인식하는 tool_end 이벤트로 내보낸다.

    이렇게 하면 채팅 우측 아티팩트 패널에 HwpxViewer(미리보기·편집)가 그대로 뜬다(프론트 무수정).
    실패는 베스트에포트."""
    if streamer is None:
        return
    paths: set[str] = set(collected or set())
    for txt in texts or []:
        if txt:
            for m in _HWPX_PATH_RE.findall(txt):
                paths.add(m)
    if not paths:
        return
    try:
        from core.agents.agent import _BASE_DIR as _AGENT_BASE_DIR, _SKILLS_DIR as _AGENT_SKILLS_DIR
        from core.sandbox.docker_sandbox import get_or_create_sandbox
    except Exception:
        logger.warning("채팅 hwpx: sandbox 모듈 로드 실패", exc_info=True)
        return
    from pathlib import PurePosixPath

    backend = get_or_create_sandbox(
        tenant_id,
        workspace_host=_AGENT_BASE_DIR / "workspace",
        skills_host=_AGENT_SKILLS_DIR,
    )
    for cp in sorted(paths):
        try:
            results = await asyncio.to_thread(backend.download_files, [cp])
            res = results[0] if results else None
            content = getattr(res, "content", None) if res else None
            if content is None or getattr(res, "error", None):
                continue
            name = PurePosixPath(cp).name
            out = await _call_office_mcp_tool(
                "store_and_render_hwpx",
                {"base64_data": base64.b64encode(content).decode("ascii"), "file_name": name, "tenant_id": tenant_id or ""},
            )
            if not out or not out.get("html_url"):
                logger.warning("채팅 hwpx: office-mcp 결과 없음 | path=%s", cp)
                continue
            await streamer.send_json({"type": "tool_end", "output": out})
            logger.info("채팅 hwpx 아티팩트 emit | %s → html_url=%s", cp, out.get("html_url"))
        except Exception:
            logger.warning("채팅 hwpx 실체화 실패 | path=%s", cp, exc_info=True)


def _file_artifact_payload(path: str, content: str | None, op: str) -> dict:
    name, ext = _ws_file_meta(path)
    text = content if isinstance(content, str) else ""
    truncated = False
    if len(text.encode("utf-8", errors="replace")) > _FILE_ARTIFACT_MAX_BYTES:
        text = text[:_FILE_ARTIFACT_MAX_BYTES]
        truncated = True
    return {
        "type": "file_artifact",
        "op": op,              # 'create' | 'edit'
        "path": str(path),
        "name": name,
        "ext": ext,
        "content": text,
        "truncated": truncated,
    }


def _room_document_paths(chat_room_id: str, since_ns: int) -> list[str]:
    """이번 턴에 작업 폴더에 생긴 문서 산출물(docx·pdf 등)의 컨테이너 경로.

    docx·pdf 는 바이너리라 `write_file` 스트림(텍스트)으로는 화면에 뜨지 않는다. 그래서
    예전에는 **답변 본문에 경로가 적혔을 때만** 사용자에게 갔다 — 모델이 경로를 적지 않은
    턴의 문서는 만들어졌다는 말만 남고 받을 길이 없었다. 글이 아니라 폴더를 본다.
    """
    try:
        from core.chat.file_links import find_room_documents
        from core.skills.tools import (
            _conv_slug,
            bpmn_container_base as _bcb,
            tenant_workspace_root,
        )
    except Exception:
        logger.warning("문서 산출물 스캔: 모듈 로드 실패", exc_info=True)
        return []
    slug = _conv_slug(chat_room_id or "")
    if not slug:
        return []
    try:
        room_host = tenant_workspace_root() / ".bpmn" / slug
        return find_room_documents(room_host, _bcb(chat_room_id or ""), since_ns=since_ns)
    except Exception:
        logger.warning("문서 산출물 스캔 실패 | room=%s", chat_room_id, exc_info=True)
        return []


def _document_artifact_payload(record: dict) -> dict | None:
    """산출물 레코드를 작업 폴더가 읽는 `file_artifact` 로 옮긴다.

    텍스트 산출물과 달리 내용을 싣지 않는다 — 바이너리는 화면이 렌더할 수 없고, 받을 수만
    있으면 된다. 대신 서명 주소와 `file_id` 를 싣는다. 주소는 한 시간이면 죽지만 파일은
    남아 있어서, 화면이 그 열쇠로 새 주소를 받아 간다.
    """
    url = str(record.get("file_url") or "")
    if not url:
        return None
    name = str(record.get("file_name") or "")
    path = str(record.get("source_path") or "") or f"/workspace/{name}"
    path_name, ext = _ws_file_meta(path)
    return {
        "type": "file_artifact",
        "op": "create",
        "path": path,
        "name": name or path_name,
        "ext": ext,
        # 바이너리는 미리보기 내용이 없다. 화면은 url 로 받거나(미리보기 불가 형식)
        # 주소를 그대로 그린다(pdf·이미지).
        "content": "",
        "truncated": False,
        "url": url,
        "file_id": str(record.get("file_id") or ""),
        "url_expires_at": str(record.get("url_expires_at") or ""),
        "content_type": str(record.get("content_type") or ""),
        "size_bytes": record.get("size_bytes") or 0,
    }


CHAT_ERROR_PREFIX = "⚠️ 오류가 발생했습니다: "


def _chat_error_display_text(error_text: str, partial_text: str = "") -> str:
    """오류로 끝난 턴의 assistant 본문. 부분 응답이 있으면 살려서 뒤에 오류를 붙인다."""
    partial = (partial_text or "").strip()
    display = f"{CHAT_ERROR_PREFIX}{error_text or '에이전트 응답 오류'}"
    return f"{partial}\n\n{display}" if partial else display


async def _persist_chat_error_message(
    context: RequestContext,
    *,
    task_id: str,
    context_id: str,
    error_text: str,
    partial_text: str = "",
) -> None:
    """오류로 끝난 채팅 턴도 assistant 메시지를 chats 에 남긴다.

    오류를 SSE 로만 보내면 화면에는 보이지만 DB 에는 아무 것도 남지 않는다. 그래서
    새로고침하거나 방에 다시 들어오면 사용자 질문만 있고 답은 아예 오지 않은 것처럼
    보인다 — 실제로 그랬다. 성공 턴과 같은 저장 경로(SDK persist_chat_to_db)를 쓰고,
    프론트가 보낸 response_message_uuid 로 upsert 하므로 같은 턴이 두 row 로
    갈라지지도 않는다.
    """
    req = getattr(context, "req", None)
    if req is None:
        # 프로세스/워크리스트 경로(ChatRequestContext 가 아님) — chats 저장 대상이 아니다.
        return
    display = _chat_error_display_text(error_text, partial_text)
    evt = new_text_artifact_update_event(
        task_id=task_id,
        context_id=context_id,
        name="assistant_response",
        text=display,
        last_chunk=True,
    )
    ParseDict({"role": "assistant", "isError": True, "error": error_text}, evt.metadata)
    await persist_chat_to_db(req, evt, display)


async def _emit_document_artifacts(streamer, records: list[dict]) -> None:
    """거둔 문서 산출물을 작업 폴더 목록에 띄운다. 실패는 베스트에포트."""
    if streamer is None:
        return
    for record in records or []:
        payload = _document_artifact_payload(record)
        if payload is None:
            continue
        try:
            await streamer.send_json(payload)
            logger.info("문서 산출물 작업 폴더 emit | %s", payload["path"])
        except Exception:
            logger.warning("문서 산출물 emit 실패 | %s", payload.get("path"), exc_info=True)


# 응답이 "병합 요청(PR)을 올렸다"고 보고하는지 판단할 표식.
# PR 을 올린 턴은 저장 버튼을 누를 일이 없으므로 기본 안내 문구로 덮어쓰면 안 된다.
_PULL_REQUEST_MARKERS = (
    "PR created",
    "pull/",
    "merge_requests/",
    "PR #",
    "병합 요청",
    "병합해 주세요",
)


def _reports_pull_request(text: str | None) -> bool:
    """응답 본문이 PR 생성 결과를 알리고 있으면 True."""
    body = text or ""
    return any(marker in body for marker in _PULL_REQUEST_MARKERS)


async def _reemit_room_artifacts(streamer, chat_room_id: str) -> None:
    """검증/완료 후 방의 모든 산출물 파일을 file_artifact(edit)로 재전송한다.

    complete 가 결정적으로 만든 forms/·skills/·manifest.json 등은 에이전트의 write_file
    스트림을 거치지 않아 프론트 패널에 안 떠 저장도 누락됐다. 여기서 전부 다시 보내
    패널 표시 + 저장 대상에 포함되게 한다."""
    if streamer is None:
        return
    try:
        from core.skills.tools import (
            bpmn_container_base as _bcb,
            _conv_slug,
            process_work_dirs,
            tenant_workspace_root,
        )
    except Exception:
        return
    base_container = _bcb(chat_room_id or "")  # /workspace/.bpmn/<slug>
    slug = _conv_slug(chat_room_id or "")
    bpmn_host = tenant_workspace_root() / ".bpmn"
    room_host = bpmn_host / slug if slug else bpmn_host
    if not room_host.exists():
        return
    # 프로세스 폴더들 + (단독 스킬용) 방 base 자체를 항상 포함해 base 바로 아래 skills/ 도 스캔한다.
    proc_dirs = process_work_dirs(room_host, chat_room_id or "")
    if room_host not in proc_dirs:
        proc_dirs.append(room_host)
    seen: set = set()
    previewable_suffixes = {".bpmn", ".json", ".md", ".markdown", ".html", ".htm", ".form", ".xml", ".dmn", ".yaml", ".yml", ".txt", ".csv"}
    excluded_names = {".process-validation.json", "validation-report.md"}
    for pdir in proc_dirs:
        # 최종 산출물은 특정 파일명만 열거하지 않고 지원 형식을 재전송한다. 그래야 모델이
        # 추가로 만든 Markdown/HTML/JSON도 라이브 표시와 새로고침 복원에서 누락되지 않는다.
        # 방 루트는 stale process-*까지 재귀하지 않고 단독 산출물과 skills만 스캔한다.
        if pdir == room_host:
            candidates = [fp for fp in pdir.iterdir() if fp.is_file()]
            if (pdir / "skills").is_dir():
                candidates += list((pdir / "skills").rglob("*"))
        else:
            candidates = list(pdir.rglob("*"))
        targets = sorted(
            fp for fp in candidates
            if fp.is_file()
            and (fp.suffix.lower() in previewable_suffixes or fp.name in {"process-definition.json", "manifest.json"})
            and fp.name.lower() not in excluded_names
        )
        for hp in targets:
            try:
                key = hp.as_posix()
                if key in seen:
                    continue
                seen.add(key)
                rel = hp.relative_to(room_host).as_posix()
                cpath = f"{base_container}/{rel}"
                await streamer.send_json(_file_artifact_payload(cpath, hp.read_text(encoding="utf-8"), "edit"))
            except Exception:
                continue



# 결정론적 실행 카드의 crew_type. `result` 를 쓰면 안 된다 — 프론트는 그 값을 "제출할
# 수 있는 산출물"로 읽어 채택 버튼을 붙이고(`EventTimeline.isSubmittableTask`), 생성기는
# 표본의 폼 산출물을 그 값으로 찾는다. 이 카드가 싣는 것은 폼 값이 아니라 실행 방식과
# 도구 호출 결과라서, `result` 로 두면 채택 버튼이 둘이 되고 그중 하나는 누르는 순간
# 폼에 실행 로그 JSON 을 밀어 넣는다. 폼 값은 뒤따르는 `최종 결과 반환` 이 싣는다.
_DETERMINISTIC_CARD_CREW_TYPE = "deterministic"

async def _enqueue_status(
    event_queue: EventQueue,
    *,
    task_id: str,
    context_id: str,
    state: TaskState,
    text: str,
    event_type: str,
    job_id: str,
    crew_type: Optional[str] = "deepagents"
) -> None:
    evt = new_text_status_update_event(task_id=task_id, context_id=context_id, state=state, text=text)
    ParseDict({"event_type": event_type, "job_id": job_id, "crew_type": crew_type}, evt.metadata)
    await event_queue.enqueue_event(evt)


async def _emit_human_asked(
    event_queue: EventQueue,
    *,
    task_id: str,
    context_id: str,
    interrupt_payload: dict[str, Any],
    run_state: dict[str, Any],
    checkpoint_thread_id: str,
    job_id: str,
    streamer: Any = None,
) -> dict[str, Any]:
    question = (
        interrupt_payload.get("question") if isinstance(interrupt_payload, dict) else None
    ) or "이 작업을 계속 진행하려면 추가 입력이 필요합니다."
    context_text = (
        interrupt_payload.get("context", "") if isinstance(interrupt_payload, dict) else ""
    )
    options_payload = (
        interrupt_payload.get("options") if isinstance(interrupt_payload, dict) else None
    )
    multi_select_payload = bool(
        interrupt_payload.get("multi_select") if isinstance(interrupt_payload, dict) else False
    )
    pending_field = "human_response"

    is_duplicate = is_duplicate_hitl_request(
        run_state, question=question, pending_field=pending_field
    )
    new_run_state = {
        "pending_field": pending_field,
        "tool_name": "request_human_input",
        "last_question": question,
        "checkpoint_thread_id": checkpoint_thread_id or "",
    }

    # 이 턴은 여기서 끝나고 방은 사람의 답을 기다린다 — 두 갈래 모두 마찬가지다.
    _mark_awaiting_human(context_id)

    if is_duplicate:
        # 같은 질문을 다시 물은 경우다. 여기서 done 마커만 보내고 끝내면 어시스턴트 메시지가
        # 하나도 남지 않아, 사용자 화면에는 "답변했는데 아무 반응이 없는" 멈춘 상태로 보인다.
        # 억제(= 새 run_state 로 갱신하지 않음)는 유지하되, 질문 본문은 반드시 남겨 사용자가
        # 다시 답할 수 있게 한다.
        logger.info("중복 HITL 요청 억제 | task_id=%s question=%.80s", task_id, question)
        if streamer:
            hitl_content = json.dumps(
                {
                    "type": "human_input_required",
                    "question": question,
                    "context": context_text,
                    "options": options_payload or [],
                    "multi_select": multi_select_payload,
                    "run_state": new_run_state,
                },
                ensure_ascii=False,
            )
            await streamer.send_json({"type": "done", "content": hitl_content})
        dup_evt = new_text_artifact_update_event(
            task_id=task_id,
            context_id=context_id,
            name="assistant_response",
            text=f"{question}\n\n{context_text}",
            last_chunk=True,
        )
        ParseDict({"role": "assistant"}, dup_evt.metadata)
        await event_queue.enqueue_event(dup_evt)
        return {"status": "waiting_for_human", "run_state": dict(run_state)}

    await _enqueue_status(
        event_queue,
        task_id=task_id,
        context_id=context_id,
        state=TaskState.TASK_STATE_INPUT_REQUIRED,
        text=json.dumps(
            {
                "question": question,
                "context": context_text,
                "options": options_payload or [],
                "multi_select": multi_select_payload,
                "type": "text",
                "run_state": new_run_state,
            },
            ensure_ascii=False,
        ),
        event_type="human_asked",
        job_id=job_id,
    )
    # 채팅 경로: ChatEventQueue 는 TaskStatusUpdateEvent(human_asked)를 무시하고,
    # interrupt 후 done/error 가 없으면 SSE 가 끝나지 않아 프론트가 무한 대기한다.
    # 그래서 human_asked enqueue 직후 질문/컨텍스트/run_state 를 담은 done 메시지도 보낸다.
    if streamer:
        hitl_content = json.dumps(
            {
                "type": "human_input_required",
                "question": question,
                "context": context_text,
                "options": options_payload or [],
                "multi_select": multi_select_payload,
                "run_state": new_run_state,
            },
            ensure_ascii=False,
        )
        await streamer.send_json({"type": "done", "content": hitl_content})

    ae_text = f"{question}\n\n{context_text}"
    artifact_evt = new_text_artifact_update_event(
        task_id=task_id,
        context_id=context_id,
        name="assistant_response",
        text=ae_text,
        last_chunk=True,
    )
    ParseDict({"role": "assistant"}, artifact_evt.metadata)
    await event_queue.enqueue_event(artifact_evt)
    return {"status": "waiting_for_human", "run_state": new_run_state}


def _iter_json_objects(text: str):
    """문자열에서 균형 잡힌 최상위 {...} 블록을 찾아 파싱되는 dict 를 순서대로 yield."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        while j < n:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[i : j + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            yield obj
                    except Exception:
                        pass
                    break
            j += 1
        i = j + 1 if j > i else i + 1


_INPUT_DATA_THRESHOLD = 500  # bytes: 이 크기 이상의 필드 값은 workspace 파일로 오프로드


def _sanitize_ws_name(name: str, max_len: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("_") or "field"
    return safe[:max_len]


def _instance_ws_dir(instance_id: str, subdir: str):
    """인스턴스 격리 workspace 디렉토리(호스트 Path)를 반환한다."""
    from core.skills.tools import tenant_workspace_root
    root = tenant_workspace_root()
    if instance_id:
        return root / _sanitize_ws_name(instance_id, 64) / subdir
    return root / subdir


def _offload_input_data(text: str, instance_id: str) -> str:
    """[InputData] 섹션의 큰 필드(≥ 500B)를 workspace 파일로 오프로드하고 참조 텍스트로 교체한다.

    실패 시 원본 텍스트를 그대로 반환한다(best-effort).
    """
    if not isinstance(text, str) or "[InputData]" not in text:
        return text
    try:
        m = re.search(r'\[InputData\]\s*\n([\s\S]+?)(?=\n\[|\Z)', text)
        if not m:
            return text
        json_block = m.group(1).strip()
        data = json.loads(json_block)
        if not isinstance(data, dict):
            return text

        dir_inputs = _instance_ws_dir(instance_id, "inputs")
        dir_inputs.mkdir(parents=True, exist_ok=True)
        if instance_id:
            container_base = f"/workspace/{_sanitize_ws_name(instance_id, 64)}/inputs"
        else:
            container_base = "/workspace/inputs"

        lines: list[str] = [
            "[이전 단계 입력 데이터]",
            "이전 단계 결과가 파일로 저장되었습니다. 필요한 항목을 read_file로 읽어 사용하세요.",
        ]
        for form_id, fields in data.items():
            lines.append(f"\n[입력: {form_id}]")
            if not isinstance(fields, dict):
                raw = json.dumps(fields, ensure_ascii=False)
                if len(raw.encode()) >= _INPUT_DATA_THRESHOLD:
                    fname = _sanitize_ws_name(str(form_id)) + ".md"
                    try:
                        (dir_inputs / fname).write_text(raw, encoding="utf-8")
                        lines.append(f"- ({len(raw.encode()) / 1024:.1f} KB) → {container_base}/{fname}")
                    except Exception:
                        logger.warning("step input 파일 저장 실패 | form_id=%s", form_id, exc_info=True)
                        lines.append(f"- {raw}")
                else:
                    lines.append(f"- {raw}")
                continue
            for field_name, value in fields.items():
                raw = json.dumps(value, ensure_ascii=False)
                size = len(raw.encode())
                if size >= _INPUT_DATA_THRESHOLD:
                    fname = _sanitize_ws_name(f"{form_id}_{field_name}") + ".md"
                    try:
                        content = value if isinstance(value, str) else raw
                        (dir_inputs / fname).write_text(content, encoding="utf-8")
                        lines.append(f"- {field_name} ({size / 1024:.1f} KB) → {container_base}/{fname}")
                    except Exception:
                        logger.warning("step input 파일 저장 실패 | field=%s", field_name, exc_info=True)
                        lines.append(f"- {field_name}: {raw}")
                else:
                    lines.append(f"- {field_name}: {raw}")

        replacement = "\n".join(lines)
        return text[: m.start()] + replacement + text[m.end():]
    except Exception:
        logger.warning("_offload_input_data 실패 — 원본 텍스트 유지", exc_info=True)
        return text


def _save_step_output(text: str, instance_id: str, activity_id: str) -> None:
    """딥에이전트 단계 완료 시 LLM 최종 응답을 workspace에 저장한다 (D2D 핸드오프용).

    실패해도 에이전트 응답 전송에 영향 없음(best-effort).
    """
    if not text or not activity_id:
        return
    try:
        dir_outputs = _instance_ws_dir(instance_id, "outputs")
        dir_outputs.mkdir(parents=True, exist_ok=True)
        fname = _sanitize_ws_name(activity_id) + ".md"
        (dir_outputs / fname).write_text(text, encoding="utf-8")
        logger.info("step output 저장 | activity_id=%s path=%s", activity_id, dir_outputs / fname)
    except Exception:
        logger.warning("step output 저장 실패 | activity_id=%s", activity_id, exc_info=True)




def _extract_output_contract(text: Any) -> dict | None:
    """출력계약 JSON(process-definition-result / processDefinition 포함)을 **전체 그대로** 추출한다.

    저장은 더 이상 백엔드가 하지 않고(보안), 사용자가 프론트에서 '저장' 버튼을 눌렀을 때 한다.
    따라서 프론트가 저장에 쓸 수 있도록 processDefinition(elements 포함)/forms/agents/skills 를
    모두 담은 전체 계약을 반환한다. 못 찾으면 None."""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return None
    if "processDefinition" not in text and "process-definition-result" not in text:
        return None
    for obj in _iter_json_objects(text):
        wrapped = isinstance(obj.get("processDefinition"), dict)
        pd = obj.get("processDefinition") if wrapped else (
            obj if (obj.get("processDefinitionId") or obj.get("elements")) else None
        )
        if not isinstance(pd, dict):
            continue
        if not (pd.get("processDefinitionId") or pd.get("processDefinitionName") or pd.get("elements")):
            continue
        return {
            "type": "process-definition-result",
            "processDefinition": pd,
            "forms": (obj.get("forms") if wrapped and isinstance(obj.get("forms"), list) else []) or [],
            "agents": (obj.get("agents") if wrapped and isinstance(obj.get("agents"), list) else []) or [],
            "skills": (obj.get("skills") if wrapped and isinstance(obj.get("skills"), list) else []) or [],
        }
    return None


def _normalize_doc_id(raw: Any) -> str:
    """memento file_id(=Supabase storage 경로)로 정규화한다.

    프론트는 publicURL(`http://host/storage/v1/object/public/<bucket>/<path>`)
    또는 sign URL 을 보낼 수 있으나, memento 청크/카탈로그는 storage 경로(`<path>`)로
    저장돼 있어 그대로는 매칭되지 않는다. URL 이면 `<path>` 만 추출한다.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    # 쿼리/프래그먼트 제거
    s = s.split("?", 1)[0].split("#", 1)[0]
    # Supabase Storage URL → 버킷 뒤 경로만 (public/sign/authenticated 모두 대응)
    m = re.search(r"/object/(?:public|sign|authenticated)/[^/]+/(.+)$", s)
    if m:
        s = m.group(1)
    return s.strip().strip("/")


def _normalize_doc_ids(ids: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in ids or []:
        nid = _normalize_doc_id(x)
        if nid and nid not in seen:
            seen.add(nid)
            out.append(nid)
    return out


def _detect_process_result(text: Any) -> dict | None:
    """도구 입출력/최종 텍스트에서 출력계약(전체)을 탐지한다(저장 없음 — 프론트 표시/저장용)."""
    return _extract_output_contract(text)


def _resolve_run_state(extras: dict, row: dict, metadata: dict) -> dict:
    """이번 요청이 이어받는 이전 turn 의 상태. 없으면 빈 dict.

    프론트는 이 값을 metadata 로 실어 보내고, todolist 경로는 workitem 의 draft 에
    들어 있다. 어느 쪽이든 같은 모양이라 찾는 순서만 다르다.
    """
    for source in (extras, metadata, row.get("draft") if isinstance(row.get("draft"), dict) else {}):
        if isinstance(source, dict) and isinstance(source.get("run_state"), dict):
            return dict(source["run_state"])
    return {}


# ---------------------------------------------------------------------------
# 수정 지시(스티어링) — SDK 표준 동작의 이쪽(에이전트) 구현
# ---------------------------------------------------------------------------

def _pending_interrupt(checkpoint_tuple: Any) -> bool:
    """체크포인트에 재개 대기 중인 사람 확인 요청이 남아 있는가."""
    return any(
        len(_w) > 1 and _w[1] == "__interrupt__"
        for _w in (getattr(checkpoint_tuple, "pending_writes", None) or [])
    )


# 사람의 답을 기다리며 멈춘 방. 체크포인트만 보고 판정하면 흔들린다 — 질문을 내보낸
# 직후 몇 백 ms 동안은 interrupt 가 아직 저장소에 안 보여서, 바로 온 수정 지시가
# "사람 확인 대기" 대신 "진행 중인 턴 없음" 으로 거절된다(실제로 그랬다). 그 사실을
# 아는 것은 방금 질문을 내보낸 이 프로세스이므로, 여기서 직접 기억한다.
_awaiting_human: set[str] = set()


def _mark_awaiting_human(conversation_id: str) -> None:
    if conversation_id:
        _awaiting_human.add(conversation_id)


def _clear_awaiting_human(conversation_id: str) -> None:
    _awaiting_human.discard(conversation_id or "")


async def _awaiting_human_input(thread_id: str) -> bool:
    """이 방이 사람의 답을 기다리며 멈춰 있는가.

    그 상태에서는 돌고 있는 실행이 없어 방향을 바꿔 넣을 곳이 없다. 사용자는 그
    질문에 답하는 기존 경로로 방향을 바꾼다(답변 자체가 이미 방향 전환이다).

    이 프로세스가 기억하는 표식을 먼저 본다. 표식이 없어도(파드가 바뀐 경우)
    체크포인트에 재개 대기 중인 요청이 남아 있으면 대기 상태다.
    """
    if not thread_id:
        return False
    if thread_id in _awaiting_human:
        return True
    checkpointer = await get_async_postgres_checkpointer() or get_shared_memory_checkpointer()
    if checkpointer is None:
        return False
    try:
        tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.warning("사람 확인 대기 여부 확인 실패 | thread_id=%s", thread_id, exc_info=True)
        return False
    return _pending_interrupt(tup)


def _is_hitl_answer(extras: dict, row: dict, metadata: dict) -> bool:
    """이번 요청이 이전 turn 의 HITL(사람 확인) 질문에 대한 **응답**인가.

    맞다면 그 turn 은 취소하면 안 되고 끝나기를 기다려야 한다. 프론트는 백엔드가
    interrupt 를 다 처리하기 전(request_human_input 도구 호출 이벤트 시점)에 이미
    확인 패널을 그리므로, 사용자가 바로 승인하면 이전 turn 이 아직 실행 중이다.
    그걸 취소하면 interrupt 체크포인트가 남지 않아 재개가 불가능해지고, 화면에는
    "새 메시지로 대체되어 이전 응답이 취소되었습니다" 오류만 남는다.
    """
    answer = str(
        extras.get("human_response_answer")
        or metadata.get("human_response_answer")
        or ""
    ).strip()
    if not answer:
        return False
    run_state = _resolve_run_state(extras, row, metadata)
    return str(run_state.get("tool_name") or "") == "request_human_input"


class DeepAgentExecutor(AgentExecutor):

    def should_supersede(self, context: RequestContext) -> bool:
        """새 turn 이 이전 turn 을 대체하는가(True) 이어가는가(False).

        SDK 가 turn 시작 전에 물어본다. False 면 SDK 는 이전 turn 을 취소하지 않고
        끝나기를 기다린다 — 두 graph.astream() 실행이 같은 체크포인트를 동시에 쓰는
        레이스는 어느 쪽이든 막힌다.
        """
        ctx_data = context.get_context_data() or {}
        extras = ctx_data.get("extras") or {}
        row = ctx_data.get("row") or {}
        metadata = extras.get("metadata") if isinstance(extras.get("metadata"), dict) else {}
        return not _is_hitl_answer(extras, row, metadata)

    async def steer(self, directive: SteerDirective) -> SteerResult | None:
        """SDK 표준 동작(`steer()`)의 이쪽 구현 — 지금 방향을 바꿀 수 있는지 판정한다.

        받아 두기만 하고 실제 갈아타기는 실행 루프가 한다. 지시를 도구 실행 중간에
        집어넣으면 쓰다 만 파일이나 결과 없는 도구 호출이 남으므로, 루프는 도구가
        끝나고 다음 판단이 시작되기 전(= 체크포인트가 온전한 지점)에만 집어 간다.
        그 시점에 `steer_applied` 가 나간다 — 접수와 반영은 다른 이벤트다.

        Returns:
            None 이면 SDK 의 표준 판정을 그대로 쓴다. 거절할 이유가 있으면
            `SteerResult.reject()` 로 사유를 돌려준다.
        """
        if await _awaiting_human_input(directive.conversation_id):
            return SteerResult.reject(
                REASON_AWAITING_HUMAN_INPUT,
                detail="에이전트가 질문에 대한 답을 기다리는 중입니다. 그 질문에 답해 방향을 알려주세요.",
            )
        return None

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> dict | None:
        try:
            await asyncio.to_thread(seed_local_system_skills)
        except Exception:
            logger.exception("local-system-skills: 시딩 실패, 번들 시스템 스킬 없이 계속 진행")

        try:
            await prefetch_git_skills()
        except Exception:
            logger.exception("git-skills: prefetch 실패, Git 스킬 없이 계속 진행")

        task_id = str(context.task_id)
        context_id = str(context.context_id)
        ctx_data = context.get_context_data() or {}
        extras = ctx_data.get("extras", {})
        row = ctx_data.get("row", {})
        agent_mode = str(row.get("agent_mode") or "").strip()

        tenant_id = str(row.get("tenant_id") or extras.get("tenant_id") or "").strip()
        root_proc_inst_id = str(row.get("root_proc_inst_id") or row.get("proc_inst_id") or "").strip()
        current_workitem_id = str(row.get("id") or "").strip()
        # todolist 행은 proc_def_id 를 실어 나른다(없으면 안전하게 "" 로 폴백).
        current_proc_def_id = str(row.get("proc_def_id") or "").strip()
        instance_id = root_proc_inst_id or current_workitem_id
        activity_id = str(row.get("activity_id") or "").strip()
        user_jwt = extras.get("user_jwt")
        user_email = extras.get("user_email")
        user_name = extras.get("user_name")
        if not (user_email or user_name):
            # process/worklist 모드는 SDK 컨텍스트가 user_email/user_name 을 채우지 않는다.
            # 대신 workitem 담당자 목록(extras["users"])에서 조회해 채운다.
            _uid = str(extras.get("user_id") or row.get("user_id") or "").split(",")[0].strip()
            for _u in (extras.get("users") or []):
                if not isinstance(_u, dict):
                    continue
                if not _uid or str(_u.get("id") or "") == _uid:
                    user_name = user_name or _u.get("name") or _u.get("username")
                    user_email = user_email or _u.get("email")
                    break

        # 새 턴이 시작됐다 — 이전 턴이 남긴 "사람 확인 대기" 표식은 끝났다. 이 턴이
        # 다시 질문하며 멈추면 그때 다시 남는다.
        _clear_awaiting_human(context_id)

        _skill_tenant_id.set(tenant_id)
        _skill_user_id.set(str(extras.get("user_id") or row.get("user_id") or "").strip())
        set_process_context(root_proc_inst_id, current_workitem_id, current_proc_def_id)
        # 채팅 경로(ChatRequestContext)에서는 프론트가 보낸 run_state/human_response_answer 가
        # extras["metadata"] 안에 들어온다. worklist 경로는 extras 최상위/draft 에 둔다.
        # 두 위치를 모두 확인해 채팅에서도 HITL resume 이 트리거되게 한다.
        metadata = extras.get("metadata", {}) if isinstance(extras.get("metadata"), dict) else {}

        agents = extras.get("agents", [])
        # 서브프로세스/액티비티 능력치 폴백(아래 `if not _has_directly_assigned_agents:`)은
        # row.user_id로 직접 배정된 에이전트가 애초에 하나도 없을 때만 타야 한다. 아래에서
        # agents 를 승격 처리로 비워도(agents = []) 이 폴백이 잘못 트리거되지 않도록,
        # 원래 배정 여부를 별도로 기억해둔다.
        _has_directly_assigned_agents = bool(agents)
        streamer = extras.get("streamer")
        form_id = extras.get("form_id")
        has_file = bool(extras.get("sources"))

        # 프론트가 이번 대화 상대로 지정한 에이전트 프로필(role/goal/persona/tools/skills).
        # 딥 에이전트의 루트 프롬프트·도구·스킬을 이 프로필로 커스터마이즈하는 데 쓰인다.
        agent_profile = metadata.get("agent_profile") if isinstance(metadata.get("agent_profile"), dict) else None

        # 설계자/사용자가 "누구를 루트로 쓸지" 명시적으로 지정했는지 확인한다.
        # - 프로세스 모드: 액티비티 정의의 rootAgent (프로세스 설계 화면의 '메인 에이전트')
        # - 채팅 모드: 프론트가 보내는 metadata.root_agent_id
        # 미지정(None)이면 아래의 기존 자동 휴리스틱이 그대로 적용된다.
        explicit_root_agent_id = str(metadata.get("root_agent_id") or "").strip() or None
        if explicit_root_agent_id is None and streamer is None and _has_directly_assigned_agents:
            try:
                explicit_root_agent_id = load_activity_root_agent(
                    tenant_id=tenant_id,
                    proc_def_id=current_proc_def_id,
                    activity_id=activity_id,
                )
            except Exception:
                logger.exception(
                    "루트 에이전트 설정 조회 실패, 자동 규칙으로 진행 | proc_def_id=%s activity_id=%s",
                    current_proc_def_id, activity_id,
                )
                explicit_root_agent_id = None

        agent_profile, agents = resolve_root_agent(
            agents=agents,
            agent_profile=agent_profile,
            explicit_root_agent_id=explicit_root_agent_id,
            activity_id=activity_id,
        )

        def _exclude_root_profile(agent_list: list) -> list:
            # 대화 상대 프로필이 루트 에이전트 자체가 된 경우, "서브에이전트 위임" 문구·
            # task() 위임 후보에서 자기 자신은 제외한다.
            if not (agent_profile and agent_profile.get("id")):
                return agent_list
            return [a for a in agent_list if str(a.get("id") or "") != str(agent_profile["id"])]

        draft = row.get("draft") if isinstance(row.get("draft"), dict) else {}
        run_state: dict[str, Any] = {}
        if isinstance(extras.get("run_state"), dict):
            run_state = dict(extras["run_state"])
        elif isinstance(metadata.get("run_state"), dict):
            run_state = dict(metadata["run_state"])
        elif isinstance(draft.get("run_state"), dict):
            run_state = dict(draft["run_state"])

        human_answer_stripped = str(
            extras.get("human_response_answer")
            or metadata.get("human_response_answer")
            or ""
        ).strip()

        is_chat = streamer is not None
        chat_room_id = resolve_chat_room_id(extras, row if isinstance(row, dict) else {})
        # 산출물을 방별 디렉터리(.bpmn/<room>/)로 격리 — 경로 충돌/덮어쓰기 방지.
        # chat_room_id 가 없는 워크리스트/액티비티 실행(is_chat=False)은 canonical_process_dir_name()이
        # tenant_id 로 폴백해 매 실행이 같은 산출물 폴더를 공유하게 된다 — 그러면 이전 실행(예: 일주일 전
        # 같은 액티비티 실행)이 남긴 파일을 이번 실행 결과로 재사용하는 사고가 난다. instance_id(process
        # instance 또는 workitem 단위로 유일)로 폴백해 실행마다 격리한다.
        workspace_conversation_id = chat_room_id or instance_id
        _skill_conversation_id.set(workspace_conversation_id or "")
        # 도구 레벨 봉쇄: ls/read_file/glob/grep 이 다른 실행의 .bpmn/<다른 방> 이나
        # <다른 instance_id>/inputs·outputs 를 절대경로로 직접 찍어 읽는 것도 막는다
        # (위 방 단위 폴더 격리만으론 에이전트가 의도적/우발적으로 형제 디렉토리를 열람하는 걸 못 막음).
        _scope_prefixes = [_bpmn_container_base(workspace_conversation_id)]
        # 스킬 체크아웃도 방 단위로 갈리므로(.checkouts/<room>) 이번 방 것만 열어 준다.
        # 방 id 가 없으면 checkout base 가 /workspace 자체라, 그걸 prefix 로 넣으면 위에서
        # 좁혀 둔 스코프가 통째로 풀린다 — 그 경우는 넣지 않는다(어차피 격리할 방이 없다).
        _checkout_base = _checkout_container_base(workspace_conversation_id)
        if _checkout_base != "/workspace":
            _scope_prefixes.append(_checkout_base)
        if instance_id:
            _scope_prefixes.append(f"/workspace/{_sanitize_ws_name(instance_id, 64)}")
        set_workspace_scope(*_scope_prefixes)

        if is_chat and chat_room_id and tenant_id:
            # 이 파드가 이 방을 처음 다루는 경우(로컬에 파일이 없음) 다른 파드가 이전 턴에
            # 만들었을 수 있는 워크스페이스 파일을 Supabase Storage에서 미리 내려받는다.
            # 로컬 디스크는 파드 로컬이라(RWX 공유 스토리지 없음) 이게 없으면 다른 파드가
            # 만든 process-definition.json 등을 이어서 못 읽는다.
            try:
                _set_storage_tenant_id(tenant_id)
                room_dir = _bpmn_work_dir()
                has_local_files = room_dir.exists() and any(room_dir.glob("**/*"))
                if not has_local_files:
                    result = await asyncio.to_thread(download_workspace_directory, chat_room_id, room_dir)
                    logger.info("워크스페이스 하이드레이션 | room_id=%s | %s", chat_room_id, result.splitlines()[0] if result else "")
            except Exception:
                logger.warning("워크스페이스 하이드레이션 실패(무시하고 진행) | room_id=%s", chat_room_id, exc_info=True)

        # 이전 turn 정리(대체할지 기다릴지), in-flight 등록, 재접속 등록, 토큰
        # 브로드캐스트는 모두 SDK 가 한다 — 채팅 출력 큐를 가로채 레지스트리에
        # 남기므로 재접속한 클라이언트가 받는 것과 원래 클라이언트가 받는 것이
        # 정의상 같아진다. 대체/이어가기 판단만 `should_supersede()` 로 이 클래스가
        # 돌려준다.
        #
        # 여기서는 tool/plan 이벤트를 채팅방 컨텍스트로 증분 저장하는 훅만 얹는다.
        # 이 훅에서 레지스트리에 또 기록하면 같은 토큰이 두 번 쌓여 재접속 스냅샷
        # 본문이 두 배가 된다(core/chat/event_tap.py).
        if streamer is not None and chat_room_id:
            _persist_tenant_id = tenant_id
            _persist_has_file = has_file

            async def _on_context_event(evt: dict) -> None:
                et = str(evt.get("type") or "")
                if not _persist_tenant_id:
                    return
                if et in ("plan_tools", "plan_skills", "plan_connectors", "plan_todos", "tool_start", "tool_end"):
                    await a_update_chat_room_context(
                        tenant_id=_persist_tenant_id,
                        room_id=chat_room_id,
                        evt=evt,
                        has_file=_persist_has_file,
                    )

            streamer = EventTapStreamer(streamer, on_event=_on_context_event)

        tenant_mcp: dict[str, Any] = {}
        if tenant_id:
            try:
                from core.storage.db import get_supabase
                sb = get_supabase()
                result = (
                    sb.table("tenants")
                    .select("mcp")
                    .eq("id", tenant_id)
                    .single()
                    .execute()
                )
                fetched = (result.data or {}).get("mcp")
                if isinstance(fetched, dict):
                    tenant_mcp = fetched
            except Exception:
                logger.warning("tenant_mcp DB 조회 실패 | tenant_id=%s", tenant_id, exc_info=True)
            tenant_mcp = ensure_process_gpt_mcp(tenant_mcp, tenant_id)
        chat_root_tools, _root_tool_servers = (
            await load_chat_root_tools(tenant_mcp) if is_chat else ([], {})
        )

        if is_chat and chat_room_id and tenant_id:
            try:
                await a_upsert_chat_room_start(
                    tenant_id=tenant_id,
                    room_id=chat_room_id,
                    participants=[],
                    room_name=None,
                    primary_agent_id=None,
                    context=build_initial_chat_room_context(has_file=has_file),
                )
            except Exception:
                logger.warning(
                    "chat_room start upsert 실패 | room_id=%s", chat_room_id, exc_info=True
                )

        # [InputData] 섹션의 큰 필드를 workspace 파일로 오프로드해 초기 LLM 컨텍스트 크기를 줄인다.
        # 피드백 재작업(FB_REQUESTED)이면 feedback 배열의 가장 최근 항목의 content만 추출해
        # 메시지를 구성한다(build_user_message 미사용 — 소스/폼 형식 안내가 재작업 지시와 섞이지 않게).
        _feedback_list = row.get("feedback")
        _latest_feedback_content = ""
        if isinstance(_feedback_list, list) and _feedback_list:
            _latest_feedback_item = _feedback_list[-1]
            if isinstance(_latest_feedback_item, dict):
                _latest_feedback_content = str(_latest_feedback_item.get("content") or "").strip()
            else:
                _latest_feedback_content = str(_latest_feedback_item or "").strip()
        # 채팅모드에서 사용자가 메시지 선두에 '/skill-name' 을 입력하면, 그 스킬이 현재
        # 테넌트에서 실제로 사용 가능할 때 해당 스킬을 강제 지정한다(아래 activity_skills 오버라이드).
        _forced_skill_name = ""
        if _latest_feedback_content:
            _original_query = str(context.get_user_input() or "").strip()
            user_message = (
                f"[재작업 요청 — 피드백]\n{_latest_feedback_content}"
                + (f"\n\n[원본 작업 지시]\n{_original_query}" if _original_query else "")
            )
        else:
            _base_message = _offload_input_data(str(context.get_user_input() or ""), instance_id)
            if is_chat:
                _slash_skill, _stripped_message = extract_skill_command(_base_message)
                if _slash_skill:
                    _forced_skill_name = resolve_slash_skill(tenant_id, _slash_skill)
                    if _forced_skill_name:
                        _base_message = _stripped_message
                        logger.info(
                            "채팅 슬래시 커맨드로 스킬 지정 | tenant_id=%s skill=%s",
                            tenant_id, _forced_skill_name,
                        )
            user_message = build_user_message(
                _base_message,
                sources=extras.get("sources") or [],
                form_id=form_id,
                form_fields=extras.get("form_fields"),
                suggest_related_outputs=(not is_chat and bool(root_proc_inst_id)),
                agents=_exclude_root_profile(agents),
            )
        recent_msgs = collect_recent_messages(extras) if is_chat else []
        # 프로세스명 도출용: 이번 사용자 요청 원문을 contextvar 로 전달(complete 가 이름이 비면 사용).
        _user_input_text = ""
        try:
            _user_input_text = str(context.get_user_input() or "")
            # HITL resume 턴의 current input은 "승인" 또는 후보 선택 답변뿐이다. 원 요청을
            # 잃으면 이름과 명시 추가 산출물(.md/.html) 요구가 완료 단계에서 사라진다.
            request_candidates = [
                str(msg.get("content") or "").strip() for msg in recent_msgs
                if msg.get("role") == "user" and str(msg.get("content") or "").strip()
            ]
            request_candidates.append(_user_input_text)
            non_hitl = [
                text for text in request_candidates
                if not re.search(r"\[(?:스킬|에이전트|DMN)\]|^\s*(?:승인|반려)\s*$", text, re.IGNORECASE)
            ]
            original_request = max(
                non_hitl or request_candidates or [""],
                key=lambda text: (
                    int(bool(re.search(r"프로세스|workflow|bpmn", text, re.IGNORECASE))),
                    int(bool(re.search(r"markdown|마크다운|\.md\b|html|\.html\b", text, re.IGNORECASE))),
                    len(text),
                ),
            )
            _skill_user_request.set(original_request)
        except Exception:
            pass
        # 후보 선택 HITL 답변이면([스킬]/[에이전트]/[DMN]) manifest 로 캡처(에이전트가 안 써도 스킬/연결 보장).
        # process-* 폴더는 1단계에서 이미 생성됨(이번 턴은 후보선택 resume). 방은 contextvar(위에서 set)로 찾는다.
        try:
            from core.skills.tools import _capture_selection_to_manifest as _cap
            _cap(_user_input_text)
        except Exception:
            logger.warning("선택→manifest 캡처 실패", exc_info=True)

        metadata = extras.get("metadata", {})

        await event_queue.enqueue_event(
            new_task(task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED)
        )

        activity_name = row.get("activity_name", "Unknown")

        # Worklist 활동에는 CrewAI와 동일한 mcp_python_code 저장소를 먼저 확인한다.
        # 코드가 없을 때만 아래 DeepAgents 그래프(LLM 추론)를 구성한다.
        if not is_chat:
            deterministic = await try_deterministic_execution(row, tenant_mcp)
            if deterministic is not None:
                deterministic_job_id = str(uuid.uuid4())
                is_undo = deterministic.mode == "deterministic-undo"
                is_undo_only = deterministic.mode == "deterministic-undo-only"
                # 되돌릴 근거가 없어 재작업 전체를 에이전트가 맡는 경우. 카드를 띄우지
                # 않으면 "되돌릴 것이 없었다"와 "되돌릴 수 없었다"가 기록에서 구분되지
                # 않는다 — 사람이 보는 자리에 사유가 남아야 한다.
                is_skipped = deterministic.mode == "deterministic-skipped"
                # 되돌릴 것이 남아 있는 채로 넘어온 경우. 사람이 보는 자리에 "보류"가 아니라
                # "에이전트가 되돌린다"로 남아야, 재작업 결과를 검수할 때 무엇을 확인해야
                # 하는지가 보인다.
                is_undo_pending = deterministic.mode == MODE_UNDO_PENDING
                if is_undo_pending:
                    card_name = "이전 실행 되돌리기 — 에이전트가 수행"
                elif is_skipped:
                    card_name = "재작업 되돌리기 보류"
                elif is_undo_only:
                    card_name = "Undo(실행 취소) 결과"
                elif is_undo:
                    card_name = "Undo(실행 취소) 후 재실행 결과"
                else:
                    card_name = "결정론적 코드 실행 결과"
                await _enqueue_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                    text=json.dumps(
                        {
                            "role": card_name,
                            "name": card_name,
                            "goal": (
                                str(deterministic.output.get("note") or "")
                                + " 사유: "
                                + str(deterministic.output.get("reason") or "")
                                if is_skipped or is_undo_pending
                                else "고착화된 undo 코드로 이전 실행을 되돌립니다. 재실행은 에이전트가 수행합니다."
                                if is_undo_only
                                else "고착화된 undo 코드로 이전 실행을 되돌린 뒤 새 입력으로 재실행합니다."
                                if is_undo
                                else "고착화된 코드로 LLM 추론 없이 재실행합니다."
                            ),
                            "agent_profile": "/images/chat-icon.png",
                            "execution_mode": deterministic.mode,
                        },
                        ensure_ascii=False,
                    ),
                    event_type="task_started",
                    job_id=deterministic_job_id,
                    crew_type=_DETERMINISTIC_CARD_CREW_TYPE,
                )
                await _enqueue_status(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_COMPLETED,
                    text=deterministic.result_text,
                    event_type="task_completed",
                    job_id=deterministic_job_id,
                    crew_type=_DETERMINISTIC_CARD_CREW_TYPE,
                )
                if not deterministic.continue_to_llm:
                    # 에이전트 경로와 같은 자리에 `최종 결과 반환` 을 하나 더 띄운다.
                    # 워크아이템의 폼 산출물은 이 이벤트와 최종 artifact 로만 전달되고,
                    # 위의 결과 카드는 실행 방식만 보여 준다. 이걸 빼면 코드가 제대로
                    # 돌았는데도 폼이 빈 채로 워크아이템이 완료되어 다음 활동이 굶는다.
                    final_text = deterministic.form_output_text or deterministic.result_text
                    if deterministic.form_output_text:
                        result_job_id = str(uuid.uuid4())
                        await _enqueue_status(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            state=TaskState.TASK_STATE_WORKING,
                            text=json.dumps(
                                {
                                    "goal": "최종 결과를 반환합니다.",
                                    "name": "최종 결과 반환",
                                    "role": "최종 결과 반환",
                                    "agent_profile": "/images/chat-icon.png",
                                },
                                ensure_ascii=False,
                            ),
                            event_type="task_started",
                            job_id=result_job_id,
                            crew_type="result",
                        )
                        _save_step_output(final_text, instance_id, activity_id)
                        await _enqueue_status(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            state=TaskState.TASK_STATE_COMPLETED,
                            text=final_text,
                            event_type="task_completed",
                            job_id=result_job_id,
                            crew_type="result",
                        )
                    artifact_evt = new_text_artifact_update_event(
                        task_id=task_id,
                        context_id=context_id,
                        name="assistant_response",
                        text=final_text,
                        last_chunk=True,
                    )
                    ParseDict({"role": "assistant"}, artifact_evt.metadata)
                    await event_queue.enqueue_event(artifact_evt)
                    return None
                # 되돌리기만 수행한 경우: 결과 카드만 남기고 DeepAgents 실행을 계속한다.
                # 최종 산출물은 에이전트가 만든다.
                if deterministic.undo_directive:
                    # 보상이 아직 끝나지 않았다. 되돌리기(또는 사람 확인) 지시를 작업 지시
                    # **앞에** 붙여, 그것을 먼저 마치고 나서 재작업하게 한다. 이 줄이 없으면
                    # 되돌려지지 않은 세상 위에 재작업이 얹혀 같은 값이 두 번 반영된다.
                    user_message = f"{deterministic.undo_directive}\n\n{user_message}"
                    logger.info(
                        "되돌리기 지시를 에이전트 입력에 주입 | todo=%s mode=%s steps=%d",
                        row.get("id"), deterministic.mode,
                        len(deterministic.output.get("undo_steps") or []),
                    )

        job_id = str(uuid.uuid4())

        await _enqueue_status(
            event_queue,
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            text=json.dumps({"goal": f"{activity_name} 작업을 시작합니다."}, ensure_ascii=False),
            event_type="task_started",
            job_id=job_id,
        )

        # Postgres(DB_*) 가 있으면 그것을, 없으면 프로세스 전역 공유 InMemory 체크포인터를 쓴다.
        # 매 요청마다 새 MemorySaver 를 만들면 직전 interrupt 체크포인트가 사라져 HITL resume 이
        # 불가능하므로, 항상 동일(공유) 인스턴스를 build_agent/build_graph_inputs 에 넘긴다.
        checkpointer = await get_async_postgres_checkpointer()
        if checkpointer is None:
            checkpointer = get_shared_memory_checkpointer()
        checkpoint_thread_id = chat_room_id if is_chat else (resolve_checkpoint_thread_id(is_chat=False, row=row) or chat_room_id)

        # 이 thread 에 이미 누적된 체크포인트가 있는지 확인 — 있으면 history 재주입을 생략해
        # 메시지 중복을 막는다(지속 체크포인터가 history 를 이미 들고 있으므로).
        # 재개 대기 중인 interrupt 가 실제로 남아 있는지까지 확인한다. 없는데 Command(resume=)
        # 를 보내면 LangGraph 는 오류 없이 그래프를 처음부터 재실행해 같은 질문으로 새 interrupt
        # 를 만들고, 그걸 executor 가 중복 HITL 로 억제해 사용자에겐 빈 응답만 남는다.
        has_checkpoint = False
        has_pending_interrupt = False
        if checkpointer is not None and checkpoint_thread_id:
            try:
                _cfg = {"configurable": {"thread_id": checkpoint_thread_id}}
                _tup = await checkpointer.aget_tuple(_cfg)
                has_checkpoint = _tup is not None
                has_pending_interrupt = _pending_interrupt(_tup)
            except Exception:
                logger.warning("체크포인트 존재 확인 실패 | thread_id=%s", checkpoint_thread_id, exc_info=True)
                has_checkpoint = False
                has_pending_interrupt = False

        inputs, use_graph_resume = build_graph_inputs(
            user_message,
            human_answer_stripped=human_answer_stripped,
            run_state=run_state,
            checkpointer=checkpointer,
            checkpoint_thread_id=checkpoint_thread_id,
            recent_messages=recent_msgs,
            has_checkpoint=has_checkpoint,
            has_pending_interrupt=has_pending_interrupt,
        )

        if use_graph_resume:
            logger.info("HITL 그래프 재개 | thread_id=%s task_id=%s", checkpoint_thread_id, task_id)
        elif human_answer_stripped:
            logger.info(
                "HITL 비-그래프 재개 | task_id=%s pending_field=%s pending_interrupt=%s",
                task_id,
                run_state.get("pending_field") or "human_response",
                has_pending_interrupt,
            )

        if human_answer_stripped:
            resume_run_state = {
                "pending_field": run_state.get("pending_field") or "human_response",
                "tool_name": run_state.get("tool_name") or "request_human_input",
                "last_question": run_state.get("last_question") or "",
                "checkpoint_thread_id": checkpoint_thread_id or "",
                "resume_source": "checkpoint_graph_resume" if use_graph_resume else "human_response",
            }
            await _enqueue_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=json.dumps(
                    {"answer": human_answer_stripped, "type": "text", "run_state": resume_run_state},
                    ensure_ascii=False,
                ),
                event_type="human_answered",
                job_id=job_id,
            )

        subprocess_alias_map: dict[str, str] = {}
        root_proc_inst_id: str = ""
        activity_tools: list[str] = []
        activity_skills: list[str] = []
        if not _has_directly_assigned_agents:
            proc_def_id = str(row.get("proc_def_id") or "").strip()
            activity_id = str(row.get("activity_id") or "").strip()
            subprocess_agents = load_subprocess_subagents(
                tenant_id=tenant_id,
                proc_def_id=proc_def_id,
                activity_id=activity_id,
            )
            if subprocess_agents:
                agents = subprocess_agents
                subprocess_alias_map = {
                    re.sub(r"[^\w-]", "-", spec["name"]).lower().strip("-"): spec["alias"]
                    for spec in subprocess_agents
                }
                root_proc_inst_id = str(row.get("root_proc_inst_id") or "").strip()
                logger.info(
                    "서브프로세스 딥에이전트 구성 | proc_def_id=%s activity_id=%s agents=%d",
                    proc_def_id, activity_id, len(subprocess_agents),
                )
                from processgpt_agent_sdk.database import fetch_form_def as _fetch_form_def
                _freeform_instruction = (
                    "\n\n[응답 형식]\n"
                    "반드시 JSON으로만 출력하세요. 다른 설명 없이 JSON만 출력하세요.\n"
                    '출력 형식:\n{"free_input": "<답변 내용>"}'
                )
                for _spec in agents:
                    _tool_val = (_spec.get("tool") or "").strip()
                    if not _tool_val.startswith("formHandler:"):
                        continue
                    try:
                        _form_id_val, _fields, _ = await _fetch_form_def(_tool_val, tenant_id)
                        if _fields:
                            _field_schema = json.dumps(_fields, ensure_ascii=False)
                            _example = json.dumps(
                                {_form_id_val: {f["key"]: f"<{f['text']}>" for f in _fields if "key" in f}},
                                ensure_ascii=False,
                                indent=2,
                            )
                            _form_instruction = (
                                "\n\n[응답 형식]\n"
                                "아래 폼 필드에 맞춰 반드시 JSON으로만 출력하세요. 다른 설명 없이 JSON만 출력하세요.\n"
                                f"필드 정의:\n{_field_schema}\n"
                                f"출력 예시:\n{_example}"
                            )
                        else:
                            _form_instruction = _freeform_instruction
                        _spec["description"] = (_spec.get("description") or "") + _form_instruction
                    except Exception:
                        logger.warning("서브에이전트 폼 정의 조회 실패 | tool=%s", _tool_val, exc_info=True)
                        _spec["description"] = (_spec.get("description") or "") + _freeform_instruction
            else:
                act_tools, act_skills = load_activity_capabilities(
                    tenant_id=tenant_id,
                    proc_def_id=proc_def_id,
                    activity_id=activity_id,
                )
                if act_tools or act_skills:
                    activity_tools = act_tools
                    activity_skills = act_skills
                    logger.info(
                        "액티비티 지정 도구/스킬을 루트 딥에이전트에 부착 | proc_def_id=%s activity_id=%s tools=%s skills=%s",
                        proc_def_id, activity_id, act_tools, act_skills,
                    )
                elif not is_chat:
                    logger.warning(
                        "서브에이전트 미설정 (activity tools/skills 없음) | proc_def_id=%s activity_id=%s",
                        proc_def_id, activity_id,
                    )

        # 사용자가 채팅 메시지에 '/skill-name' 슬래시 커맨드를 입력한 경우, 액티비티 정의로
        # 유도된 스킬 목록과 무관하게 그 스킬 하나로 반드시 실행되도록 덮어쓴다.
        if _forced_skill_name:
            activity_skills = [_forced_skill_name]

        _root_tools_by_server: dict[str, list[str]] = {}
        for _tool_name, _srv in _root_tool_servers.items():
            _root_tools_by_server.setdefault(_srv, []).append(_tool_name)
        _root_mcp_meta: list[dict] = [
            {"agent": "__root__", "servers": [_srv], "tools": _tool_names}
            for _srv, _tool_names in _root_tools_by_server.items()
        ]
        _accum = CtxAccumulator(mcp_tools=_root_mcp_meta)

        sources = extras.get("sources") or []
        knowledge_doc_ids: list[str] = []
        for _s in sources:
            if not isinstance(_s, dict):
                continue
            # file_id 우선, 없으면 file_path/URL 을 normalize 해서 storage 경로로 사용
            _kid = str(_s.get("file_id") or _s.get("fileId") or "").strip()
            if not _kid:
                _raw_fp = str(_s.get("file_path") or _s.get("url") or "").strip()
                if _raw_fp:
                    _kid = _normalize_doc_id(_raw_fp)
            if _kid:
                knowledge_doc_ids.append(_kid)
        if not knowledge_doc_ids:
            knowledge_docs = (metadata or {}).get("knowledge_docs") or []
            knowledge_doc_ids = [
                d.get("id", "") for d in knowledge_docs
                if isinstance(d, dict) and d.get("id")
            ]
        # 채팅 첨부 업로드(extras["files"] = req.files / metadata.input_data.files)도
        # memento file_id(=storage path) 를 그대로 knowledge_doc_ids 로 사용한다.
        # 프론트의 knowledge_docs 병합과 무관하게, 업로드한 파일은 항상 검색 가능해야 한다.
        _uploaded = list(extras.get("files") or [])
        _idata = metadata.get("input_data") if isinstance(metadata.get("input_data"), dict) else {}
        _uploaded += list(_idata.get("files") or [])
        for _f in _uploaded:
            if not isinstance(_f, dict):
                continue
            _kid = (
                _f.get("fileId") or _f.get("file_id") or _f.get("filePath")
                or _f.get("file_path") or _f.get("fullPath") or _f.get("path")
                or _f.get("publicUrl") or _f.get("fileUrl") or _f.get("url") or ""
            )
            _kid = str(_kid).strip()
            if _kid:
                knowledge_doc_ids.append(_kid)
        # memento 청크의 file_id 는 storage 경로(예: 'files/<uuid>.pdf') 다.
        # 프론트가 publicURL/sign URL 을 보내도 storage 경로로 정규화해 매칭시킨다.
        knowledge_doc_ids = _normalize_doc_ids(knowledge_doc_ids)
        logger.info(
            "knowledge_doc_ids 구성 | n=%d ids=%s",
            len(knowledge_doc_ids), knowledge_doc_ids[:5],
        )

        def _discard_streamed_text() -> None:
            """수정 지시가 얹히는 순간, 그 전까지 쓰던 본문을 버린다.

            사용자가 거둬들인 지시로 만든 글이다. 남겨 두면 최종 답변이 "버려진 초안 +
            새 답변" 이 되어, 고쳐 달라고 했더니 같은 말을 두 번 한 것처럼 보인다.
            """
            nonlocal full_response
            if full_response:
                logger.info(
                    "steering: 수정 지시 전까지 쓴 본문 %d자를 버린다 | room=%s",
                    len(full_response), context_id,
                )
            full_response = ""

        graph, _, _sub_mcp_tools_meta = await build_agent(
            tenant_id=tenant_id,
            conversation_id=workspace_conversation_id or "",
            checkpointer=checkpointer,
            subagents_raw=_exclude_root_profile(agents),
            tenant_mcp=tenant_mcp,
            extra_tools=chat_root_tools,
            # 작업 중에 들어온 수정 지시를 다음 모델 판단에 얹는다(채팅 전용 —
            # 워크아이템 실행에는 방향을 바꿀 사용자가 붙어 있지 않다).
            middleware=[
                ToolResultTruncationMiddleware(),
                *(
                    [SteeringMiddleware(
                        conversation_id=context_id, on_applied=_discard_streamed_text,
                    )]
                    if is_chat else []
                ),
            ],
            # skill-creator 서브에이전트(단독 스킬 생성 위임 대상)는 채팅에서 사용자가
            # "스킬 만들어줘" 라고 요청하는 흐름 전용이다. 프로세스(워크아이템) 모드는
            # BPMN 액티비티를 실행하는 것이지 스킬을 생성하는 게 아니므로 붙이지 않는다.
            static_subagents=build_skill_creator_subagents(task_id=task_id) if is_chat else [],
            agent_mode=agent_mode,
            is_chat=is_chat,
            activity_tools=activity_tools,
            activity_skills=activity_skills,
            root_profile=agent_profile,
            user_context={
                "user_jwt": user_jwt,
                "user_email": user_email,
                "user_name": user_name,
                "user_id": str(extras.get("user_id") or row.get("user_id") or "").strip(),
            },
        )

        if _sub_mcp_tools_meta:
            _accum.mcp_tools = _root_mcp_meta + _sub_mcp_tools_meta

        _mcp_tool_servers: dict[str, list[str]] = {}
        for meta in _accum.mcp_tools:
            srvs = meta.get("servers") or []
            for tn in (meta.get("tools") or []):
                _mcp_tool_servers.setdefault(tn, [])
                for s in srvs:
                    if s not in _mcp_tool_servers[tn]:
                        _mcp_tool_servers[tn].append(s)

        _tracking_cb = (
            ToolTrackingCallback(
                streamer=streamer,
                push_ctx=_accum.push,
                mcp_tool_servers=_mcp_tool_servers,
            )
            if is_chat
            else None
        )

        config = {
            "configurable": {
                "thread_id": checkpoint_thread_id,
                "tenant_id": tenant_id,
                "knowledge_doc_ids": knowledge_doc_ids,
            },
            **({"callbacks": [_tracking_cb]} if _tracking_cb else {}),
        }

        active_tool_calls: dict[str, str] = {}
        # request_human_input 의 인자(질문·초안 본문)를 쓰는 동안 화면에 흘려보내기 위한
        # 스트리머. 이 도구는 사용자에게 보여줄 본문이 통째로 **인자 안에** 들어가고
        # 어시스턴트 content 는 비어 있어서, 중계하지 않으면 몇 분간 "생각 중..." 만 뜬다.
        # key = tool_call index(스트리밍 청크는 id 없이 index 로만 온다).
        hitl_arg_streamers: dict[int, ToolArgTextStreamer] = {}
        hitl_arg_indexes: set[int] = set()
        # 도구 호출 인자 tool_call_id → args. tool_usage_finished 이벤트에 인자를 함께
        # 실어야 고착화 코드 생성과 보상(undo) 생성이 성립한다. 인자 없이 결과만
        # 남으면 되돌릴 대상을 복원할 수 없다.
        pending_tool_args: dict[str, dict] = {}
        # write_file/edit_file tool_call_id → {path, op}. tool_end 시 파일을 읽어 file_artifact emit.
        pending_file_ops: dict[str, dict] = {}
        # subprocess 딥에이전트: task() tool_call_id → subagent_type slug
        pending_task_calls: dict[str, str] = {}
        full_response = ""
        final_state: dict[str, Any] = {}
        _seen_msg_namespaces: dict[str, str] = {}
        process_result_sent = False
        # 생성 턴 폴백: 에이전트가 process-definition.json 은 만들었지만 complete 를 호출하지 않고
        # 턴을 끝내는 경우(스킬 생성 등으로 step 소진)가 있어, 스트림 종료 시 자동으로 complete 한다.
        process_json_written = False
        skill_written = False
        complete_called = False
        # 채팅 경로: 스킬이 만든 hwpx 컨테이너 경로 수집(종료 시 office-mcp 로 실체화 → 아티팩트).
        chat_hwpx_paths: set[str] = set()
        # /workspace 에 파일을 썼으면 True — 컨설팅 1단계(파일 미작성)와 구분하는 데 사용한다.
        workspace_write_made = False
        pending_hitl_payload: dict[str, Any] | None = None
        # 이번 턴이 시작한 시각. 턴이 끝나면 작업 폴더에서 이 시각 뒤에 바뀐 문서 산출물을
        # 찾아 거둔다(`_room_document_paths`). 이 표식이 없으면 지난 턴의 문서까지 다시
        # 올라가 화면에 같은 것이 여러 번 뜬다.
        turn_started_ns = time.time_ns()

        try:
            async for _ns, stream_evt, data in graph.astream(
                inputs, config=config, stream_mode=["messages", "updates", "values"], subgraphs=True
            ):
                if stream_evt == "messages":
                    msg, _meta = data
                    if isinstance(msg, AIMessageChunk):
                        text = msg.content if isinstance(msg.content, str) else "".join(
                            b.get("text", "") for b in msg.content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if text:
                            msg_id = getattr(msg, "id", "") or ""
                            if msg_id:
                                ns = (_meta or {}).get("langgraph_checkpoint_ns", "") if isinstance(_meta, dict) else ""
                                first_ns = _seen_msg_namespaces.setdefault(msg_id, ns)
                                if first_ns != ns:
                                    continue
                            full_response += text
                            if streamer:
                                await streamer.send_json({"type": "token", "content": text})
                        # HITL 질문/초안은 도구 인자로만 오므로 별도로 흘려보낸다.
                        # 화면 표시 전용이라 full_response 에는 넣지 않는다 — 최종 메시지
                        # 본문은 HITL 패널이 대신하고, 여기에 섞으면 저장 내용이 중복된다.
                        if streamer:
                            for _tcc in (getattr(msg, "tool_call_chunks", None) or []):
                                if not isinstance(_tcc, dict):
                                    continue
                                _idx = _tcc.get("index")
                                if _idx is None:
                                    continue
                                if (_tcc.get("name") or "") == "request_human_input":
                                    hitl_arg_indexes.add(_idx)
                                    hitl_arg_streamers.setdefault(_idx, ToolArgTextStreamer())
                                if _idx not in hitl_arg_indexes:
                                    continue
                                _delta = hitl_arg_streamers[_idx].feed(_tcc.get("args") or "")
                                if _delta:
                                    await streamer.send_json({"type": "token", "content": _delta})

                elif stream_evt == "updates":
                    interrupt_payload = find_interrupt_payload(data)
                    if interrupt_payload:
                        return await _emit_human_asked(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            interrupt_payload=interrupt_payload,
                            run_state=run_state,
                            checkpoint_thread_id=checkpoint_thread_id or "",
                            job_id=job_id,
                            streamer=streamer,
                        )
                    for _node, update in data.items():
                        if not isinstance(update, dict):
                            continue
                        messages_update = update.get("messages", [])
                        if isinstance(messages_update, Overwrite):
                            messages_update = messages_update.value or []
                        for msg in messages_update:
                            if getattr(msg, "tool_calls", None):
                                for tc in msg.tool_calls:
                                    tool_name = tc["name"]
                                    tool_args = tc.get("args", {})
                                    _tc_id = tc.get("id")
                                    if _tc_id:
                                        active_tool_calls[_tc_id] = tool_name
                                        if isinstance(tool_args, dict):
                                            pending_tool_args[_tc_id] = tool_args
                                    if tool_name == "request_human_input":
                                        pending_hitl_payload = normalize_hitl_payload(tool_args)
                                    # 파일 산출물 추적: write_file/edit_file 의 대상 경로를 기록해두고
                                    # tool_end 에서 실제 내용을 읽어 file_artifact 로 프론트에 전달한다.
                                    if subprocess_alias_map and tool_name == "task":
                                        _subagent_slug = (tool_args.get("subagent_type") or "") if isinstance(tool_args, dict) else ""
                                        if _tc_id and _subagent_slug:
                                            pending_task_calls[_tc_id] = _subagent_slug
                                    if streamer and tool_name in _FILE_TOOL_NAMES:
                                        _fp = (tool_args.get("file_path") or tool_args.get("path") or "") if isinstance(tool_args, dict) else ""
                                        if (
                                            _fp
                                            and _tc_id
                                            and str(_fp).startswith("/workspace")
                                            and _EVAL_RUN_PATH_MARKER not in str(_fp)
                                        ):
                                            pending_file_ops[_tc_id] = {
                                                "path": str(_fp),
                                                "op": "create" if tool_name == "write_file" else "edit",
                                                "inline": (
                                                    tool_args.get("content")
                                                    if tool_name == "write_file"
                                                    else None
                                                ),
                                            }
                                            workspace_write_made = True
                                            if str(_fp).endswith("process-definition.json"):
                                                process_json_written = True
                                            if str(_fp).endswith("SKILL.md") or "/skills/" in str(_fp):
                                                skill_written = True
                                    await _enqueue_status(
                                        event_queue,
                                        task_id=task_id,
                                        context_id=context_id,
                                        state=TaskState.TASK_STATE_WORKING,
                                        text=json.dumps(
                                            {"tool": tool_name, "args": tool_args, "tool_name": tool_name},
                                            ensure_ascii=False,
                                        ),
                                        event_type="tool_usage_started",
                                        job_id=job_id,
                                    )
                                    if streamer:
                                        await streamer.send_json({
                                            "type": "plan_tools",
                                            "tools": [{"tool": tool_name, "name": tool_name, "args": tool_args}],
                                        })
                                        # 도구 호출 인자에서 출력계약을 잡아 프론트에 process_result 카드로 전달.
                                        # complete_process_generation 은 검증된 '출력'(tool_end)에서 잡으므로 입력으로는 띄우지 않음.
                                        if not process_result_sent and tool_name not in ("complete_process_generation",):
                                            # tool_args 의 raw 문자열 값들(=execute command 의 heredoc JSON 등)을
                                            # 그대로 모아 스캔한다. json.dumps 로 감싸면 내부 JSON 이 escape 돼 못 잡는다.
                                            _arg_strings: list[str] = []

                                            def _collect_strs(v: Any) -> None:
                                                if isinstance(v, str):
                                                    _arg_strings.append(v)
                                                elif isinstance(v, dict):
                                                    for x in v.values():
                                                        _collect_strs(x)
                                                elif isinstance(v, list):
                                                    for x in v:
                                                        _collect_strs(x)

                                            _collect_strs(tool_args)
                                            _pr_in = _detect_process_result("\n".join(_arg_strings))
                                            if _pr_in is not None:
                                                process_result_sent = True
                                                await streamer.send_json({"type": "process_result", "data": _pr_in})
                                        skill_names = _skills_from_tool_args(tool_args)
                                        if skill_names:
                                            await streamer.send_json({"type": "plan_skills", "skills": skill_names})
                                        if tool_name == "write_todos":
                                            todos = tool_args.get("todos") if isinstance(tool_args, dict) else None
                                            if isinstance(todos, list):
                                                await streamer.send_json({"type": "plan_todos", "todos": todos})

                            elif isinstance(msg, ToolMessage):
                                tool_name = active_tool_calls.pop(
                                    msg.tool_call_id, getattr(msg, "name", "unknown")
                                )
                                tool_args_used = pending_tool_args.pop(msg.tool_call_id, {})
                                result_content = msg.content
                                # 채팅: 도구 결과(예: build_hwpx 실행 출력)에서 hwpx 경로를 모아 종료 시 실체화.
                                if is_chat:
                                    try:
                                        for _hp in _HWPX_PATH_RE.findall(str(result_content or "")):
                                            chat_hwpx_paths.add(_hp)
                                    except Exception:
                                        pass
                                await _enqueue_status(
                                    event_queue,
                                    task_id=task_id,
                                    context_id=context_id,
                                    state=TaskState.TASK_STATE_WORKING,
                                    text=json.dumps(
                                        {
                                            "tool": tool_name,
                                            "tool_name": tool_name,
                                            "args": tool_args_used,
                                            "result": result_content,
                                        },
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                    event_type="tool_usage_finished",
                                    job_id=job_id,
                                )
                                if streamer:
                                    # 파일 산출물 실시간 전달: 방금 끝난 write_file/edit_file 의 결과 파일을
                                    # 샌드박스에서 읽어 file_artifact 이벤트로 프론트(파일 UI)에 보낸다.
                                    _fop = pending_file_ops.pop(msg.tool_call_id, None)
                                    if _fop:
                                        _content = await _read_ws_file_content(tenant_id, _fop["path"])
                                        if _content is None:
                                            _content = _fop.get("inline")
                                        if _content is not None:
                                            # 표시는 항상 실제 생성된 파일 그대로(가공 금지). 규격 교정은 검증 단계에서 on-disk 로 한다.
                                            await streamer.send_json(
                                                _file_artifact_payload(_fop["path"], _content, _fop["op"])
                                            )

                                    # 검증/완료 도구는 정규화·결정적 생성(폼/스킬/manifest 등)을 수행한다.
                                    # 방의 모든 산출물 파일을 재전송해 프론트 패널 표시 + 저장 대상에 포함시킨다.
                                    if tool_name in ("validate_process_definition", "complete_process_generation"):
                                        await _reemit_room_artifacts(streamer, chat_room_id or "")

                                    # 완료 도구가 끝나면 5단계 todo 를 모두 completed 로 확정(마지막 todo 미완료 표시 방지).
                                    if tool_name == "complete_process_generation":
                                        complete_called = True
                                    if tool_name == "complete_process_generation" and not str(result_content or "").startswith("ERROR"):
                                        _done_todos = [
                                            {"content": "1. 프로세스 초안 설계 & JSON 생성", "status": "completed"},
                                            {"content": "2. 스킬·에이전트·DMN 선택 & 생성", "status": "completed"},
                                            {"content": "3. 입력 폼 & 참조정보 생성", "status": "completed"},
                                            {"content": "4. 프로세스 검증 & 자동 보정", "status": "completed"},
                                            {"content": "5. 결과 통합 & 완료", "status": "completed"},
                                        ]
                                        await streamer.send_json({"type": "plan_todos", "todos": _done_todos})

                                    # 도구 출력의 출력계약(JSON)을 잡아 프론트에 구조화 이벤트로
                                    # 전달 → 생성 결과 카드(프로세스/스킬/에이전트) 렌더. 저장은 하지 않는다.
                                    if not process_result_sent:
                                        _pr = _detect_process_result(result_content)
                                        if _pr is not None:
                                            process_result_sent = True
                                            await streamer.send_json({"type": "process_result", "data": _pr})

                                if subprocess_alias_map and tool_name == "task":
                                    _subagent_slug = pending_task_calls.pop(msg.tool_call_id, None)
                                    if _subagent_slug:
                                        _child_activity_id = subprocess_alias_map.get(_subagent_slug)
                                        if _child_activity_id:
                                            _child_todo = fetch_child_activity_todo(root_proc_inst_id, _child_activity_id)
                                            if _child_todo:
                                                _child_todo_id = _child_todo["id"]
                                                _child_proc_inst_id = _child_todo.get("proc_inst_id")
                                                _child_form_id = _child_todo.get("form_id")
                                                _child_result_text = extract_json_text(str(result_content)) if _child_form_id else str(result_content or "")
                                                _child_job_id = str(uuid.uuid4())
                                                await _enqueue_status(
                                                    event_queue,
                                                    task_id=_child_todo_id,
                                                    context_id=_child_proc_inst_id,
                                                    state=TaskState.TASK_STATE_WORKING,
                                                    text=json.dumps(
                                                        {"goal": "최종 결과를 반환합니다.", "name": "최종 결과 반환", "role": "최종 결과 반환", "agent_profile": "/images/chat-icon.png"},
                                                        ensure_ascii=False,
                                                    ),
                                                    event_type="task_started",
                                                    job_id=_child_job_id,
                                                    crew_type="result",
                                                )
                                                await _enqueue_status(
                                                    event_queue,
                                                    task_id=_child_todo_id,
                                                    context_id=_child_proc_inst_id,
                                                    state=TaskState.TASK_STATE_COMPLETED,
                                                    text=_child_result_text,
                                                    event_type="task_completed",
                                                    job_id=_child_job_id,
                                                    crew_type="result",
                                                )
                                                from processgpt_agent_sdk.event_queue_process import ProcessEventQueue as _ChildProcessEventQueue
                                                _child_eq = _ChildProcessEventQueue(
                                                    todolist_id=_child_todo_id,
                                                    agent_orch="",
                                                    proc_inst_id=_child_proc_inst_id,
                                                )
                                                _artifact_evt = new_text_artifact_update_event(
                                                    task_id=_child_todo_id,
                                                    context_id=_child_proc_inst_id,
                                                    name="assistant_response",
                                                    text=_child_result_text,
                                                    last_chunk=True,
                                                )
                                                ParseDict({"role": "assistant"}, _artifact_evt.metadata)
                                                await _child_eq.enqueue_event(_artifact_evt)
                                            else:
                                                logger.warning(
                                                    "child todo 없음: subagent=%s activity_id=%s root_proc_inst_id=%s",
                                                    _subagent_slug, _child_activity_id, root_proc_inst_id,
                                                )
                elif stream_evt == "values" and isinstance(data, dict):
                    interrupt_payload = find_interrupt_payload(data)
                    if interrupt_payload:
                        return await _emit_human_asked(
                            event_queue,
                            task_id=task_id,
                            context_id=context_id,
                            interrupt_payload=interrupt_payload,
                            run_state=run_state,
                            checkpoint_thread_id=checkpoint_thread_id or "",
                            job_id=job_id,
                            streamer=streamer,
                        )
                    if not _ns:  # root graph only — ignore subagent intermediate states
                        final_state = data

            if is_chat and chat_room_id and tenant_id:
                try:
                    await a_set_chat_room_context(
                        tenant_id=tenant_id,
                        room_id=chat_room_id,
                        context=_accum.to_context(),
                    )
                except Exception:
                    logger.warning("chat_room context 저장 실패 | room_id=%s", chat_room_id, exc_info=True)

            raw_text = extract_final_text(final_state, streamed_fallback=full_response)

            # 채팅: 스킬이 만든 hwpx 를 office-mcp 로 실체화해 기존 채팅 아티팩트(미리보기/편집)로 띄운다.
            if is_chat and streamer is not None:
                try:
                    await _emit_chat_hwpx_artifacts(
                        streamer, tenant_id, [raw_text, full_response], chat_hwpx_paths
                    )
                except Exception:
                    logger.warning("채팅 hwpx 아티팩트 emit 실패", exc_info=True)

            # 채팅: 그 밖의 산출물은 답변에 컨테이너 경로만 남아 받을 길이 없었다.
            # 비공개 버킷에 보관하고 서명 주소로 바꿔 준다. 레코드(`chat_artifacts`)는
            # 아래에서 메시지에 함께 저장돼, 새로고침해도 주소가 만료돼도 다시 집을 수 있다.
            chat_artifacts: list[dict] = []
            if is_chat:
                try:
                    # 본문에 경로가 적혔든 아니든, 이번 턴에 작업 폴더에 생긴 문서는 거둔다.
                    _doc_paths = await asyncio.to_thread(
                        _room_document_paths, chat_room_id or "", turn_started_ns
                    )
                    raw_text, chat_artifacts = await _publish_chat_artifacts(
                        tenant_id, chat_room_id or context_id or "", raw_text, _doc_paths,
                    )
                except Exception:
                    logger.warning("채팅 산출물 보관 실패 — 원본 유지", exc_info=True)
                # 거둔 문서를 작업 폴더 목록에 띄운다. 바이너리는 write_file 스트림을
                # 타지 않아, 이 emit 이 없으면 목록에서 통째로 빠진다.
                try:
                    await _emit_document_artifacts(streamer, chat_artifacts)
                except Exception:
                    logger.warning("문서 산출물 작업 폴더 emit 실패", exc_info=True)

            # 도구 출력에서 못 잡았을 때 대비: 최종 텍스트에 요약/출력계약 JSON 이 있으면 결과 카드용 이벤트 emit.
            if streamer and not process_result_sent:
                _pr_final = _detect_process_result(raw_text)
                if _pr_final is not None:
                    process_result_sent = True
                    await streamer.send_json({"type": "process_result", "data": _pr_final})

            interrupt_payload = extract_interrupt_payload(final_state) or pending_hitl_payload

            if interrupt_payload:
                return await _emit_human_asked(
                    event_queue,
                    task_id=task_id,
                    context_id=context_id,
                    interrupt_payload=interrupt_payload,
                    run_state=run_state,
                    checkpoint_thread_id=checkpoint_thread_id or "",
                    job_id=job_id,
                    streamer=streamer,
                )

            # 폴백: 에이전트가 process-definition.json 은 만들었으나(이번 턴) complete 를 호출하지 않고
            # 인터럽트 없이 턴을 끝낸 경우 — 스킬 생성 등으로 step 이 소진되면 발생한다.
            # 결과 통합이 누락되므로 executor 가 직접 complete 를 호출해 최종본을 만들고 프론트에 전달한다.
            # On HITL resume the process definition may have been written in the
            # previous turn. Detect the durable room artifact as well as this
            # turn's write_file event, otherwise a resumed process is mistaken
            # for a standalone skill generation and never post-processed.
            if not process_json_written and chat_room_id:
                try:
                    from core.skills.tools import process_work_dirs as _process_work_dirs, _conv_slug
                    from core.skills.tools import tenant_workspace_root as _tenant_ws
                    room_dir = _tenant_ws() / ".bpmn" / _conv_slug(chat_room_id)
                    process_json_written = any(
                        (directory / "process-definition.json").is_file()
                        for directory in _process_work_dirs(room_dir, chat_room_id)
                    )
                except Exception:
                    pass

            if streamer and process_json_written and not complete_called:
                try:
                    from core.skills.tools import (
                        complete_process_generation as _cpg,
                        validate_process_definition as _vpd,
                        _current_conversation_id as _ccid,
                        process_work_dirs as _process_work_dirs,
                        _conv_slug,
                    )
                    from core.skills.tools import tenant_workspace_root as _tenant_ws

                    _ccid.set(chat_room_id or "")
                    _room_dir = _tenant_ws() / ".bpmn" / _conv_slug(chat_room_id or "")
                    _validation_failures = []
                    for _work_dir in _process_work_dirs(_room_dir, chat_room_id or ""):
                        _definition_path = _work_dir / "process-definition.json"
                        if not _definition_path.is_file():
                            continue
                        _vres = await asyncio.to_thread(
                            _vpd.invoke, {"path": str(_definition_path)}
                        )
                        try:
                            _vobj = json.loads(str(_vres or "{}"))
                        except Exception:
                            _vobj = {"passed": False, "remaining_defects": [{"detail": str(_vres)}]}
                        if _vobj.get("passed") is not True:
                            _validation_failures.append({
                                "dir": _work_dir.name,
                                "defects": _vobj.get("remaining_defects") or [],
                            })
                    if _validation_failures:
                        # 정상적인 실패 경로다(예외 아님). 예외로 올려 스택트레이스를 남기는
                        # 대신 사용자에게 무엇이 걸렸는지 한 줄로 알려주고 이 턴을 마친다.
                        complete_called = True  # 이번 턴의 무검증 재시도 방지
                        logger.error(
                            "auto-complete 검증 실패 | room=%s failures=%s",
                            chat_room_id,
                            _validation_failures,
                        )
                        raw_text = (
                            "프로세스 정의가 검증을 통과하지 못해 결과 전달을 보류했어요.\n"
                            + _validation_failure_summary(_validation_failures)
                            + "\n어떻게 고칠지 알려주시면 이어서 수정할게요."
                        )
                    else:
                        _cres = await asyncio.to_thread(_cpg.invoke, {})
                        logger.info("auto-complete | room=%s result=%.140s", chat_room_id, str(_cres))
                        complete_called = True
                        if str(_cres or "").startswith("ERROR"):
                            raw_text = (
                                "프로세스 산출물을 통합하는 중 문제가 생겨 결과 전달을 보류했어요. "
                                "다시 시도하거나 수정할 부분을 알려주세요."
                            )
                        else:
                            await _reemit_room_artifacts(streamer, chat_room_id or "")
                            await streamer.send_json({"type": "plan_todos", "todos": [
                                {"content": "1. 프로세스 초안 설계 & JSON 생성", "status": "completed"},
                                {"content": "2. 스킬·에이전트·DMN 선택 & 생성", "status": "completed"},
                                {"content": "3. 입력 폼 & 참조정보 생성", "status": "completed"},
                                {"content": "4. 프로세스 검증 & 자동 보정", "status": "completed"},
                                {"content": "5. 결과 통합 & 완료", "status": "completed"},
                            ]})
                            if "확인 후 저장" not in (raw_text or ""):
                                raw_text = "프로세스를 생성했어요. 확인 후 저장 버튼을 눌러주세요."
                except Exception:
                    logger.exception("auto-complete 실패")
                    complete_called = True
                    raw_text = (
                        "프로세스 마무리 처리 중 오류가 발생했어요. "
                        "잠시 후 다시 시도하거나 수정할 부분을 알려주세요."
                    )

            # 단독 스킬 생성: process-definition.json 이 없어 위 complete 폴백을 타지 않으므로,
            # 방 산출물(base/skills/*/SKILL.md)을 file_artifact 로 재전송해 프론트 패널 표시 + 저장을 보장한다.
            # 실제로 SKILL.md 를 썼는지(skill_written, 관측된 부작용)로만 판단한다 — 사용자 입력
            # 문장에 "스킬"이 들어있다는 이유만으로(조회성 질문 포함) 응답을 대체하지 않는다.
            if streamer and not process_json_written and not complete_called and skill_written:
                try:
                    await _reemit_room_artifacts(streamer, chat_room_id or "")
                    logger.info("단독 스킬 산출물 재전송 | room=%s", chat_room_id)
                    # PR(병합 요청)을 올린 턴은 사용자가 저장 버튼을 누를 일이 없다. 그런데도
                    # 이 문구로 덮어쓰면 PR 링크와 병합 위치 안내가 사라지고 "저장하라"는
                    # 잘못된 안내만 남는다. 그런 응답은 에이전트의 원문을 그대로 둔다.
                    if "확인 후 저장" not in (raw_text or "") and not _reports_pull_request(raw_text):
                        raw_text = "스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요."
                except Exception:
                    logger.exception("단독 스킬 산출물 재전송 실패")

            # 빈 턴 방지: 모델이 아무 텍스트도 내지 않고(도구 호출도 없이) 끝난 경우
            # 빈 어시스턴트 메시지가 저장돼 사용자 화면에는 "답변했는데 반응이 없는" 상태로 보인다.
            # HITL 응답 직후 재개가 헛돌 때 특히 잘 생긴다 — 무슨 일이 있었는지 짧게라도 남긴다.
            if not str(raw_text or "").strip():
                logger.warning(
                    "빈 응답으로 턴 종료 | room=%s resume=%s pending_interrupt=%s",
                    chat_room_id, use_graph_resume, has_pending_interrupt,
                )
                raw_text = (
                    "이어서 진행하지 못했어요. 한 번만 더 알려주시면 그 지점부터 계속할게요."
                    if human_answer_stripped
                    else "요청을 처리하지 못했어요. 다시 말씀해 주세요."
                )

            await _enqueue_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=json.dumps({"result": raw_text}, ensure_ascii=False),
                event_type="task_completed",
                job_id=job_id,
            )

            result_job_id = str(uuid.uuid4())
            await _enqueue_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=json.dumps(
                    {
                        "goal": "최종 결과를 반환합니다.",
                        "name": "최종 결과 반환",
                        "role": "최종 결과 반환",
                        "agent_profile": "/images/chat-icon.png",
                    },
                    ensure_ascii=False,
                ),
                event_type="task_started",
                job_id=result_job_id,
                crew_type="result",
            )

            final_text = extract_json_text(raw_text) if form_id else raw_text
            # worklist(폼) 산출물에 샌드박스 hwpx 경로가 있으면 office-mcp 로 실체화(storage 업로드 +
            # HTML 변환)해 폼값을 file_url/html_url 로 치환한다 → 프론트 다운로드/미리보기/편집이 동작.
            if form_id:
                try:
                    final_text = await _materialize_hwpx_form_outputs(tenant_id, final_text)
                except Exception:
                    logger.warning("hwpx 폼 산출물 실체화 처리 실패 — 원본 유지", exc_info=True)
                try:
                    final_text = await _materialize_ws_file_form_outputs(
                        tenant_id, final_text, chat_room_id or instance_id or "",
                    )
                except Exception:
                    logger.warning("ws 파일 폼 산출물 실체화 처리 실패 — 원본 유지", exc_info=True)
            # D2D 핸드오프: 단계 완료 시 LLM 최종 응답을 workspace에 저장해 다음 딥에이전트 단계가 직접 읽을 수 있게 한다.
            _save_step_output(raw_text, instance_id, activity_id)
            await _enqueue_status(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=final_text,
                event_type="task_completed",
                job_id=result_job_id,
                crew_type="result",
            )

            artifact_evt = new_text_artifact_update_event(
                task_id=task_id,
                context_id=context_id,
                name="assistant_response",
                text=final_text,
                last_chunk=True,
            )
            _final_meta: dict = {"role": "assistant"}
            if chat_artifacts:
                # 화면이 읽는 이름은 오래된 것(`pdfFiles`)이다. 레코드에는 `file_id` 와
                # 만료 시각이 함께 실려, 주소가 죽은 뒤에도 새 주소를 받아 갈 수 있다.
                _final_meta["pdfFiles"] = [download_file(item) for item in chat_artifacts]
            ParseDict(_final_meta, artifact_evt.metadata)
            await event_queue.enqueue_event(artifact_evt)

            if is_chat and chat_room_id:
                await get_run_registry().mark_done(chat_room_id)
                logger.info(
                    "채팅 완료 | room_id=%s task_id=%s response_len=%d",
                    chat_room_id, task_id, len(final_text),
                )
                if tenant_id:
                    # 이번 턴이 만든/수정한 워크스페이스 파일을 Supabase Storage에 올려서,
                    # 다음 턴이 다른 파드로 라우팅돼도 이어서 읽을 수 있게 한다.
                    try:
                        room_dir = _bpmn_work_dir()
                        if room_dir.exists():
                            _set_storage_tenant_id(tenant_id)
                            result = await asyncio.to_thread(upload_workspace_directory, chat_room_id, room_dir)
                            logger.info("워크스페이스 동기화 | room_id=%s | %s", chat_room_id, result.splitlines()[0] if result else "")
                    except Exception:
                        logger.warning("워크스페이스 동기화 실패(무시) | room_id=%s", chat_room_id, exc_info=True)

        except Exception as e:
            logger.exception("execute 실패: %s", e)
            error_text = f"{type(e).__name__}: {e}"
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text=json.dumps({"error": error_text}, ensure_ascii=False),
                )
            )
            # 오류도 chats 에 남긴다 — SSE 로만 보내면 화면에는 보이지만 레코드가
            # 없어 새로고침/재진입 시 답변이 아예 없던 것처럼 보인다. 최종 아티팩트가
            # 이미 저장된 뒤(예: 뒤처리 중 실패)라면 같은 uuid 를 오류로 덮어쓰지
            # 않도록 건너뛴다.
            # 프로세스/워크리스트 경로는 헬퍼가 알아서 건너뛴다(저장 대상 아님).
            if not getattr(event_queue, "_finalized", False):
                try:
                    await _persist_chat_error_message(
                        context,
                        task_id=task_id,
                        context_id=context_id,
                        error_text=error_text,
                        partial_text=full_response,
                    )
                except Exception:
                    logger.warning(
                        "오류 메시지 chats 저장 실패 | room_id=%s", chat_room_id, exc_info=True
                    )

            # 채팅: error 타입으로 보내야 drain_sse_queue 가 스트림을 종료하고
            # 프론트 onError 가 발동해 오류를 화면에 표시한다(무한 "생각 중..." 방지).
            if streamer:
                await streamer.send_json(
                    {"type": "error", "content": error_text, "error": error_text}
                )

        return None

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = str(context.task_id)
        context_id = str(context.context_id)
        logger.info("cancel task_id=%s", task_id)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="작업이 취소되었습니다.",
            )
        )
