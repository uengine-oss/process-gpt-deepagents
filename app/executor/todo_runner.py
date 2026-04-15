"""TODO utilities and (optional) worker-style todo execution helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from app.executor.a2a_events import _enqueue_status
from app.executor.context import ExecutionContext
from app.executor.tool_instrumentation import _instrument_tools
from app.executor.workitem_text import _extract_plain_text
from app.runtime_config import ModelRoutingConfig
from app.skills.volume_tool_events import skill_volume_active_exec_ctx, skill_volume_active_job_id

logger = logging.getLogger(__name__)

# File-ish input types that require an explicit extract step first.
_FILE_INPUT_TYPES: set[str] = {"pdf", "xlsx", "csv", "docx", "html"}

# Tools that are NOT considered "execution/output-producing" on their own.
# If a todo run only uses these, we treat it as an execution failure per spec.
_NON_OUTPUT_TOOL_NAMES: set[str] = {
    "mem0",
    "memento",
    "dmn_rule",
    "find_helpful_skills",
    "list_skills",
    "read_skill_document",
}

_FILE_OUTPUT_TYPES: set[str] = {"xlsx", "pdf", "docx", "csv", "zip", "file", "artifact"}


def _is_file_output_type(output_type: str) -> bool:
    return (output_type or "").strip().lower() in _FILE_OUTPUT_TYPES


def _normalized_todos(value: Any) -> list[dict[str, Any]]:
    """Best-effort normalize DeepAgents todo state to list[dict]."""
    if value is None:
        return []
    parsed: Any = value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
    if isinstance(parsed, dict) and isinstance(parsed.get("todos"), list):
        parsed = parsed["todos"]
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            out.append(dict(item))
        elif isinstance(item, str) and item.strip():
            out.append({"id": str(uuid.uuid4()), "content": item.strip(), "status": "pending"})
    return out


def _todo_id(todo: dict[str, Any]) -> str:
    tid = str(todo.get("id") or "").strip()
    return tid or str(uuid.uuid4())


def _todo_status(todo: dict[str, Any]) -> str:
    return str(todo.get("status") or "").strip() or "pending"


def _todo_content(todo: dict[str, Any]) -> str:
    return str(todo.get("content") or "").strip()


def _extract_io_types(todo: dict[str, Any]) -> tuple[str, str]:
    """Return (input_type, output_type) from todo fields or content markers.

    Accepted:
    - todo["input_type"] / todo["output_type"]
    - content markers: [IN=...] [OUT=...]
    - content pairs: input_type: ... / output_type: ...
    """
    it = str(todo.get("input_type") or "").strip()
    ot = str(todo.get("output_type") or "").strip()
    if it and ot:
        return it, ot
    content = _todo_content(todo)
    if content:
        m_in = re.search(r"\[IN\s*=\s*([^\]]+)\]", content, flags=re.IGNORECASE)
        m_out = re.search(r"\[OUT\s*=\s*([^\]]+)\]", content, flags=re.IGNORECASE)
        if not it and m_in:
            it = (m_in.group(1) or "").strip()
        if not ot and m_out:
            ot = (m_out.group(1) or "").strip()
        if not it:
            m = re.search(r"\binput_type\s*[:=]\s*([a-z0-9_\-]+)\b", content, flags=re.IGNORECASE)
            if m:
                it = (m.group(1) or "").strip()
        if not ot:
            m = re.search(r"\boutput_type\s*[:=]\s*([a-z0-9_\-]+)\b", content, flags=re.IGNORECASE)
            if m:
                ot = (m.group(1) or "").strip()
    return it, ot


def _infer_pipeline_stage(input_type: str, output_type: str) -> str:
    """Infer pipeline stage for ordering: extract → structure → generate."""
    it = (input_type or "").strip().lower()
    ot = (output_type or "").strip().lower()
    if ot == "json" and (it in _FILE_INPUT_TYPES or it == "url"):
        return "extract"
    if it == "json" and ot == "json":
        return "structure"
    return "generate"


def _validate_pipeline_requirements(runnable: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Validate required pipeline steps for file/template workflows."""
    if not runnable:
        return None
    staged: list[tuple[str, dict[str, Any]]] = []
    any_file_input = False
    any_xlsx_generate = False
    has_structure = False
    for td in runnable:
        it, ot = _extract_io_types(td)
        it_l = (it or "").strip().lower()
        ot_l = (ot or "").strip().lower()
        if it_l in _FILE_INPUT_TYPES:
            any_file_input = True
        if ot_l == "xlsx":
            any_xlsx_generate = True
        st = _infer_pipeline_stage(it_l, ot_l)
        staged.append((st, td))
        if st == "structure":
            has_structure = True

    if any_file_input:
        first_stage = staged[0][0]
        if first_stage != "extract":
            return {
                "error": "pipeline_order_violation",
                "message": "파일 입력이 존재하면 첫 TODO는 extract(구조 추출) 단계여야 합니다.",
                "required_first_stage": "extract",
                "found_first_stage": first_stage,
            }

    if any_xlsx_generate and not has_structure:
        return {
            "error": "missing_template_definition",
            "message": "xlsx 생성(OUT=xlsx)이 포함되면 structure(template 정의) 단계(IN=json OUT=json)가 반드시 필요합니다.",
        }

    return None


