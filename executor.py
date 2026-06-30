from __future__ import annotations

import asyncio
import json
import logging
import re
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
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Overwrite
from typing_extensions import override

from core.agents.agent import build_agent
from core.storage.checkpointer import (
    get_async_postgres_checkpointer,
    get_shared_memory_checkpointer,
    resolve_checkpoint_thread_id,
)
from core.chat.hitl import extract_interrupt_payload, is_duplicate_hitl_request
from core.chat.callbacks import ToolTrackingCallback
from core.agents.input_builder import (
    build_graph_inputs,
    build_user_message,
    collect_recent_messages,
    resolve_chat_room_id,
)
from core.chat.activity import build_fallback_subagent, load_activity_capabilities
from core.chat.chat_room import (
    CtxAccumulator,
    a_set_chat_room_context,
    a_upsert_chat_room_start,
    build_initial_chat_room_context,
)
from core.llm.mcp import ensure_process_gpt_mcp, load_chat_root_tools
from core.skills.git_skill_fetcher import prefetch_git_skills
from core.skills.tools import _skills_from_tool_args, _current_tenant_id as _skill_tenant_id, _current_user_id as _skill_user_id
from core.utils.output_utils import extract_final_text, extract_json_text
from core.llm.token_hygiene import ToolResultTruncationMiddleware
from core.agents.skill_creator_subagents import build_skill_creator_subagents
# 우리 기능(프로세스 생성 HITL 캡처/이름도출)에서 사용하는 contextvar — incoming 에는 없으나 유지.
from core.skills.tools import _current_conversation_id as _skill_conversation_id
from core.skills.tools import _current_user_request as _skill_user_request

logger = logging.getLogger(__name__)

# deepagent 가 샌드박스 workspace 에 생성/수정하는 산출물 파일을 프론트(Claude Desktop식 파일 UI)로
# 실시간 전달하기 위한 설정. write_file/edit_file 도구가 건드린 /workspace 하위 파일을 file_artifact 로 보낸다.
_FILE_TOOL_NAMES = ("write_file", "edit_file")
_FILE_ARTIFACT_MAX_BYTES = 400_000  # 미리보기에 과한 대용량 파일은 잘라 보냄


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


async def _reemit_room_artifacts(streamer, chat_room_id: str) -> None:
    """검증/완료 후 방의 모든 산출물 파일을 file_artifact(edit)로 재전송한다.

    complete 가 결정적으로 만든 forms/·skills/·manifest.json 등은 에이전트의 write_file
    스트림을 거치지 않아 프론트 패널에 안 떠 저장도 누락됐다. 여기서 전부 다시 보내
    패널 표시 + 저장 대상에 포함되게 한다."""
    if streamer is None:
        return
    try:
        from core.skills.tools import bpmn_container_base as _bcb, _conv_slug
        from core.agents.agent import _BASE_DIR as _ABASE
    except Exception:
        return
    base_container = _bcb(chat_room_id or "")  # /workspace/.bpmn/<slug>
    slug = _conv_slug(chat_room_id or "")
    room_host = (_ABASE / "workspace" / ".bpmn" / slug) if slug else (_ABASE / "workspace" / ".bpmn")
    if not room_host.exists():
        return
    proc_dirs = sorted([d for d in room_host.glob("process-*") if d.is_dir()]) or [room_host]
    seen: set = set()
    for pdir in proc_dirs:
        # process-definition.json·manifest.json·agents.json + forms/*.html + skills/**/SKILL.md
        targets = []
        for fn in ("process-definition.json", "manifest.json", "agents.json"):
            fp = pdir / fn
            if fp.is_file():
                targets.append(fp)
        targets += sorted((pdir / "forms").glob("*.html")) if (pdir / "forms").is_dir() else []
        targets += sorted((pdir / "skills").glob("*/SKILL.md")) if (pdir / "skills").is_dir() else []
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


_POSTPROCESS_MARKER = "process-definition-postprocess-result"


def _extract_postprocess_result(text: Any) -> dict | None:
    """run_postprocess.py 가 stdout 으로 낸 최종 요약 JSON(process-definition-postprocess-result)을
    도구 출력/최종 텍스트에서 추출한다. 못 찾으면 None."""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return None
    if _POSTPROCESS_MARKER not in text:
        return None

    # 1) 보통 마지막 stdout 한 줄이 요약 JSON (앞 줄은 로그) — 뒤에서부터 줄 단위 파싱
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or _POSTPROCESS_MARKER not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == _POSTPROCESS_MARKER:
            return obj

    # 2) fallback: 마커를 포함하는 최외곽 중괄호 블록을 균형 맞춰 추출
    idx = text.find(_POSTPROCESS_MARKER)
    start = text.rfind("{", 0, idx)
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                        if isinstance(obj, dict) and obj.get("type") == _POSTPROCESS_MARKER:
                            return obj
                    except Exception:
                        pass
                    break
    return None


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


def _inject_sources_to_workspace(sources: list) -> list:
    """업로드 문서를 Supabase Storage(버킷 'files')에서 받아 샌드박스 workspace 에 넣고
    file_path 를 컨테이너 경로(/workspace/uploads/<name>)로 교체한다. 실패는 베스트에포트(원본 유지)."""
    if not isinstance(sources, list) or not sources:
        return sources or []
    import httpx
    from core.skills.tools import write_upload_to_workspace
    try:
        from core.storage.db import get_supabase
        sb = get_supabase()
    except Exception:
        sb = None
    out: list = []
    for s in sources:
        if not isinstance(s, dict):
            out.append(s)
            continue
        s = dict(s)
        fp = str(s.get("file_path") or "").strip()
        fid = str(s.get("file_id") or "").strip()
        name = str(s.get("file_name") or "").strip() or (fp.rsplit("/", 1)[-1] if fp else fid) or "upload"
        content: bytes | None = None
        try:
            if fp.startswith("http"):
                r = httpx.get(fp, timeout=30)
                if r.status_code < 300:
                    content = r.content
            if content is None and sb is not None:
                keys = [k for k in (fp, fid, (f"uploads/{fp}" if fp and not fp.startswith("uploads/") else "")) if k]
                for key in keys:
                    try:
                        data = sb.storage.from_("files").download(key)
                        if data:
                            content = data
                            break
                    except Exception:
                        pass
        except Exception:
            content = None
        if content:
            try:
                s["file_path"] = write_upload_to_workspace(name, content)
                logger.info("source 샌드박스 주입 | name=%s → %s", name, s["file_path"])
            except Exception:
                logger.warning("source 샌드박스 주입 실패 | name=%s", name, exc_info=True)
        else:
            logger.warning("source 다운로드 실패(원본 file_path 유지) | name=%s fp=%s", name, fp)
        out.append(s)
    return out


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