def _select_todos_to_execute(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick todos that must be executed now.

    Spec:
    - Execute todos that are status=in_progress
    - Also execute newly created todos (treated as pending)
    """
    runnable: list[dict[str, Any]] = []
    for td in todos:
        st = _todo_status(td)
        if st in ("completed", "failed", "cancelled"):
            continue
        if st in ("in_progress", "pending"):
            runnable.append(td)
    return runnable


def _tokenize(text: str) -> set[str]:
    blob = (text or "").lower()
    blob = re.sub(r"[^a-z0-9가-힣_\\-\\s]", " ", blob)
    parts = [p.strip() for p in re.split(r"\\s+", blob) if p.strip()]
    return set(parts)


def _capability_signature(sa: dict[str, Any]) -> str:
    """Build a capability-only signature string (no agent name/role/description)."""
    caps = sa.get("capabilities") if isinstance(sa, dict) else None
    skills: list[str] = []
    declared_tools: list[str] = []
    model = ""
    if isinstance(caps, dict):
        skills = [str(x).strip() for x in (caps.get("skills") or []) if str(x).strip()]
        declared_tools = [str(x).strip() for x in (caps.get("tools") or []) if str(x).strip()]
        model = str(caps.get("model") or "").strip()
    # Also include attached tool object names (still capability, not agent identity).
    attached = []
    for t in (sa.get("tools") or []) if isinstance(sa, dict) else []:
        nm = getattr(t, "name", None) or type(t).__name__
        attached.append(str(nm))
    parts = []
    if model:
        parts.append(model)
    parts.extend(skills)
    parts.extend(declared_tools)
    parts.extend(attached)
    return " ".join(parts)


def route_todo_to_subagent(
    *,
    todo: dict[str, Any],
    subagents: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Capability-based routing (no agent name/role/keyword hardcoding)."""
    if not subagents:
        return None
    content = _todo_content(todo)
    tokens = _tokenize(content)

    def score(sa: dict[str, Any]) -> int:
        sig = _capability_signature(sa).lower()
        sig_tokens = _tokenize(sig)
        overlap = len(tokens & sig_tokens)
        # Prefer agents with more declared capabilities when overlap ties (more general).
        cap_size = len(sig_tokens)
        return overlap * 10 + min(cap_size, 200)

    ranked = sorted(subagents, key=score, reverse=True)
    best = ranked[0] if ranked else None
    return best


async def run_todo_subagent_job(
    *,
    td: dict[str, Any],
    results: dict[str, Any],
    invoke_user_text: str,
    model_name: str,
    subagents: list[dict[str, Any]],
    name_to_exec_ctx: dict[str, ExecutionContext],
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    task_label: str = "TODO",
    name_to_subagent_job_id: dict[str, str] | None = None,
    shared_job_tool_log: dict[str, list[str]] | None = None,
    shared_job_skill_log: dict[str, list[str]] | None = None,
    model_routing: ModelRoutingConfig | None = None,
) -> None:
    """Execute a single todo-shaped task dict; mutates ``td`` and writes ``results[tid]``."""
    tid = _todo_id(td)
    td["id"] = tid
    td["status"] = "in_progress"

    input_type, output_type = _extract_io_types(td)
    if not input_type or not output_type:
        td["status"] = "failed"
        results[tid] = {
            "status": "failed",
            "error": "missing_io_types",
            "required": ["input_type", "output_type"],
            "hint": "todo.content에 [IN=<입력타입>][OUT=<출력타입>]를 포함해야 합니다.",
        }
        return
    td["input_type"] = input_type
    td["output_type"] = output_type

    sa = route_todo_to_subagent(todo=td, subagents=subagents)
    if not isinstance(sa, dict) or not sa:
        td["status"] = "failed"
        results[tid] = {"status": "failed", "error": "no_subagent_routed"}
        return

    sa_name = str(sa.get("name") or "").strip() or "(unknown)"
    sub_exec_ctx = name_to_exec_ctx.get(sa_name)
    if sub_exec_ctx is None:
        sub_exec_ctx = ExecutionContext(
            correlation_id=str(uuid.uuid4()),
            agent_path=[sa_name],
            task_id=task_id or None,
            proc_inst_id=proc_inst_id or None,
        )
    stable_jid = ""
    if name_to_subagent_job_id:
        stable_jid = str(name_to_subagent_job_id.get(sa_name) or "").strip()
    job_id = stable_jid or str(td.get("job_id") or "").strip() or str(uuid.uuid4())
    td["job_id"] = job_id
    try:
        _enqueue_status(
            event_queue,
            "task_started",
            {
                "message": f"{task_label} 실행 시작: {(_todo_content(td)[:200] + '…') if len(_todo_content(td)) > 200 else _todo_content(td)}",
                "role": None,
                "name": sa_name,
                "goal": _todo_content(td),
            },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_id,
            exec_ctx=sub_exec_ctx,
            logical_event_type="agent_started",
            phase="reasoning",
            agent_name=sa_name,
        )
    except Exception:
        logger.exception("Failed to enqueue todo task_started", exc_info=True)

    try:
        from app.agent.factory import build_runtime_agent
    except Exception as exc:
        td["status"] = "failed"
        results[tid] = {"status": "failed", "error": f"build_runtime_agent_unavailable: {exc}"}
        return

    job_tool_log: dict[str, list[str]] = shared_job_tool_log if shared_job_tool_log is not None else {}
    job_skill_log: dict[str, list[str]] = shared_job_skill_log if shared_job_skill_log is not None else {}
    tool_outputs_by_name: dict[str, list[dict[str, Any]]] = {}
    tools_for_worker = list(sa.get("tools") or [])

    def _tool_result_hook(
        *,
        base_tool: Any,
        job_id: str,
        tool_input: Any,
        tool_output: Any,
        error: Exception | None,
    ) -> None:
        tname = str(getattr(base_tool, "name", type(base_tool).__name__) or "")
        bucket = tool_outputs_by_name.setdefault(tname, [])
        bucket.append(
            {
                "input": tool_input,
                "output": tool_output,
                "error": str(error) if error is not None else None,
            }
        )

    def _todo_job_id_getter(jid: str):
        def _inner() -> str:
            active = skill_volume_active_job_id.get()
            return active if active else jid

        return _inner

    _instrument_tools(
        tools_for_worker,
        todo_id=task_id or None,
        proc_inst_id=proc_inst_id or None,
        crew_type="action",
        job_id_getter=_todo_job_id_getter(job_id),
        job_tool_log=job_tool_log,
        job_skill_log=job_skill_log,
        tool_result_hook=_tool_result_hook,
        exec_ctx=sub_exec_ctx,
        event_queue=event_queue,
        tool_input_augmenter=None,
        tool_call_guard=None,
        context_tool_tracker=None,
    )

    worker = build_runtime_agent(
        model_name=model_name,
        system_prompt=str(sa.get("system_prompt") or ""),
        subagents=[],
        tools=tools_for_worker,
        hitl_tools=[],
        middleware=[],
        skills=None,
        memory=None,
        model_routing=model_routing,
    )

    todo_user_msg = (
        "아래는 동일 워크아이템의 원문 컨텍스트입니다.\n\n"
        "[WORKITEM]\n"
        f"{(invoke_user_text or '').strip()}\n\n"
        f"[{task_label}]\n"
        f"{_todo_content(td)}\n"
        "\n\n[TODO_EXECUTION_RULES]\n"
        "- 당신은 반드시 최소 1개 이상의 도구(tool)를 호출해 실제 실행/산출을 만들어야 합니다.\n"
        "- 단순 조사/설명만 하고 도구를 호출하지 않으면 실패로 처리됩니다.\n"
        "- 가능한 경우 샌드박스 실행/파일 생성/파싱/변환 등 결과물을 생성하세요.\n"
        f"- 이 작업의 입력 데이터 타입: {input_type}\n"
        f"- 이 작업의 출력 데이터 타입: {output_type}\n"
        "- 출력 타입이 xlsx/pdf/docx/csv/zip/file/artifact 중 하나라면, 반드시 아래 체인을 완료해야 합니다:\n"
        "  1) 산출물 파일 생성\n"
        "  2) 파일 존재 여부 및 크기 검증\n"
        "  3) `sandbox_upload_file_to_public_storage(workspace_id, relative_path, tenant_id)`로 업로드\n"
        "  4) 반환된 `public_url`을 최종 출력에 포함\n"
    )

    out_state: Any = None
    err: Exception | None = None
    tok_job = skill_volume_active_job_id.set(job_id)
    tok_ex = skill_volume_active_exec_ctx.set(sub_exec_ctx)
    try:
        out_state = await worker.ainvoke({"messages": [{"role": "user", "content": todo_user_msg}]})
    except Exception as exc:
        err = exc
    finally:
        skill_volume_active_exec_ctx.reset(tok_ex)
        skill_volume_active_job_id.reset(tok_job)

    if err is not None:
        td["status"] = "failed"
        results[tid] = {"status": "failed", "agent": sa_name, "error": str(err)}
        try:
            _enqueue_status(
                event_queue,
                "task_completed",
                {"message": f"{task_label} 실행 실패", "error": str(err), "name": sa_name},
                proc_inst_id,
                task_id,
                "action",
                job_id=job_id,
                exec_ctx=sub_exec_ctx,
                logical_event_type="agent_finished",
                phase="reasoning",
                agent_name=sa_name,
            )
        except Exception:
            logger.exception("Failed to enqueue todo task_completed (error)", exc_info=True)
        return

    used = list(dict.fromkeys(job_tool_log.get(job_id, []) or []))
    if not used:
        td["status"] = "failed"
        results[tid] = {
            "status": "failed",
            "agent": sa_name,
            "job_id": job_id,
            "error": "no_tool_calls",
        }
        return
    used_non_output = [u for u in used if u in _NON_OUTPUT_TOOL_NAMES]
    if len(used_non_output) == len(used):
        td["status"] = "failed"
        results[tid] = {
            "status": "failed",
            "agent": sa_name,
            "job_id": job_id,
            "error": "no_output_producing_tool_used",
            "tools_used": used,
        }
        return

    if _is_file_output_type(output_type):
        uploads = tool_outputs_by_name.get("sandbox_upload_file_to_public_storage") or []
        public_urls: list[str] = []
        for ent in uploads:
            out = ent.get("output")
            if isinstance(out, dict) and isinstance(out.get("public_url"), str) and out["public_url"].strip():
                public_urls.append(out["public_url"].strip())
        if not public_urls:
            td["status"] = "failed"
            results[tid] = {
                "status": "failed",
                "agent": sa_name,
                "job_id": job_id,
                "error": "missing_public_upload",
                "message": (
                    "파일 산출 작업은 `sandbox_upload_file_to_public_storage` 업로드 및 "
                    "`public_url` 반환 전까지 완료로 간주하지 않습니다."
                ),
                "tools_used": used,
            }
            return

    text = ""
    try:
        if isinstance(out_state, dict):
            msgs = out_state.get("messages") or []
            if msgs:
                last = msgs[-1]
                content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
                text = _extract_plain_text(content)
    except Exception:
        text = ""

    td["status"] = "completed"
    results[tid] = {
        "status": "completed",
        "agent": sa_name,
        "job_id": job_id,
        "output": text,
        "tools_used": used,
    }
    if _is_file_output_type(output_type):
        uploads = tool_outputs_by_name.get("sandbox_upload_file_to_public_storage") or []
        for ent in reversed(uploads):
            out = ent.get("output")
            if isinstance(out, dict) and isinstance(out.get("public_url"), str) and out["public_url"].strip():
                results[tid]["public_url"] = out["public_url"].strip()
                results[tid]["storage_path"] = out.get("storage_path")
                results[tid]["name"] = out.get("name")
                results[tid]["size_bytes"] = out.get("size_bytes")
                break
    try:
        _enqueue_status(
            event_queue,
            "task_completed",
            {"message": f"{task_label} 실행 완료", "name": sa_name},
            proc_inst_id,
            task_id,
            "action",
            job_id=job_id,
            exec_ctx=sub_exec_ctx,
            logical_event_type="agent_finished",
            phase="reasoning",
            agent_name=sa_name,
        )
    except Exception:
        logger.exception("Failed to enqueue todo task_completed", exc_info=True)


async def execute_todos(
    *,
    todos_state: Any,
    invoke_user_text: str,
    model_name: str,
    subagents: list[dict[str, Any]],
    name_to_exec_ctx: dict[str, ExecutionContext],
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    max_concurrency: int = 4,
    name_to_subagent_job_id: dict[str, str] | None = None,
    shared_job_tool_log: dict[str, list[str]] | None = None,
    shared_job_skill_log: dict[str, list[str]] | None = None,
    model_routing: ModelRoutingConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute runnable todos (fan-out), updating status and collecting results."""
    todos = _normalized_todos(todos_state)
    runnable = _select_todos_to_execute(todos)
    if not runnable:
        return todos, {"executed": 0, "results": {}}

    results: dict[str, Any] = {}

    violation = _validate_pipeline_requirements(runnable)
    if violation is not None:
        for td in runnable:
            tid = _todo_id(td)
            td["id"] = tid
            td["status"] = "failed"
            results[tid] = {"status": "failed", **violation}
        return todos, {"executed": 0, "results": results, "pipeline_violation": violation}

    buckets: dict[str, list[dict[str, Any]]] = {"extract": [], "structure": [], "generate": []}
    for td in runnable:
        it, ot = _extract_io_types(td)
        buckets[_infer_pipeline_stage(it, ot)].append(td)
    stage_order = ["extract", "structure", "generate"]

    async def _run_one(td: dict[str, Any], sem: asyncio.Semaphore) -> None:
        async with sem:
            await run_todo_subagent_job(
                td=td,
                results=results,
                invoke_user_text=invoke_user_text,
                model_name=model_name,
                subagents=subagents,
                name_to_exec_ctx=name_to_exec_ctx,
                event_queue=event_queue,
                proc_inst_id=proc_inst_id,
                task_id=task_id,
                task_label="TODO",
                name_to_subagent_job_id=name_to_subagent_job_id,
                shared_job_tool_log=shared_job_tool_log,
                shared_job_skill_log=shared_job_skill_log,
                model_routing=model_routing,
            )

    executed_total = 0
    for st in stage_order:
        group = buckets.get(st) or []
        if not group:
            continue
        sem = asyncio.Semaphore(max(1, int(max_concurrency or 1)))
        await asyncio.gather(*[_run_one(td, sem) for td in group])
        executed_total += len(group)
    return todos, {"executed": executed_total, "results": results, "pipeline_order": stage_order}