class DeepAgentExecutor(AgentExecutor):

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> dict | None:
        try:
            await prefetch_git_skills()
        except Exception:
            logger.exception("git-skills: prefetch 실패, Git 스킬 없이 계속 진행")

        task_id = str(context.task_id)
        context_id = str(context.context_id)
        ctx_data = context.get_context_data() or {}
        extras = ctx_data.get("extras", {})
        row = ctx_data.get("row", {})

        tenant_id = str(row.get("tenant_id") or extras.get("tenant_id") or "").strip()
        user_jwt = extras.get("user_jwt")
        user_email = extras.get("user_email")
        user_name = extras.get("user_name")

        _skill_tenant_id.set(tenant_id)
        _skill_user_id.set(str(extras.get("user_id") or row.get("user_id") or "").strip())
        agents = extras.get("agents", [])
        streamer = extras.get("streamer")
        form_id = extras.get("form_id")
        has_file = bool(extras.get("sources"))

        # 채팅 경로(ChatRequestContext)에서는 프론트가 보낸 run_state/human_response_answer 가
        # extras["metadata"] 안에 들어온다. worklist 경로는 extras 최상위/draft 에 둔다.
        # 두 위치를 모두 확인해 채팅에서도 HITL resume 이 트리거되게 한다.
        metadata = extras.get("metadata", {}) if isinstance(extras.get("metadata"), dict) else {}

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

        tenant_mcp: dict[str, Any] = dict(extras.get("tenant_mcp") or {})
        if not tenant_mcp and tenant_id:
            try:
                from processgpt_agent_sdk.database import fetch_tenant_mcp
                fetched = await fetch_tenant_mcp(tenant_id)
                if isinstance(fetched, dict):
                    tenant_mcp = fetched
            except Exception:
                logger.warning("tenant_mcp DB 조회 실패 | tenant_id=%s", tenant_id, exc_info=True)

        is_chat = streamer is not None
        chat_room_id = resolve_chat_room_id(extras, row if isinstance(row, dict) else {})
        # 산출물을 방별 디렉터리(.bpmn/<room>/)로 격리 — 경로 충돌/덮어쓰기 방지.
        _skill_conversation_id.set(chat_room_id or "")

        if is_chat:
            tenant_mcp = ensure_process_gpt_mcp(tenant_mcp, tenant_id)
        chat_root_tools = await load_chat_root_tools(tenant_mcp) if is_chat else []
        _root_mcp_tool_names = [
            getattr(t, "name", "") for t in chat_root_tools if getattr(t, "name", "")
        ]

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

        # 업로드 문서를 Supabase Storage 에서 받아 샌드박스 workspace 로 주입하고,
        # file_path 를 컨테이너 경로(/workspace/uploads/<name>)로 바꿔 에이전트가 직접 read_file 하게 한다.
        injected_sources = _inject_sources_to_workspace(extras.get("sources") or [])
        user_message = build_user_message(
            context.get_user_input(),
            sources=injected_sources,
            form_id=form_id,
            form_fields=extras.get("form_fields"),
        )
        recent_msgs = collect_recent_messages(extras) if is_chat else []
        # 프로세스명 도출용: 이번 사용자 요청 원문을 contextvar 로 전달(complete 가 이름이 비면 사용).
        _user_input_text = ""
        try:
            _user_input_text = str(context.get_user_input() or "")
            _skill_user_request.set(_user_input_text)
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
        has_checkpoint = False
        if checkpointer is not None and checkpoint_thread_id:
            try:
                _cfg = {"configurable": {"thread_id": checkpoint_thread_id}}
                _tup = await checkpointer.aget_tuple(_cfg)
                has_checkpoint = _tup is not None
            except Exception:
                logger.warning("체크포인트 존재 확인 실패 | thread_id=%s", checkpoint_thread_id, exc_info=True)
                has_checkpoint = False

        inputs, use_graph_resume = build_graph_inputs(
            user_message,
            human_answer_stripped=human_answer_stripped,
            run_state=run_state,
            checkpointer=checkpointer,
            checkpoint_thread_id=checkpoint_thread_id,
            recent_messages=recent_msgs,
            has_checkpoint=has_checkpoint,
        )

        if use_graph_resume:
            logger.info("HITL 그래프 재개 | thread_id=%s task_id=%s", checkpoint_thread_id, task_id)
        elif human_answer_stripped:
            logger.info(
                "HITL 비-그래프 재개 | task_id=%s pending_field=%s",
                task_id,
                run_state.get("pending_field") or "human_response",
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

        if not agents:
            proc_def_id = str(row.get("proc_def_id") or "").strip()
            activity_id = str(row.get("activity_id") or "").strip()
            act_tools, act_skills = load_activity_capabilities(
                tenant_id=tenant_id,
                proc_def_id=proc_def_id,
                activity_id=activity_id,
            )
            if act_tools or act_skills:
                fallback = build_fallback_subagent(
                    tenant_id=tenant_id,
                    activity_tools=act_tools,
                    activity_skills=act_skills,
                )
                if fallback:
                    agents = [fallback]
                    logger.info(
                        "서브에이전트 폴백 활성화 | proc_def_id=%s activity_id=%s tools=%s skills=%s",
                        proc_def_id, activity_id, act_tools, act_skills,
                    )
            elif not is_chat:
                logger.warning(
                    "서브에이전트 미설정 (activity fallback 없음) | proc_def_id=%s activity_id=%s",
                    proc_def_id, activity_id,
                )

        _root_mcp_meta: list[dict] = (
            [{"agent": "__root__", "servers": ["process-gpt-mcp"], "tools": _root_mcp_tool_names}]
            if _root_mcp_tool_names else []
        )
        _accum = CtxAccumulator(mcp_tools=_root_mcp_meta)

        sources = extras.get("sources") or []
        knowledge_doc_ids = [
            s.get("file_id", "") for s in sources
            if isinstance(s, dict) and s.get("file_id")
        ]
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

        graph, _, _sub_mcp_tools_meta = await build_agent(
            tenant_id=tenant_id,
            conversation_id=chat_room_id or "",
            checkpointer=checkpointer,
            subagents_raw=agents,
            tenant_mcp=tenant_mcp,
            extra_tools=chat_root_tools,
            middleware=[ToolResultTruncationMiddleware()],
            static_subagents=build_skill_creator_subagents(task_id=task_id),
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
        # write_file/edit_file tool_call_id → {path, op}. tool_end 시 파일을 읽어 file_artifact emit.
        pending_file_ops: dict[str, dict] = {}
        full_response = ""
        final_state: dict[str, Any] = {}
        _seen_msg_namespaces: dict[str, str] = {}
        process_result_sent = False
        # 생성 턴 폴백: 에이전트가 process-definition.json 은 만들었지만 complete 를 호출하지 않고
        # 턴을 끝내는 경우(스킬 생성 등으로 step 소진)가 있어, 스트림 종료 시 자동으로 complete 한다.
        process_json_written = False
        complete_called = False

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

                elif stream_evt == "updates":
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
                                    # 파일 산출물 추적: write_file/edit_file 의 대상 경로를 기록해두고
                                    # tool_end 에서 실제 내용을 읽어 file_artifact 로 프론트에 전달한다.
                                    if streamer and tool_name in _FILE_TOOL_NAMES:
                                        _fp = (tool_args.get("file_path") or tool_args.get("path") or "") if isinstance(tool_args, dict) else ""
                                        _tc_id = tc.get("id")
                                        if _fp and _tc_id and str(_fp).startswith("/workspace"):
                                            pending_file_ops[_tc_id] = {
                                                "path": str(_fp),
                                                "op": "create" if tool_name == "write_file" else "edit",
                                                "inline": (tool_args.get("content") if tool_name == "write_file" else None),
                                            }
                                            if str(_fp).endswith("process-definition.json"):
                                                process_json_written = True
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
                                result_content = msg.content
                                await _enqueue_status(
                                    event_queue,
                                    task_id=task_id,
                                    context_id=context_id,
                                    state=TaskState.TASK_STATE_WORKING,
                                    text=json.dumps(
                                        {"tool": tool_name, "tool_name": tool_name, "result": result_content},
                                        ensure_ascii=False,
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

                                    # run_postprocess 가 낸 저장/검증 요약(JSON)을 도구 출력에서 잡아
                                    # 프론트에 구조화 이벤트로 전달 → 생성 결과 카드(프로세스/스킬/에이전트) 렌더.
                                    if not process_result_sent:
                                        _pr = _detect_process_result(result_content)
                                        if _pr is not None:
                                            process_result_sent = True
                                            await streamer.send_json({"type": "process_result", "data": _pr})

                elif stream_evt == "values" and isinstance(data, dict):
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

            # 도구 출력에서 못 잡았을 때 대비: 최종 텍스트에 요약/출력계약 JSON 이 있으면 결과 카드용 이벤트 emit.
            if streamer and not process_result_sent:
                _pr_final = _detect_process_result(raw_text)
                if _pr_final is not None:
                    process_result_sent = True
                    await streamer.send_json({"type": "process_result", "data": _pr_final})

            interrupt_payload = extract_interrupt_payload(final_state)

            # 컨설팅 강제 HITL(resume 호환) — 모델이 비일관적으로 request_human_input 을 빠뜨리고
            # 초안을 평문으로 끝내는 경우, 같은 그래프 thread 로 한 번 더 돌려 강제로 도구를 호출시켜
            # '진짜' interrupt 를 만든다. 아직 생성 전(process-definition.json 미작성)이라 프로세스가
            # 중복 생성되지 않으며, 이후 승인은 진짜 graph resume(Command) 으로 처리되어 단일 생성이 보장된다.
            if (
                not interrupt_payload
                and is_chat
                and not process_json_written
                and not complete_called
            ):
                _txt = (raw_text or "").strip()
                _steps = len(re.findall(r"(?m)^\s*\d+\s*[\.\)]\s", _txt))
                if len(_txt) > 100 and (_steps >= 2 or re.search(r"진행|승인|반려|초안|어떻게|수정|바꿀|추가하거나", _txt)):
                    logger.info(
                        "컨설팅 강제 HITL: request_human_input 미호출 → 같은 thread 로 강제 interrupt 생성 | room=%s",
                        chat_room_id,
                    )
                    forced_inputs = {
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "방금 작성한 프로세스 초안을 평문으로 끝내지 말고, 지금 즉시 `request_human_input` 도구를 호출해 "
                                    "승인/반려를 물어라. question 인자에는 '이대로 진행할까요? 추가하거나 바꿀 단계가 있으면 알려주세요.' 만, "
                                    "context 인자에는 위 초안 전체(번호 단계·담당 역할·분기 포함)를 담아라. "
                                    "다른 평문 출력이나 프로세스 정의 생성은 하지 말고, 오직 request_human_input 도구만 호출하라."
                                ),
                            }
                        ]
                    }
                    try:
                        forced_state = await graph.ainvoke(forced_inputs, config=config)
                        _forced_ip = extract_interrupt_payload(forced_state)
                        if _forced_ip:
                            interrupt_payload = _forced_ip
                            final_state = forced_state
                    except Exception:
                        logger.warning("컨설팅 강제 HITL 2차 패스 실패", exc_info=True)

            if interrupt_payload:
                question = (
                    interrupt_payload.get("question") if isinstance(interrupt_payload, dict) else None
                ) or "이 작업을 계속 진행하려면 추가 입력이 필요합니다."
                context_text = (
                    interrupt_payload.get("context", "") if isinstance(interrupt_payload, dict) else ""
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
                # 채팅 경로: ChatEventQueue 는 TaskStatusUpdateEvent(human_asked)를 무시하고,
                # interrupt 후 done/error 가 없으면 SSE 가 끝나지 않아 프론트가 무한 대기한다.
                # 그래서 질문/컨텍스트/run_state 를 담은 done 메시지를 streamer 로 보내 스트림을
                # 정상 종료시키고, 프론트가 이 마커로 HITL 패널을 띄우고 같은 세션으로 resume 한다.
                if streamer:
                    hitl_content = json.dumps(
                        {
                            "type": "human_input_required",
                            "question": question,
                            "context": context_text,
                            "run_state": new_run_state,
                        },
                        ensure_ascii=False,
                    )
                    await streamer.send_json({"type": "done", "content": hitl_content})

                if is_duplicate:
                    logger.info("중복 HITL 요청 억제 | task_id=%s", task_id)
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
                            "type": "text",
                            "run_state": new_run_state,
                        },
                        ensure_ascii=False,
                    ),
                    event_type="human_asked",
                    job_id=job_id,
                )
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

            # 폴백: 에이전트가 process-definition.json 은 만들었으나(이번 턴) complete 를 호출하지 않고
            # 인터럽트 없이 턴을 끝낸 경우 — 스킬 생성 등으로 step 이 소진되면 발생한다.
            # 결과 통합이 누락되므로 executor 가 직접 complete 를 호출해 최종본을 만들고 프론트에 전달한다.
            if streamer and process_json_written and not complete_called:
                try:
                    from core.skills.tools import (
                        complete_process_generation as _cpg,
                        _current_conversation_id as _ccid,
                    )

                    _ccid.set(chat_room_id or "")
                    _cres = await asyncio.to_thread(_cpg.invoke, {})
                    logger.info("auto-complete(폴백) | room=%s result=%.140s", chat_room_id, str(_cres))
                    complete_called = True
                    if not str(_cres or "").startswith("ERROR"):
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
                    logger.exception("auto-complete(폴백) 실패")

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

            if is_chat and chat_room_id:
                logger.info(
                    "채팅 완료 | room_id=%s task_id=%s response_len=%d",
                    chat_room_id, task_id, len(final_text),
                )

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
