"""DeepAgents executor: ProcessGPT A2A 래퍼 + DeepAgents orchestrator."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from typing import Any, Mapping

from langgraph.types import Command
from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from app.database import (
    a_update_chat_room_context,
    a_upsert_chat_room_start,
    get_latest_human_response,
    build_initial_chat_room_context,
    _load_proc_def_activity_capabilities
)
from app.agent.backend import (
    tenant_memory_sources_for_middleware,
    tenant_skill_sources_for_middleware,
)
from app.agent.factory import (
    base_orchestrator_identity_prompt,
    build_orchestrator,
    build_orchestrator_prompt_sections,
    default_orchestrator_prompt,
)
from app.execution_context import format_execution_clock_section
from app.agent.hitl import extract_interrupt_payload, get_hitl_tools
from app.output_utils import (
    convert_agent_output,
    ensure_trailing_newline,
    sanitize_deliverable_form_fields,
    unwrap_output_wrapper_result_text,
)
from app.sandbox.output_delivery import (
    collect_readable_artifacts,
    default_artifact_globs,
    enqueue_sandbox_file_artifacts,
    sandbox_output_paths_begin_task,
    sandbox_output_paths_discard_task,
    take_sandbox_output_paths_task,
)
from app.sandbox.tools import collect_task_open_workspace_paths_for_globs, get_sandbox_tools
from app.runtime_config import ModelRoutingConfig, resolve_model_routing
from app.skills.resolution_tools import get_skill_resolution_tools
from app.skills.github_skill_fetcher import prefetch_github_skills
from app.skills.catalog_tools import get_skill_tools
from app.skills.volume_subagent_scope_middleware import SubagentJobScopeMiddleware
from app.skills.volume_tool_events import (
    SkillVolumeToolEventBinding,
    skill_volume_active_exec_ctx,
    skill_volume_active_job_id,
    skill_volume_tool_event_binding,
)
from app.agent.subagents import (
    build_subagent_configs,
    load_adhoc_agent_rows,
    parse_skills_csv,
)
from app.tooling.tagging_safe_tool_loader import TaggingSafeToolLoader
from processgpt_agent_utils.utils.context_manager import set_context

from app.executor.a2a_events import (
    enqueue_artifact_update,
    enqueue_status_update,
    enqueue_event,
    new_artifact_update_event,
    new_status_update_event,
    bind_executor_asyncio_loop_for_enqueue,
    reset_executor_asyncio_loop_for_enqueue,
)
from app.executor.context import ExecutionContext, _build_execution_evidence, build_event_payload
from app.executor.exceptions import HitlInterrupt, MissingSubagentsConfigurationError
from app.executor.tool_calls import _json_safe
from app.executor.hitl_utils import (
    _classify_tenant_context_tool,
    _is_duplicate_hitl_request,
    _resolve_pending_field_from_decision,
    _should_defer_hitl_interrupt,
)
from app.executor.langchain_tools import _ensure_langchain_tools
from app.executor.execution_state_validator import (
    messages_to_audit_blob,
    strict_pipeline_state_violations,
    validate_native_execution_contract,
)
from app.executor.input_assets import build_asset_inventory, collect_input_assets
from app.executor.asset_role_resolver import format_pipeline_asset_roles_for_system_prompt, resolve_asset_roles
from app.executor.compact_tool_result_middleware import create_compact_tool_result_middleware
from app.executor.oss_channel_tool_call_middleware import create_oss_channel_tool_call_middleware
from app.executor.pipeline_spec_middleware import create_pipeline_spec_middleware
from app.executor.deep_required_root_gate_middleware import _create_deep_required_root_gate_middleware
from app.executor.planning_middleware import _create_write_todos_planning_middleware
from app.executor.prompt_sections_middleware import create_prompt_sections_middleware
from app.executor.subagent_prompt_sections import build_subagent_prompt_sections
from app.executor.subagent_prompt_sections_middleware import create_subagent_prompt_sections_middleware
from app.executor.token_hygiene_tool_result_middleware import create_token_hygiene_tool_result_middleware
from app.executor.tool_result_projection_middleware import (
    create_tool_result_projection_middleware_with_rules,
)
from app.executor.subagent_middleware import (
    _create_subagent_event_middleware,
    _emit_subagent_timeline_fallback,
    _reconcile_partial_subagent_task_completed,
)
from app.executor.domain_agent_mcp_prefetch import extend_mcp_request_with_domain_agent_declared_servers
from app.executor.tenant_context import (
    _mcp_config_without_blocked_servers,
)
from app.executor.postgres_checkpointer import (
    get_async_postgres_checkpointer,
    resolve_checkpoint_thread_id,
)
from app.executor.tool_instrumentation import (
    _instrument_tools,
    http_chat_tool_display_name,
)
from app.executor.workitem_text import (
    _build_form_event_result,
    _coerce_tool_input_dict,
    _extract_form_field_keys,
    _extract_plain_text,
    _extract_result_block,
    _is_korean_user_input,
    _join_todo_contents,
    cut_index_after_leading_workitem_echo_for_stream,
    parse_workitem_query,
)
from app.executor.form_prefill import (
    loose_json_loads as _loose_json_loads,
    prefill_form_data_from_structured_outputs as _prefill_form_data_from_structured_outputs,
)

logger = logging.getLogger(__name__)

def _get_extras(context: RequestContext) -> dict[str, Any]:
    try:
        data = context.get_context_data() or {}
    except Exception:
        return {}
    extras = data.get("extras")
    return extras if isinstance(extras, dict) else {}


def _is_chat_request(context: RequestContext) -> bool:
    extras = _get_extras(context)
    kind = extras.get("request_kind")
    return isinstance(kind, str) and kind.strip().lower() == "chat"

def _resolve_chat_room_id(extras: dict[str, Any], row: dict[str, Any]) -> str:
    """Best-effort resolve stable chat room id (conversation_id) for chat persistence."""
    for key in ("conversation_id", "room_id", "chat_room_id"):
        v = extras.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    v2 = row.get("conversation_id")
    if isinstance(v2, str) and v2.strip():
        return v2.strip()
    return ""


async def _emit_chunk_text(context: RequestContext, text: str) -> bool:
    extras = _get_extras(context)
    streamer = extras.get("streamer")
    send_text = getattr(streamer, "send_text", None)
    if callable(send_text):
        res = send_text(text)
        if inspect.isawaitable(res):
            await res
        return True
    return False


async def _emit_chunk_json(context: RequestContext, payload: Any) -> bool:
    extras = _get_extras(context)
    streamer = extras.get("streamer")
    send_json = getattr(streamer, "send_json", None)
    if callable(send_json):
        res = send_json(payload)
        if inspect.isawaitable(res):
            await res
        return True
    return False


_ENV_HTTP_CHAT_ENABLE_TENANT_CONTEXT_PREFLIGHT = "DEEPAGENTS_HTTP_CHAT_ENABLE_TENANT_CONTEXT_PREFLIGHT"


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


# HTTP chat: work-assistant MCP tools on the *root* orchestrator must stay small.
# OpenAI/LiteLLM counts tool *schemas* toward input tokens; dozens of MCP tools can exceed
# small-context models (~21k tokens) even when the system prompt string is short.
_HTTP_CHAT_WORK_ASSISTANT_SCHEMA_WEIGHT_BUDGET = 18_000
_HTTP_CHAT_MAX_WORK_ASSISTANT_ROOT_TOOLS = 8


def _is_work_assistant_mcp_tool_obj(obj: Any) -> bool:
    """True when the tool was loaded from the work-assistant MCP server (see TaggingSafeToolLoader)."""
    srv = str(getattr(obj, "_mcp_server", "") or "").strip().lower().replace("_", "-")
    return srv == "work-assistant" or srv.endswith("work-assistant")


def _identity_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _normalize_identity_field_key(key: str) -> str | None:
    """Map request / metadata keys to canonical work-assistant arg names."""
    k = str(key or "").strip()
    if not k:
        return None
    lk = k.lower().replace("-", "_")
    if lk in ("tenant_id", "tenantid"):
        return "tenant_id"
    if lk in (
        "user_jwt",
        "userjwt",
        "jwt",
        "authorization",
        "access_token",
        "accesstoken",
        "id_token",
        "idtoken",
        "supabase_jwt",
        "supabasejwt",
    ):
        return "user_jwt"
    if lk in ("user_uid", "useruid", "uid"):
        return "user_uid"
    if lk in ("user_email", "useremail", "email"):
        return "user_email"
    return None


def _gather_chat_identity_fields(
    *,
    input_data: Mapping[str, Any] | dict[str, Any] | None,
    row: Mapping[str, Any] | dict[str, Any] | None,
    extras: Mapping[str, Any] | dict[str, Any] | None,
) -> dict[str, str]:
    """Collect tenant_id / user_jwt / user_uid / user_email from row, extras, InputData, metadata."""
    idata = dict(input_data or {})
    ex = dict(extras or {})
    if isinstance(ex.get("input_data"), dict):
        # SDK/HTTP 클라이언트가 본문을 extras.input_data 로만 넣는 경우
        idata = {**dict(ex["input_data"]), **idata}
    blobs: list[dict[str, Any]] = [idata, ex, dict(row or {})]
    for side in (idata.get("metadata"), ex.get("metadata")):
        if isinstance(side, dict):
            blobs.append(side)
    out: dict[str, str] = {}
    for blob in blobs:
        for raw_k, raw_v in blob.items():
            nk = _normalize_identity_field_key(str(raw_k))
            if nk is None:
                continue
            s = _identity_str(raw_v)
            if not s:
                continue
            out.setdefault(nk, s)
    return out


def _http_chat_tool_schema_weight(t: Any) -> int:
    """Rough size proxy for a tool's contribution to the ChatCompletions payload."""
    name = str(getattr(t, "name", "") or "")
    desc = str(getattr(t, "description", "") or "")[:8000]
    try:
        if hasattr(t, "get_input_jsonschema"):
            sch = t.get_input_jsonschema()
            js = json.dumps(sch, ensure_ascii=False, separators=(",", ":"))
        else:
            js = ""
    except Exception:
        js = ""
    return len(name) + len(desc) + len(js)


def select_http_chat_root_work_assistant_tools(all_tools_list: list[Any] | None) -> list[Any]:
    """Pick a bounded subset of work-assistant tools (smallest schemas first) for HTTP chat root."""
    wa = [t for t in (all_tools_list or []) if _is_work_assistant_mcp_tool_obj(t)]
    if not wa:
        logger.info("http_chat root work-assistant tools: none available from tool loader")
        return []
    wa_sorted = sorted(wa, key=_http_chat_tool_schema_weight)
    selected: list[Any] = []
    total_w = 0
    for t in wa_sorted:
        w = _http_chat_tool_schema_weight(t)
        if len(selected) >= _HTTP_CHAT_MAX_WORK_ASSISTANT_ROOT_TOOLS:
            break
        if selected and total_w + w > _HTTP_CHAT_WORK_ASSISTANT_SCHEMA_WEIGHT_BUDGET:
            break
        selected.append(t)
        total_w += w
    if not selected:
        t0 = wa_sorted[0]
        selected = [t0]
        total_w = _http_chat_tool_schema_weight(t0)
    logger.info(
        "http_chat root work-assistant tools: selected=%d/%d schema_weight=%d budget=%d cap=%d names=%s",
        len(selected),
        len(wa),
        total_w,
        _HTTP_CHAT_WORK_ASSISTANT_SCHEMA_WEIGHT_BUDGET,
        _HTTP_CHAT_MAX_WORK_ASSISTANT_ROOT_TOOLS,
        [getattr(x, "name", type(x).__name__) for x in selected],
    )
    return selected


def select_http_chat_root_work_assistant_tools_allowlist(
    all_tools_list: list[Any] | None,
    *,
    allowlist: list[str] | None,
) -> list[Any]:
    names = [str(x).strip() for x in (allowlist or []) if str(x).strip()]
    if not names:
        return []
    wa = [t for t in (all_tools_list or []) if _is_work_assistant_mcp_tool_obj(t)]
    if not wa:
        return []
    by_name: dict[str, Any] = {}
    for t in wa:
        nm = str(getattr(t, "name", "") or "").strip()
        if nm and nm not in by_name:
            by_name[nm] = t
    selected: list[Any] = []
    for nm in names:
        t = by_name.get(nm)
        if t is not None:
            selected.append(t)
    return selected


def merge_http_chat_identity_into_work_assistant_args(
    args: dict[str, Any],
    *,
    input_data: Mapping[str, Any] | dict[str, Any] | None,
    row: Mapping[str, Any] | dict[str, Any] | None,
    extras: Mapping[str, Any] | dict[str, Any] | None,
    tenant_id: str | None,
) -> dict[str, Any]:
    """Fill work-assistant MCP tool args from chat request surfaces when the model omits them.

    Note: LangSmith ``tool_calls`` on the LLM run often show the **raw model output** (nulls).
    This merge runs at ``invoke`` time via ``tool_input_augmenter``; the MCP call uses ``out``.
    """
    out = dict(args)
    fields = _gather_chat_identity_fields(input_data=input_data, row=row, extras=extras)
    r = dict(row or {})
    ex = dict(extras or {})
    tid = (
        str(tenant_id or "").strip()
        or fields.get("tenant_id", "")
        or str(r.get("tenant_id") or "").strip()
    )
    jwt = fields.get("user_jwt", "") or str(ex.get("user_jwt") or "").strip()
    uid = (
        fields.get("user_uid", "")
        or str(r.get("user_id") or "").strip()
        or str(r.get("user_uid") or "").strip()
        or str(ex.get("user_uid") or "").strip()
    )
    eml = fields.get("user_email", "")
    if not eml:
        ne = ex.get("notify_user_emails")
        if isinstance(ne, list) and ne:
            eml = str(ne[0] or "").strip()

    def _set(key: str, val: str) -> None:
        if not val:
            return
        cur = out.get(key)
        if cur is None or (isinstance(cur, str) and not cur.strip()):
            out[key] = val

    _set("tenant_id", tid)
    _set("user_jwt", jwt)
    _set("user_uid", uid)
    _set("user_email", eml)
    # get_current_user 등 일부 도구는 email 파라미터를 사용한다.
    _set("email", eml)
    return out


_TOOLCALL_ENVELOPE_KEYS = frozenset(
    {"type", "id", "name", "tool_call_id", "index", "artifact"}
)


def make_http_chat_work_assistant_tool_input_augmenter(
    *,
    input_data: dict[str, Any],
    row: dict[str, Any],
    extras: dict[str, Any],
    tenant_id: str | None,
    chat_identity_tool_ids: frozenset[int] | None = None,
) -> Any:
    """Return augmenter for _instrument_tools: merge chat identity into work-assistant tool calls.

    ``chat_identity_tool_ids`` (object ids of the exact root tools mounted for this chat run) ensures
    we merge even when ``_mcp_server`` uses a tenant-specific MCP key (e.g. ``process-gpt-mcp``)
    that would not match ``_is_work_assistant_mcp_tool_obj``.
    """

    def _aug(base_tool: Any, tool_input: Any) -> Any:
        in_chat_root = chat_identity_tool_ids is not None and id(base_tool) in chat_identity_tool_ids
        if not in_chat_root and not _is_work_assistant_mcp_tool_obj(base_tool):
            return tool_input
        merged = _coerce_tool_input_dict(tool_input)
        if not isinstance(merged, dict):
            return tool_input
        # Some stacks pass OpenAI ``args`` as a JSON string instead of a dict.
        _raw_args = merged.get("args")
        if isinstance(_raw_args, str) and _raw_args.strip():
            try:
                _parsed_args = json.loads(_raw_args)
                if isinstance(_parsed_args, dict):
                    merged = dict(merged)
                    merged["args"] = _parsed_args
            except Exception:
                pass
        # LangGraph / OpenAI-style tool_call: {"type":"tool_call","args":{...},...}
        # LangChain ToolNode often passes **flat** schema args: {"tenant_id": None, ...}
        # If we only write merged["args"], flat inputs never get tenant_id/jwt (real bug).
        if isinstance(merged.get("args"), dict):
            raw_args = dict(merged["args"])
            use_nested_args = True
        else:
            raw_args = {k: v for k, v in merged.items() if k not in _TOOLCALL_ENVELOPE_KEYS}
            use_nested_args = False
        new_args = merge_http_chat_identity_into_work_assistant_args(
            raw_args,
            input_data=input_data,
            row=row,
            extras=extras,
            tenant_id=tenant_id,
        )
        out = dict(merged)
        if use_nested_args:
            out["args"] = new_args
        else:
            out.update(new_args)
        return out

    return _aug


def _http_chat_tool_calls_summary(subagent_tool_log: dict[str, list[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for agent, tools in (subagent_tool_log or {}).items():
        out.append({"agent": agent, "tools": list(tools or [])})
    return out


def _http_chat_skill_calls_summary(
    subagent_skill_log: dict[str, list[str]],
    *,
    orchestrator_job_id: str | None = None,
) -> list[dict[str, Any]]:
    """HTTP /chat 응답용 스킬 사용 요약.

    Shape matches tool_calls: [{"agent": "...", "skills": ["a", "b"]}, ...]
    """
    out: list[dict[str, Any]] = []
    if not isinstance(subagent_skill_log, dict):
        return out
    for agent_name, skills in subagent_skill_log.items():
        if not agent_name:
            continue
        if not isinstance(skills, list):
            continue
        uniq = [str(x) for x in skills if str(x)]
        if not uniq:
            continue
        label = str(agent_name)
        if orchestrator_job_id and label == str(orchestrator_job_id):
            label = "작업 실행 기획자"
        out.append({"agent": label, "skills": uniq})
    return out


def _http_chat_merge_tool_calls_summary(
    orchestrator_tool_log: dict[str, list[str]],
    subagent_tool_log: dict[str, list[str]],
    *,
    orchestrator_job_id: str,
) -> list[dict[str, Any]]:
    """루트 오케스트레이터(MCP 등) + 서브에이전트 도구 로그를 HTTP 응답용 한 줄 요약으로 합친다.

    루트와 HITL 도구는 동일한 job_uuid 버킷에 쌓일 수 있어 orchestrator_job_id 기준으로 병합한다.
    """
    root_tools: list[str] = []
    for k, tl in (orchestrator_tool_log or {}).items():
        if str(k) != str(orchestrator_job_id):
            continue
        if not isinstance(tl, list):
            continue
        root_tools.extend(str(x) for x in tl if str(x))

    rows: list[dict[str, Any]] = []
    for agent, tls in (subagent_tool_log or {}).items():
        if str(agent) == str(orchestrator_job_id):
            if isinstance(tls, list):
                root_tools.extend(str(x) for x in tls if str(x))
            continue
        if isinstance(tls, list) and tls:
            rows.append({"agent": str(agent), "tools": list(tls)})

    out: list[dict[str, Any]] = []
    if root_tools:
        out.append({"agent": "작업 실행 기획자", "tools": list(dict.fromkeys(root_tools))})
    out.extend(rows)
    return out


def _last_assistant_message(messages: list[Any]) -> Any | None:
    for m in reversed(messages or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role is not None and str(role) == "assistant":
            return m
    return None


def _maybe_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        s = s.replace(",", "")
        return int(s)
    except Exception:
        return None


def _maybe_date_yyyy_mm_dd(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    return None


def _extract_close_facts_from_dict(blob: dict[str, Any]) -> dict[str, Any] | None:
    """Extract (symbol, name, date, close, currency, source) from dict-like outputs."""
    if not isinstance(blob, dict):
        return None

    symbol = blob.get("symbol") or blob.get("ticker") or blob.get("code") or blob.get("itemCode")
    date = blob.get("date")
    close = blob.get("close")
    currency = blob.get("currency") or "KRW"
    source = blob.get("source")
    name = blob.get("name") or blob.get("stockName")

    date2 = _maybe_date_yyyy_mm_dd(date)
    close2 = _maybe_int(close)

    # korea_econ_api stdout shape
    if (date2 is None or close2 is None) and isinstance(blob.get("data"), dict):
        data = blob.get("data") if isinstance(blob.get("data"), dict) else {}
        if not name:
            name = data.get("stockName")
        dti = data.get("dealTrendInfos")
        if isinstance(dti, list) and dti:
            first = dti[0] if isinstance(dti[0], dict) else {}
            if date2 is None:
                date2 = _maybe_date_yyyy_mm_dd(first.get("bizdate"))
            if close2 is None:
                close2 = _maybe_int(first.get("closePrice"))
            if not symbol:
                symbol = first.get("itemCode") or symbol
        if not source and isinstance(blob.get("source"), str):
            source = blob.get("source")

    if date2 is None or close2 is None:
        return None

    return {
        "symbol": str(symbol).strip() if symbol is not None else "",
        "name": str(name).strip() if isinstance(name, str) else "",
        "date": date2,
        "close": close2,
        "currency": str(currency).strip() if currency else "KRW",
        "source": str(source).strip() if isinstance(source, str) else "",
    }


def _render_close_sentence_ko(*, facts: dict[str, Any], user_text: str) -> str:
    date = str(facts.get("date") or "").strip()
    close_i = _maybe_int(facts.get("close"))
    symbol = str(facts.get("symbol") or "").strip()
    name = str(facts.get("name") or "").strip()
    if not date or close_i is None:
        return ""
    prefix = f"어제({date})" if ("어제" in (user_text or "")) else date
    subject = "삼성전자" if (not name and symbol == "005930") else (name or "해당 종목")
    suffix = f"({symbol})" if symbol else ""
    return f"{prefix} {subject}{suffix} 종가는 {close_i:,}원입니다."


def _tool_calls_from_assistant_message(msg: Any) -> list[dict[str, Any]]:
    """LangChain AIMessage.tool_calls / dict 메시지를 평범한 dict 리스트로 정규화."""
    if msg is None:
        return []
    raw: Any = None
    if isinstance(msg, dict):
        raw = msg.get("tool_calls")
    else:
        raw = getattr(msg, "tool_calls", None)
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            name = str(tc.get("name") or "").strip()
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            tid = str(tc.get("id") or "").strip()
            if name:
                out.append({"name": name, "args": dict(args), "id": tid})
            continue
        try:
            name = str(getattr(tc, "name", "") or "").strip()
            args = getattr(tc, "args", None)
            if not isinstance(args, dict):
                args = {}
            tid = str(getattr(tc, "id", "") or "").strip()
            if name:
                out.append({"name": name, "args": args, "id": tid})
        except Exception:
            continue
    return out


def _assistant_messages_with_tool_calls_in_order(messages: list[Any]) -> list[Any]:
    """tool_calls가 비어 있지 않은 assistant 메시지를 대화 순서대로 반환.

    도구 실행 후 마지막 assistant가 최종 답변 텍스트만 가진 경우가 많아,
    ``_last_assistant_message``만 쓰면 ``plan_tools``를 놓친다.
    """
    out: list[Any] = []
    for m in messages or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role is None or str(role) != "assistant":
            continue
        if _tool_calls_from_assistant_message(m):
            out.append(m)
    return out


def _display_tool_name_for_plan(internal_name: str, tools_by_name: dict[str, Any] | None) -> str:
    if not internal_name:
        return internal_name
    if not tools_by_name:
        return internal_name
    t = tools_by_name.get(internal_name)
    if t is None:
        return internal_name
    return http_chat_tool_display_name(t, internal_name)


def _plan_skills_from_tool_entries(entries: list[dict[str, Any]]) -> list[str]:
    skills: list[str] = []
    for e in entries:
        name = str(e.get("name") or "")
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        if name == "read_skill_document":
            sn = args.get("skill_name")
            if isinstance(sn, str) and sn.strip():
                skills.append(sn.strip())
        elif name == "sandbox_copy_mounted_skill":
            one = args.get("skill_name")
            if isinstance(one, str) and one.strip():
                skills.append(one.strip())
            many = args.get("skill_names_csv")
            if isinstance(many, str) and many.strip():
                skills.extend([s.strip() for s in many.split(",") if s.strip()])
            many2 = args.get("skill_names")
            if isinstance(many2, list):
                skills.extend([str(s).strip() for s in many2 if str(s).strip()])
    return list(dict.fromkeys(skills))


def _plan_signature_for_tool_calls(tcs: list[dict[str, Any]]) -> str:
    keys = []
    for tc in tcs:
        tid = (tc.get("id") or "").strip()
        keys.append(tid or f"n:{tc.get('name')}|a:{json.dumps(tc.get('args') or {}, sort_keys=True, default=str)}")
    return "|".join(keys)


def _emit_http_plan_event(http_stream_event_emit: Any, payload: dict[str, Any]) -> None:
    _emit_http_stream_event(http_stream_event_emit, payload)


def _emit_http_stream_event(http_stream_event_emit: Any, payload: dict[str, Any]) -> None:
    """Emit a structured event to the chat SSE stream.

    In legacy HTTP chat, the emitter was sync. In SDK chat mode we bind an **async**
    emitter (awaits `streamer.send_json`). Many call-sites are synchronous guards/hooks,
    so we must schedule the coroutine onto the running loop.
    """
    if http_stream_event_emit is None:
        return
    try:
        ret = http_stream_event_emit(payload)
        if asyncio.iscoroutine(ret):
            try:
                asyncio.get_running_loop().create_task(ret)
            except RuntimeError:
                # No running loop (should be rare inside execute); drop silently to avoid crashing tool calls.
                logger.warning(
                    "Dropping http_chat stream event: no running loop | type=%s",
                    payload.get("type"),
                )
    except Exception:
        logger.exception(
            "http_chat stream event emit failed | type=%s",
            payload.get("type"),
            exc_info=True,
        )


def _planned_skills_from_tool_call(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Heuristic: extract referenced skill names from a tool call."""
    tn = (tool_name or "").strip()
    if not tn or not isinstance(args, dict):
        return []
    skills: list[str] = []
    if tn == "read_skill_document":
        sn = args.get("skill_name")
        if isinstance(sn, str) and sn.strip():
            skills.append(sn.strip())
    if tn == "skill_volume_probe":
        many = args.get("skill_names_csv")
        if isinstance(many, str) and many.strip():
            skills.extend([s.strip() for s in many.split(",") if s.strip()])
        many2 = args.get("skill_names")
        if isinstance(many2, list):
            skills.extend([str(s).strip() for s in many2 if str(s).strip()])
    if tn == "sandbox_copy_mounted_skill":
        one = args.get("skill_name")
        if isinstance(one, str) and one.strip():
            skills.append(one.strip())
        many = args.get("skill_names_csv")
        if isinstance(many, str) and many.strip():
            skills.extend([s.strip() for s in many.split(",") if s.strip()])
        many2 = args.get("skill_names")
        if isinstance(many2, list):
            skills.extend([str(s).strip() for s in many2 if str(s).strip()])
    return list(dict.fromkeys(skills))


def _coerce_langgraph_stream_chunk(part: Any) -> list[tuple[str, Any]]:
    """LangGraph astream 한 덩어리를 (mode, payload) 목록으로 통일.

    - ``version=\"v2\"``: ``{\"type\": \"values\"|\"messages\", \"data\": ...}``
    - 구버전/멀티 모드: ``(\"values\", state)``, ``(\"messages\", (chunk, meta))``
    - 서브그래프: ``(namespace, \"values\", state)`` 등 3-튜플
    """
    if isinstance(part, dict):
        t = part.get("type")
        if t in ("values", "messages"):
            data = part.get("data")
            # LangGraph ``version=\"v2\"`` 스트림: ``interrupt()`` 페이로드가 ``data`` 안의
            # ``__interrupt__``가 아니라 루트의 ``interrupts`` 튜플로만 내려온다.
            # 여기서 떨어뜨리면 최종 state에 interrupt가 없어 HTTP 채팅이 completed/done으로
            # 끝나고 OpenUI HITL SSE가 발행되지 않는다.
            if t == "values" and isinstance(data, dict):
                ir = part.get("interrupts")
                if ir:
                    data = dict(data)
                    if isinstance(ir, (list, tuple)):
                        data["__interrupt__"] = tuple(ir)
                    else:
                        data["__interrupt__"] = (ir,)
            return [(t, data)]
        return []
    if isinstance(part, tuple):
        if len(part) == 2 and isinstance(part[0], str) and part[0] in ("values", "messages"):
            return [(part[0], part[1])]
        if len(part) == 3 and isinstance(part[1], str) and part[1] in ("values", "messages"):
            return [(part[1], part[2])]
    return []


def _llm_stream_text_piece(msg: Any) -> str:
    """LangGraph ``messages`` 스트림 청크에서 사용자에게 보여줄 텍스트 델타만 추출."""
    if msg is None:
        return ""
    try:
        from langchain_core.messages import AIMessageChunk

        if isinstance(msg, AIMessageChunk):
            c = getattr(msg, "content", None)
            return _extract_plain_text(c) if c not in (None, "") else ""
    except ImportError:
        pass
    role = getattr(msg, "type", None) or getattr(msg, "role", None)
    if role is not None and str(role).lower() not in ("ai", "assistant"):
        return ""
    c = getattr(msg, "content", None)
    return _extract_plain_text(c) if c not in (None, "") else ""


async def _iter_http_chat_astream_parts(
    orchestrator: Any,
    state_input: Any,
    *,
    run_config: dict[str, Any] | None = None,
) -> Any:
    """LangGraph astream을 ``values`` + ``messages``(토큰)로 통합하거나, 불가 시 ``values``만.

    Yields: ``{"kind": "values"|"messages", "data": ..., "dual": bool}``

    주의: 멀티 모드에서 구버전 LangGraph는 dict가 아니라 ``(\"values\", state)`` 튜플을
    돌려준다. 이를 무시하면 스트림이 비어 ``ainvoke`` 폴백만 타게 되어 plan_*/토큰이 전혀 안 나간다.
    """
    attempts: list[dict[str, Any]] = [
        {"stream_mode": ["values", "messages"], "version": "v2"},
        {"stream_mode": ["values", "messages"]},
    ]
    stream_kw: dict[str, Any] = {}
    if run_config is not None:
        stream_kw["config"] = run_config

    for attempt in attempts:
        received = 0
        yielded = False
        first_bad: Any | None = None
        try:
            async for part in orchestrator.astream(state_input, **attempt, **stream_kw):
                received += 1
                pairs = _coerce_langgraph_stream_chunk(part)
                if not pairs:
                    if first_bad is None:
                        first_bad = part
                    continue
                for mode, data in pairs:
                    if mode == "values":
                        yield {"kind": "values", "data": data, "dual": True}
                        yielded = True
                    elif mode == "messages":
                        yield {"kind": "messages", "data": data, "dual": True}
                        yielded = True
            if yielded:
                return
            if received > 0:
                logger.warning(
                    "HTTP chat: astream %s got %d chunks but none parsed as values/messages "
                    "(first chunk type=%s repr=%r); using values-only fallback",
                    attempt,
                    received,
                    type(first_bad).__name__,
                    repr(first_bad)[:500],
                )
                break
        except TypeError:
            continue
        except Exception as e:
            logger.info("HTTP chat: astream attempt %s failed: %s", attempt, e)
            continue

    async for state in orchestrator.astream(state_input, stream_mode="values", **stream_kw):
        yield {"kind": "values", "data": state, "dual": False}


async def _ainvoke_or_stream_values(
    orchestrator: Any,
    state_input: Any,
    *,
    stream_emit: Any | None,
    http_stream_event_emit: Any | None = None,
    plan_tools_by_name: dict[str, Any] | None = None,
    run_config: dict[str, Any] | None = None,
) -> Any:
    """Run graph; HTTP 스트림은 가능하면 LLM 토큰(messages) + 상태(values), 아니면 values만.

    - ``messages`` 모드: 모델이 스트리밍을 지원하면 답변이 작은 청크로 ``stream_emit``에 전달된다.
    - ``values`` 모드: ``plan_*`` 이벤트 및(폴백 시) 노드 단위 텍스트 델타.
    """
    invoke_kw: dict[str, Any] = {}
    if run_config is not None:
        invoke_kw["config"] = run_config

    if stream_emit is None:
        return await orchestrator.ainvoke(state_input, **invoke_kw)
    prev_stream_len = 0
    msg_cumulative = ""
    msg_streamable_emitted_len = 0
    streamed_cumulative = ""
    final_state: Any = None
    any_dual_stream = False
    emitted_tool_plan_sigs: set[str] = set()
    last_todos_sig: str | None = None

    async for item in _iter_http_chat_astream_parts(
        orchestrator, state_input, run_config=run_config
    ):
        if item.get("dual"):
            any_dual_stream = True

        if item["kind"] == "messages":
            raw = item.get("data")
            msg_obj: Any = None
            if isinstance(raw, tuple) and len(raw) >= 1:
                msg_obj = raw[0]
            elif raw is not None:
                msg_obj = raw
            if msg_obj is None:
                continue
            piece = _llm_stream_text_piece(msg_obj)
            if not piece:
                continue
            msg_cumulative += piece
            cut = cut_index_after_leading_workitem_echo_for_stream(msg_cumulative)
            streamable = msg_cumulative[cut:]
            if len(streamable) > msg_streamable_emitted_len:
                delta = streamable[msg_streamable_emitted_len:]
                if delta:
                    await stream_emit(delta)
                    streamed_cumulative += delta
                msg_streamable_emitted_len = len(streamable)
            continue

        state = item.get("data")
        final_state = state
        if not isinstance(state, dict):
            continue

        if http_stream_event_emit is not None and "todos" in state:
            try:
                sig = json.dumps(_json_safe(state.get("todos")), ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                sig = str(id(state.get("todos")))
            if sig != last_todos_sig:
                last_todos_sig = sig
                _emit_http_plan_event(
                    http_stream_event_emit,
                    {
                        "type": "plan_todos",
                        "todos": _json_safe(state.get("todos")),
                    },
                )

        msgs = state.get("messages") or []
        las = _last_assistant_message(msgs)
        # NOTE: plan_tools / plan_skills are emitted from the tool_start guard/hook.
        # Emitting them again from streamed assistant tool_calls duplicates SSE events.

        if item.get("dual"):
            continue

        if not msgs or las is None:
            continue
        content = las.get("content") if isinstance(las, dict) else getattr(las, "content", None)
        txt = _extract_plain_text(content)
        cut = cut_index_after_leading_workitem_echo_for_stream(txt)
        streamable = txt[cut:]
        if len(streamable) < prev_stream_len:
            prev_stream_len = 0
        if len(streamable) > prev_stream_len:
            delta = streamable[prev_stream_len:]
            if delta:
                await stream_emit(delta)
                streamed_cumulative += delta
            prev_stream_len = len(streamable)

    if any_dual_stream and stream_emit is not None and isinstance(final_state, dict):
        msgs = final_state.get("messages") or []
        las = _last_assistant_message(msgs)
        if las is not None:
            content = las.get("content") if isinstance(las, dict) else getattr(las, "content", None)
            full_txt = _extract_plain_text(content)
            cut = cut_index_after_leading_workitem_echo_for_stream(full_txt)
            streamable = full_txt[cut:]
            if len(streamable) > msg_streamable_emitted_len:
                tail = streamable[msg_streamable_emitted_len:]
                if tail:
                    await stream_emit(tail)
                    streamed_cumulative += tail
                msg_streamable_emitted_len = len(streamable)
    # Ensure the final state carries the streamed assistant text.
    # Some backends end the state with tool/function messages or empty assistant tool-call messages,
    # which makes final response extraction unreliable if we only look at state["messages"][-1].
    if stream_emit is not None and isinstance(final_state, dict) and streamed_cumulative:
        final_state["_http_chat_streamed_text"] = streamed_cumulative

    if final_state is None:
        return await orchestrator.ainvoke(state_input, **invoke_kw)
    return final_state


def _subagent_tool_job_id_getter(default_jid: str):
    """Prefer active task scope (ContextVar), else stable per-subagent job id."""

    def _inner() -> str:
        active = skill_volume_active_job_id.get()
        return active if active else default_jid

    return _inner


def _build_fallback_activity_subagent(
    *,
    tenant_id: str,
    tool_user_id: str,
    all_tools: list[Any],
    activity_tools: list[str],
    activity_skills: list[str],
) -> dict[str, Any] | None:
    """Create a single subagent config from activity-specified tools/skills.

    - Tool entries can be either tool names or MCP server keys.
    - Token hygiene:
      - If activity defines `tools`, build a **slim** toolset using only those MCP/tool entries.
      - If activity does NOT define `tools` (skills-only), attach sandbox + skill-resolution tools.
    """
    if not activity_tools and not activity_skills:
        return None

    wanted_names = [str(x).strip() for x in (activity_tools or []) if str(x).strip()]
    want_slim = bool(wanted_names)

    merged_tools = list(all_tools)
    if not want_slim:
        # Skills-only fallback still needs local execution + skill resolution primitives.
        merged_tools = merged_tools + list(get_sandbox_tools()) + list(get_skill_resolution_tools())
    tools_by_name: dict[str, Any] = {str(getattr(t, "name", type(t).__name__)): t for t in merged_tools}

    wanted_norm = {w.lower().replace("_", "-") for w in wanted_names}

    picked: list[Any] = []
    seen_ids: set[int] = set()

    def _add_tool_obj(t: Any) -> None:
        if t is None:
            return
        if id(t) in seen_ids:
            return
        picked.append(t)
        seen_ids.add(id(t))

    # 1) Exact tool name matches.
    for n in wanted_names:
        t = tools_by_name.get(n)
        if t is not None:
            _add_tool_obj(t)

    # 2) MCP-server matches (treat each wanted name as possible server key).
    for t in merged_tools:
        srv = str(getattr(t, "_mcp_server", "") or "").strip()
        if not srv:
            continue
        norm = srv.lower().replace("_", "-")
        if norm in wanted_norm or any(norm.endswith(w) for w in wanted_norm):
            _add_tool_obj(t)

    if not want_slim:
        # Best-effort include tenant context tools already loaded (if present).
        base = tools_by_name.get("memento")
        if base is not None:
            _add_tool_obj(base)

        # Always include sandbox tools & skill resolution tools.
        for t in list(get_sandbox_tools()) + list(get_skill_resolution_tools()):
            _add_tool_obj(t)

    name = "업무 실행자"
    sys_lines = [
        "=== 역할 ===",
        "- 당신은 프로세스 액티비티에 정의된 도구/스킬을 사용해 업무를 수행하는 실행 에이전트입니다.",
        "- 결과는 간결하고 검증 가능한 형태(파일/JSON/근거)로 반환하세요.",
    ]
    if wanted_names:
        sys_lines.append(f"- 액티비티 지정 도구: {', '.join(wanted_names)}")
    if activity_skills:
        sys_lines.append(f"- 액티비티 지정 스킬: {', '.join(activity_skills)}")
    sys_lines.extend(
        [
            "",
            "=== 우선순위 ===",
            "- 1) task() 위임(description)의 지시/출력 형식을 최우선으로 따른다.",
            "- 2) 스킬은 추정하지 말고, 필요한 경우 도구로 문서를 조회/로드한 뒤 사용한다.",
            "- 3) 실행이 가능하면 실행(샌드박스)하고, 불가하면 근거와 다음 시도안을 보고한다.",
        ]
    )
    system_prompt = "\n".join(sys_lines)

    caps_tools = list(dict.fromkeys(wanted_names))
    caps_skills = list(dict.fromkeys([str(x).strip() for x in (activity_skills or []) if str(x).strip()]))

    return {
        "name": name,
        "description": f"Process activity fallback executor (tenant={tenant_id or '(unknown)'}, user={tool_user_id or '(unknown)'})",
        "system_prompt": system_prompt,
        "tools": picked,
        "capabilities": {
            "skills": caps_skills,
            "tools": caps_tools,
            "agent_type": "activity_fallback",
            "agent_id": tool_user_id or "activity_fallback",
        },
    }


def _select_tools_for_activity_tools(
    *,
    all_tools: list[Any],
    activity_tools: list[str],
) -> list[Any]:
    """Select only tools matching activity-declared tool/server keys (token hygiene)."""
    wanted_names = [str(x).strip() for x in (activity_tools or []) if str(x).strip()]
    if not wanted_names:
        return []
    wanted_norm = {w.lower().replace("_", "-") for w in wanted_names}
    tools_by_name: dict[str, Any] = {str(getattr(t, "name", type(t).__name__)): t for t in (all_tools or [])}

    picked: list[Any] = []
    seen_ids: set[int] = set()

    def _add(t: Any) -> None:
        if t is None:
            return
        if id(t) in seen_ids:
            return
        picked.append(t)
        seen_ids.add(id(t))

    # 1) Exact tool name matches.
    for n in wanted_names:
        _add(tools_by_name.get(n))

    # 2) MCP-server matches (treat each wanted name as possible server key).
    for t in all_tools or []:
        srv = str(getattr(t, "_mcp_server", "") or "").strip()
        if not srv:
            continue
        norm = srv.lower().replace("_", "-")
        if norm in wanted_norm or any(norm.endswith(w) for w in wanted_norm):
            _add(t)

    return picked


class DeepAgentsExecutor(AgentExecutor):
    """Runs todolist query via create_deep_agent(subagents); emits A2A Task*Events."""

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        tok = bind_executor_asyncio_loop_for_enqueue(asyncio.get_running_loop())
        logger.info("DeepAgents 멀티 에이전트 실행 시작")
        try:
            await prefetch_github_skills()
        except Exception:
            logger.exception("github-skills: failed to prefetch skills; continuing without GitHub skills")

        query = context.get_user_input()
        context_data = context.get_context_data() or {}
        row = context_data.get("row", {})
        extras = context_data.get("extras", {})
        # Chat: SDK sets request_kind=chat (streamer in extras). One flag drives legacy HTTP parity.
        is_chat = bool(_is_chat_request(context))
        checkpoint_thread_id = resolve_checkpoint_thread_id(is_chat=is_chat, row=row)

        is_adhoc = False
        if isinstance(row, dict):
            adhoc_val = row.get("adhoc")
            if adhoc_val is True or (isinstance(adhoc_val, str) and adhoc_val.lower() == "true"):
                is_adhoc = True

        if is_adhoc:
            proc_inst_id = row.get("proc_inst_id") or ""
        else:
            proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id") or ""
        task_id = str(row.get("id") or "")
        tenant_id = row.get("tenant_id") or ""
        form_id = extras.get("form_id")
        _raw_sources = extras.get("sources")
        if isinstance(_raw_sources, dict):
            proc_inst_sources: list[Any] = [_raw_sources]
        elif isinstance(_raw_sources, list):
            proc_inst_sources = _raw_sources
        else:
            proc_inst_sources = []

        logger.info("task_id=%s proc_inst_id=%s form_id=%s", task_id, proc_inst_id, form_id)
        sandbox_output_paths_begin_task(task_id)

        # Chat (SSE): streaming callbacks + optional chat room persistence.
        if is_chat and isinstance(extras, dict):
            chat_room_id = _resolve_chat_room_id(extras, row if isinstance(row, dict) else {})
            has_file = bool(proc_inst_sources)

            # Ensure chat room exists (best-effort) so context updates have a target row.
            if chat_room_id and tenant_id:
                try:
                    await a_upsert_chat_room_start(
                        tenant_id=str(tenant_id),
                        room_id=str(chat_room_id),
                        participants=[],
                        room_name=None,
                        primary_agent_id=None,
                        context=build_initial_chat_room_context(has_file=has_file),
                    )
                except Exception:
                    logger.exception(
                        "chat: upsert_chat_room_start failed; continuing | room_id=%s",
                        chat_room_id,
                        exc_info=True,
                    )

            async def _sdk_stream_emit(text: str) -> None:
                try:
                    await _emit_chunk_text(context, text)
                except Exception:
                    logger.exception("chat: chunk emit failed", exc_info=True)

            async def _sdk_stream_event_emit(payload: dict[str, Any]) -> None:
                try:
                    await _emit_chunk_json(context, payload)
                    # Persist tool/todo/skill timeline into chat_rooms.context for the UI.
                    if chat_room_id and tenant_id and isinstance(payload, dict):
                        et = str(payload.get("type") or "").strip()
                        if et in ("plan_tools", "plan_skills", "plan_todos", "tool_start", "tool_end"):
                            try:
                                await a_update_chat_room_context(
                                    tenant_id=str(tenant_id),
                                    room_id=str(chat_room_id),
                                    evt=payload,
                                    has_file=has_file,
                                )
                            except Exception:
                                logger.exception(
                                    "chat: update_chat_room_context failed; continuing | room_id=%s type=%s",
                                    chat_room_id,
                                    et,
                                    exc_info=True,
                                )
                except Exception:
                    logger.exception("chat: json chunk emit failed", exc_info=True)

            extras["_http_chat_stream_emit"] = _sdk_stream_emit
            extras["_http_chat_stream_event_emit"] = _sdk_stream_event_emit

        # Prepare run_state / human_response before creating execution context.
        # SDK prepare_context() does not pass these in extras; restore from todolist.draft + events.
        draft_raw = row.get("draft")
        draft = draft_raw if isinstance(draft_raw, dict) else {}

        run_state: dict[str, Any] = {}
        if isinstance(extras.get("run_state"), dict):
            run_state = dict(extras["run_state"])
        elif isinstance(draft.get("run_state"), dict):
            run_state = dict(draft["run_state"])

        human_response_answer = extras.get("human_response_answer")
        if (
            (not isinstance(human_response_answer, str) or not human_response_answer.strip())
            and task_id
            and run_state
        ):
            try:
                hr = get_latest_human_response(str(task_id))
            except Exception:
                logger.exception("get_latest_human_response failed for todo_id=%s", task_id)
                hr = None
            if isinstance(hr, str) and hr.strip():
                human_response_answer = hr.strip()
                logger.info(
                    "HITL resume: loaded human_response from DB (todo_id=%s, len=%d)",
                    task_id,
                    len(human_response_answer),
                )

        checkpointer = await get_async_postgres_checkpointer()
        human_answer_stripped = (
            human_response_answer.strip()
            if isinstance(human_response_answer, str) and human_response_answer.strip()
            else ""
        )
        use_graph_resume = bool(
            checkpointer is not None
            and bool(checkpoint_thread_id)
            and bool(human_answer_stripped)
            and isinstance(run_state, dict)
            and str(run_state.get("tool_name") or "") == "request_human_input"
        )
        if use_graph_resume:
            logger.info(
                "HITL checkpoint resume | thread_id=%s is_chat=%s",
                checkpoint_thread_id,
                is_chat,
            )

        hitl_resume: dict[str, str] | None = None

        # Create or reuse execution context for this run.
        initial_agent_path = ["작업 실행 기획자"]
        if isinstance(run_state, dict) and isinstance(run_state.get("agent_path"), list):
            # Reuse stored agent_path when resuming.
            initial_agent_path = list(run_state.get("agent_path")) or initial_agent_path
        if exec_ctx is None:
            base_correlation = None
            if isinstance(run_state, dict) and isinstance(run_state.get("correlation_id"), str):
                base_correlation = run_state.get("correlation_id")
            job_uuid = base_correlation or str(uuid.uuid4())
            exec_ctx = ExecutionContext(
                correlation_id=job_uuid,
                agent_path=initial_agent_path,
                task_id=task_id or None,
                proc_inst_id=proc_inst_id or None,
            )
        job_uuid = exec_ctx.correlation_id

        if use_graph_resume:
            try:
                enqueue_status_update(
                    event_queue,
                    "task_working",
                    {
                        "resume_source": "checkpoint_graph_resume",
                        "human_answer": human_answer_stripped,
                        "checkpoint_thread_id": checkpoint_thread_id,
                        "previous_interrupt_agent_path": run_state.get("agent_path")
                        if isinstance(run_state, dict)
                        else None,
                        "previous_interrupt_tool_name": run_state.get("tool_name")
                        if isinstance(run_state, dict)
                        else None,
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="resume",
                    phase="resume",
                    agent_name=None,
                )
            except Exception:
                logger.exception("Failed to enqueue checkpoint resume event", exc_info=True)

        # Parse work-item query into structured parts for downstream logic / HITL.
        parsed_query = parse_workitem_query(query or "")
        description = parsed_query.get("description") or ""
        instruction = parsed_query.get("instruction") or ""
        input_data = parsed_query.get("input_data") or {}

        # Resume: if we have run_state and human_response, merge answer into input_data.
        # Graph-native interrupt(request_human_input) + AsyncPostgresSaver 는 Command(resume) 로 재개한다.
        if human_answer_stripped and not use_graph_resume:
            pending_field = run_state.get("pending_field") or "human_response"
            input_data = dict(input_data)
            input_data[pending_field] = human_answer_stripped
            parsed_query["input_data"] = input_data
            hitl_resume = {
                "field": str(pending_field),
                "answer": human_answer_stripped,
            }

            # Emit a resume event for tracing via SDK EventQueue.
            try:
                enqueue_status_update(
                    event_queue,
                    "task_working",
                    {
                        "resume_source": "human_response",
                        "human_answer": human_answer_stripped,
                        "previous_interrupt_agent_path": run_state.get("agent_path"),
                        "previous_interrupt_tool_name": run_state.get("tool_name"),
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="resume",
                    phase="resume",
                    agent_name=None,
                )
            except Exception:
                logger.exception("Failed to enqueue resume event", exc_info=True)

        # Chat: prefer Instruction from work-item template (HTTP parity); fall back to raw query for plain SDK text.
        if is_chat:
            invoke_user_text = str(instruction or "").strip() or (query or "").strip()
        else:
            invoke_user_text = (query or "").strip()
        if hitl_resume and hitl_resume.get("answer"):
            invoke_user_text = (
                f"{invoke_user_text}\n\n"
                f"[HITL follow-up — field `{hitl_resume.get('field', 'human_response')}`]:\n"
                f"{hitl_resume['answer']}\n\n"
                "Use this to continue the task immediately; do not ask for the same information again."
            )

        # Initialize processgpt_agent_utils context (for tools like mem0, memento, dmn_rule).
        set_context(
            task_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="deepagents",
            users_email=extras.get("notify_user_emails", []),
        )

        domain_agent_rows = extras.get("agents", []) or []
        all_tools: list[Any] = []
        activity_fallback_tools: list[str] = []
        # Prefer agents[].id as tool user_id when available.
        first_agent_user_id = ""
        if isinstance(domain_agent_rows, list):
            for ag in domain_agent_rows:
                if isinstance(ag, dict):
                    aid = str(ag.get("id") or "").strip()
                    if aid:
                        first_agent_user_id = aid
                        break
        raw_user_id = first_agent_user_id or extras.get("user_id") or extras.get("request_user_id") or row.get("user_id") or ""
        # todolist.user_id can be CSV; tools expect a single user id.
        tool_user_id = str(raw_user_id).split(",")[0].strip() if raw_user_id else ""
        http_stream_event_emit = extras.get("_http_chat_stream_event_emit")
        mcp_cfg_raw = extras.get("tenant_mcp")
        if not mcp_cfg_raw and tenant_id:
            try:
                from processgpt_agent_sdk.database import fetch_tenant_mcp
                mcp_cfg_raw = await fetch_tenant_mcp(tenant_id)
            except Exception as e:
                logger.warning("채팅 모드 tenant_mcp 조회 실패: %s", str(e))
        mcp_cfg: dict[str, Any] = _mcp_config_without_blocked_servers(mcp_cfg_raw)
        # Merge defaults: work-assistant for any chat entrypoint (HTTP or SDK SSE).
        try:
            mcp_cfg = dict(mcp_cfg or {})
            mcp_servers0 = mcp_cfg.get("mcpServers")
            if not isinstance(mcp_servers0, dict):
                mcp_servers0 = {}
            else:
                mcp_servers0 = dict(mcp_servers0)
            if is_chat and "work-assistant" not in mcp_servers0:
                # process-gpt-mcp는 Supabase로 사용자 JWT를 검증한다. HTTP 채팅 호스트와 동일 프로젝트의 URL/키가 필요하다.
                # GKE 등에서는 SUPABASE_KEY(서비스 롤)만 두고 SUPABASE_ANON_KEY를 비워 두는 경우가 많아, anon 미설정 시 KEY로 폴백한다.
                _sb_url = str(os.environ.get("SUPABASE_URL") or "").strip()
                _sb_anon = str(os.environ.get("SUPABASE_ANON_KEY") or "").strip()
                if not _sb_anon:
                    _sb_anon = str(
                        os.environ.get("SUPABASE_KEY")
                        or os.environ.get("SERVICE_ROLE_KEY")
                        or ""
                    ).strip()
                wa_env: dict[str, str] = {
                    "SUPABASE_URL": _sb_url,
                    "SUPABASE_ANON_KEY": _sb_anon,
                }
                _jwt_secret = str(os.environ.get("SUPABASE_JWT_SECRET") or "").strip()
                if _jwt_secret:
                    wa_env["SUPABASE_JWT_SECRET"] = _jwt_secret
                mcp_servers0["work-assistant"] = {
                    "command": "uvx",
                    "args": ["process-gpt-mcp"],
                    "env": wa_env,
                }
            mcp_cfg["mcpServers"] = mcp_servers0
        except Exception:
            logger.exception("Failed to inject default MCP servers (work-assistant); continuing", exc_info=True)
        mcp_servers = mcp_cfg.get("mcpServers") or {}

        # NOTE: Chat modes default to disabling tenant context preflight for TTFT/stability.
        enable_tenant_context_preflight = (not is_chat) or _truthy_env(
            _ENV_HTTP_CHAT_ENABLE_TENANT_CONTEXT_PREFLIGHT, default=False
        )
        # MCP servers to request.
        # Process mode intentionally does NOT auto-connect all tenant MCP servers.
        # We keep this minimal and let activity/subagent capabilities decide what to load.
        desired_mcp_servers = ("work-assistant",) if is_chat else ()

        resolved_server_keys: dict[str, list[str]] = {name: [] for name in desired_mcp_servers}
        if isinstance(mcp_servers, dict):
            for key in mcp_servers:
                raw = str(key).strip()
                if not raw:
                    continue
                normalized = raw.lower().replace("_", "-")
                for canonical in desired_mcp_servers:
                    if normalized == canonical or normalized.endswith(canonical):
                        resolved_server_keys[canonical].append(raw)

            # Ensure canonical keys exist for exact-match lookup.
            for canonical, found_keys in resolved_server_keys.items():
                if found_keys and canonical not in mcp_servers:
                    primary_key = found_keys[0]
                    cfg = mcp_servers.get(primary_key)
                    if isinstance(cfg, dict):
                        mcp_servers[canonical] = dict(cfg)

        # 로컬 컨텍스트 도구를 먼저 요청한 뒤, MCP를 이어서 요청한다.
        # memento는 채팅 모드 포함 항상 기본 도구로 로드한다(preflight 비활성화 여부와 무관).
        mcp_request: list[str] = []
        if enable_tenant_context_preflight or is_chat:
            mcp_request.append("memento")
        for canonical in desired_mcp_servers:
            for candidate in [*resolved_server_keys.get(canonical, []), canonical]:
                if candidate and candidate not in mcp_request:
                    mcp_request.append(candidate)
        extend_mcp_request_with_domain_agent_declared_servers(
            mcp_request,
            mcp_servers=mcp_servers,
            domain_agent_rows=domain_agent_rows,
        )
        logger.info(
            "테넌트 연결 도구 로드 준비 | task_id=%s tenant_id=%s mcp_servers=%s request=%s",
            task_id,
            tenant_id or "(empty)",
            list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
            mcp_request,
        )
        tool_loader: TaggingSafeToolLoader | None = None
        try:
            # tenant_mcp 병합 설정으로 MCP 도구 로드.
            # TaggingSafeToolLoader 가 각 MCP 툴에 _mcp_server 를 붙여 서브 에이전트 baseline 라우팅을 가능하게 함.
            tool_loader = TaggingSafeToolLoader(
                tenant_id=tenant_id or None,
                user_id=tool_user_id or None,
                agent_name="작업 실행 기획자",
                mcp_config=mcp_cfg,
                allowed_local_tools=frozenset({"memento"}),
            )
            all_tools = tool_loader.create_tools_from_names(tool_names=mcp_request, agent_type=None)
            # CrewAI/MCP 툴을 LangChain BaseTool로 래핑하여 tool() 데코레이터 __name__ 오류 방지
            all_tools = _ensure_langchain_tools(all_tools)
            _kn = []
            _other = []
            for _t in all_tools:
                _nm = getattr(_t, "name", type(_t).__name__)
                (_kn if _classify_tenant_context_tool(_t) else _other).append(_nm)
            logger.info(
                "연결 도구 로드 완료 | task_id=%s total_tools=%d "
                "tenant_knowledge_tools=%s (개인 기억·RAG·비즈니스 규칙) other_tools=%s (스킬·기타 MCP 등)",
                task_id,
                len(all_tools),
                _kn,
                _other,
            )
        except Exception as e:
            logger.warning(
                "MCP tools loading via TaggingSafeToolLoader failed (subagent config continues with reduced MCP tools): %s",
                e,
                exc_info=False,
            )
            logger.info("연결 도구 로드 실패 후 계속 진행 | task_id=%s total_tools=%d", task_id, len(all_tools))

        context_tool_tracker: dict[str, Any] = {"attempted_global": set()}

        # 서브 에이전트별 도구 호출은 각 서브 에이전트 전용 job_id로 분리된다.
        # 루트 오케스트레이터(chat 전용 work-assistant / worker 전용 process-gpt-mcp 등)는
        # 별도 로그에 쌓은 뒤 HTTP 응답에서 합친다.
        orchestrator_tool_log: dict[str, list[str]] = {}
        subagent_tool_log: dict[str, list[str]] = {}
        subagent_skill_log: dict[str, list[str]] = {}
        # Structured domain execution outputs captured from tool results (e.g. sandbox stdout JSON).
        # Used to backfill output forms when the orchestrator forgets to emit JSON.
        structured_domain_outputs_by_job: dict[str, dict[str, Any]] = {}
        # Upload tool outputs that produce deliverable public URLs (e.g. xlsx).
        # We merge these into `deliverable_files` so form fields like `draft_file` can be populated
        # even when the sandbox artifact scanner misses the output path.
        deliverable_files_from_tools: list[dict[str, Any]] = []

        allowed_skill_names: list[str] = []
        allowed_skill_names_by_agent: dict[str, list[str]] = {}
        seen_skill_names: set[str] = set()

        # 1. Collect skills from domain agents (subagents)
        for ag in domain_agent_rows:
            if isinstance(ag, dict):
                agent_name = (
                    ag.get("alias")
                    or ag.get("username")
                    or ag.get("name")
                    or str(ag.get("id") or "")
                )
            else:
                agent_name = ""
            per_agent = []
            for sk in parse_skills_csv(ag.get("skills") if isinstance(ag, dict) else None):
                n = sk.strip()
                if n and n not in seen_skill_names:
                    seen_skill_names.add(n)
                    allowed_skill_names.append(n)
                if n:
                    per_agent.append(n)
            if agent_name and per_agent:
                # Keep order stable and unique.
                allowed_skill_names_by_agent[agent_name] = list(dict.fromkeys(per_agent))

        # 2. Collect skills from the root agent (Deep Agent / orchestrator)
        root_agent_name = (
            row.get("alias")
            or row.get("username")
            or row.get("name")
            or str(row.get("id"))
        )
        per_root = []
        for sk in parse_skills_csv(row.get("skills") if isinstance(row, dict) else None):
            n = sk.strip()
            if n and n not in seen_skill_names:
                seen_skill_names.add(n)
                allowed_skill_names.append(n)
            if n:
                per_root.append(n)
        if per_root:
            allowed_skill_names_by_agent[root_agent_name] = list(dict.fromkeys(per_root))
        logger.info(
            "서브에이전트 구성 시작 | task_id=%s domain_agent_rows=%d allowed_skill_names=%d",
            task_id,
            len(domain_agent_rows),
            len(allowed_skill_names),
        )

        if is_adhoc:
            domain_agent_rows = load_adhoc_agent_rows(
                task_id=task_id or None,
                root_proc_inst_id=proc_inst_id or None,
                tenant_id=tenant_id or None,
            )
        subagents_domain = build_subagent_configs(
            domain_agent_rows,
            all_tools=all_tools,
            tenant_id=tenant_id or None,
            always_attach_local_tool_names=("memento",) if is_adhoc else None,
        )
        subagents = list(subagents_domain)
        agent_rows = list(domain_agent_rows)
        logger.info("서브에이전트 구성 결과 | task_id=%s built_subagents=%d", task_id, len(subagents))
        for idx, sa in enumerate(subagents):
            sa_name = sa.get("name")
            sa_tools = sa.get("tools") or []
            logger.info(
                "서브에이전트[%d] name=%s tools=%d tool_names=%s",
                idx,
                sa_name,
                len(sa_tools),
                [getattr(t, "name", type(t).__name__) for t in sa_tools],
            )
        if not subagents:
            # Extended spec: even if no explicit subagent is configured, the activity can define
            # tools/skills to execute this work-item. If present, do NOT fail.
            proc_def_id = str(row.get("proc_def_id") or "").strip()
            activity_id = str(row.get("activity_id") or "").strip()
            act_tools, act_skills = _load_proc_def_activity_capabilities(
                tenant_id=str(tenant_id or ""),
                proc_def_id=proc_def_id,
                activity_id=activity_id,
            )
            # If activity declares tools, ensure they're loaded (MCP adapters) before building fallback subagent.
            if tool_loader is not None and act_tools:
                try:
                    existing_names = {
                        str(getattr(t, "name", type(t).__name__)) for t in (all_tools or [])
                    }
                    wanted = [t for t in act_tools if t and t not in existing_names]
                    if wanted:
                        more = tool_loader.create_tools_from_names(tool_names=wanted, agent_type=None)
                        more = _ensure_langchain_tools(list(more or []))
                        # Preserve existing tool instances; append only new identities.
                        seen_ids = {id(t) for t in (all_tools or [])}
                        for t in more:
                            if id(t) not in seen_ids:
                                all_tools.append(t)
                                seen_ids.add(id(t))
                        logger.info(
                            "액티비티 도구 추가 로드 완료 | task_id=%s requested=%d loaded=%d",
                            task_id,
                            len(wanted),
                            len(more or []),
                        )
                except Exception:
                    logger.exception(
                        "액티비티 도구 추가 로드 실패(계속 진행) | task_id=%s tools=%s",
                        task_id,
                        act_tools,
                        exc_info=True,
                    )
            fallback = _build_fallback_activity_subagent(
                tenant_id=str(tenant_id or ""),
                tool_user_id=str(tool_user_id or ""),
                all_tools=all_tools,
                activity_tools=act_tools,
                activity_skills=act_skills,
            )
            if fallback is not None:
                subagents = [fallback]
                activity_fallback_tools = list(act_tools or [])
                agent_rows = [
                    {
                        "id": tool_user_id or "activity_fallback",
                        "alias": fallback.get("name"),
                        "username": fallback.get("name"),
                        "name": fallback.get("name"),
                        "role": "업무 실행",
                        "description": "프로세스 액티비티 정의 기반 폴백 실행자",
                        "skills": ",".join(act_skills or []),
                        "tools": ",".join(act_tools or []),
                        "agent_type": "activity_fallback",
                    }
                ]
                logger.info(
                    "서브에이전트 폴백 활성화 | task_id=%s proc_def_id=%s activity_id=%s tools=%d skills=%d picked_tools=%d",
                    task_id,
                    proc_def_id or "(empty)",
                    activity_id or "(empty)",
                    len(act_tools),
                    len(act_skills),
                    len(fallback.get("tools") or []),
                )
            else:
                # Chat mode: allow running without any subagents.
                # - HTTP chat (legacy) and SDK chat both represent "single-agent chat" entrypoints.
                # - In this mode, the root orchestrator should still run (with its tools) even when
                #   there is no domain agent configured and the process/activity doesn't define a fallback.
                if is_chat:
                    logger.info(
                        "서브에이전트 없이 채팅 모드로 계속 진행 | task_id=%s proc_def_id=%s activity_id=%s",
                        task_id,
                        proc_def_id or "(empty)",
                        activity_id or "(empty)",
                    )
                    subagents = []
                    agent_rows = []
                else:
                    logger.error(
                        "Subagents are required (no activity fallback): domain_agent_rows=%d subagents_domain=%d task_id=%s proc_def_id=%s activity_id=%s",
                        len(domain_agent_rows),
                        len(subagents_domain),
                        task_id,
                        proc_def_id or "(empty)",
                        activity_id or "(empty)",
                    )
                    raise MissingSubagentsConfigurationError()

        def _domain_tool_result_hook(
            *,
            base_tool: Any,
            job_id: str,
            tool_input: Any,
            tool_output: Any,
            error: Exception | None,
        ) -> None:
            """Capture structured stdout from domain / pipeline subagent runs for form backfill."""
            # HTTP SSE: tool_end 이벤트 스트리밍(성공/실패 모두).
            try:
                if http_stream_event_emit is not None:
                    tname0 = str(getattr(base_tool, "name", "") or "")
                    payload: dict[str, Any] = {
                        "type": "tool_end",
                        "tool": http_chat_tool_display_name(base_tool, tname0),
                    }
                    if error is not None:
                        payload["output"] = {"error": f"{type(error).__name__}: {str(error)}"}
                    else:
                        # ToolMessage/AIMessage 류는 .content에 실제 텍스트가 들어가는데,
                        # str()로 찍으면 "content='...'" repr이 되어 표준 포맷에 맞지 않는다.
                        content0 = getattr(tool_output, "content", None)
                        if isinstance(content0, str):
                            # JSON이면 dict로, 아니면 {"content": "..."}로 감싼다.
                            s = content0.strip()
                            if s.startswith("{") and s.endswith("}"):
                                try:
                                    parsed = json.loads(s)
                                except Exception:
                                    parsed = None
                                payload["output"] = parsed if isinstance(parsed, dict) else {"content": content0}
                            else:
                                payload["output"] = {"content": content0}
                        elif isinstance(tool_output, dict):
                            payload["output"] = tool_output
                        elif isinstance(tool_output, list):
                            payload["output"] = {"content": json.dumps(tool_output, ensure_ascii=False)}
                        elif isinstance(tool_output, (str, int, float, bool)):
                            payload["output"] = {"content": str(tool_output)}
                        else:
                            payload["output"] = {"content": str(tool_output)}
                    _emit_http_stream_event(http_stream_event_emit, payload)
            except Exception:
                logger.exception("http_chat tool_end emit failed", exc_info=True)

            def _best_effort_extract_tool_output_dict(value: Any) -> dict[str, Any] | None:
                """Attempt to recover a dict-shaped tool output.

                Why: LangChain/ToolNode sometimes hands us a ToolMessage-like object or stringified
                representation (e.g. ``content='{"exit_code":0,"stdout":"{...}"}'``). In those cases,
                the finalizer evidence path would otherwise miss rich stdout JSON.
                """
                if value is None:
                    return None
                if isinstance(value, dict):
                    return value
                # ToolMessage-like objects often have `.content`.
                content = getattr(value, "content", None)
                if isinstance(content, str) and content.strip():
                    s = content.strip()
                else:
                    # Fall back to string form (may include "content='...'" wrapper).
                    try:
                        s = str(value)
                    except Exception:
                        return None
                    s = s.strip()
                if not s:
                    return None
                # Case 1) Raw JSON object string.
                if s.startswith("{") and s.endswith("}"):
                    try:
                        parsed = json.loads(s)
                    except Exception:
                        parsed = None
                    return parsed if isinstance(parsed, dict) else None
                # Case 2) Wrapped like: content='<JSON>' name='tool' tool_call_id='...'
                m = re.search(r"content=(?:'|\")(\{[\s\S]*?\})(?:'|\")", s)
                if m:
                    blob = (m.group(1) or "").strip()
                    if blob:
                        try:
                            parsed = json.loads(blob)
                        except Exception:
                            parsed = None
                        if isinstance(parsed, dict):
                            return parsed
                # Case 3) Best-effort: grab the first {...} substring and parse.
                m2 = re.search(r"(\{[\s\S]*\})", s)
                if not m2:
                    return None
                blob2 = (m2.group(1) or "").strip()
                if not blob2:
                    return None
                try:
                    parsed2 = json.loads(blob2)
                except Exception:
                    parsed2 = None
                return parsed2 if isinstance(parsed2, dict) else None

            if error is not None:
                return
            tname = str(getattr(base_tool, "name", "") or "")
            if tname == "sandbox_upload_file_to_public_storage":
                if isinstance(tool_output, dict):
                    pu = tool_output.get("public_url")
                    if isinstance(pu, str) and pu.strip():
                        entry: dict[str, Any] = {
                            "name": tool_output.get("name") or "deliverable",
                            "public_url": pu.strip(),
                        }
                        sp = tool_output.get("storage_path")
                        if isinstance(sp, str) and sp.strip():
                            entry["storage_path"] = sp.strip()
                        mime = tool_output.get("mime_type")
                        if isinstance(mime, str) and mime.strip():
                            entry["mime_type"] = mime.strip()
                        sz = tool_output.get("size_bytes")
                        if isinstance(sz, int) and sz >= 0:
                            entry["size_bytes"] = sz
                        deliverable_files_from_tools.append(entry)
                return
            if tname not in (
                "sandbox_run_shell",
                "sandbox_write_and_run",
                "sandbox_write_files_and_run",
            ):
                return
            out_dict = _best_effort_extract_tool_output_dict(tool_output)
            if not isinstance(out_dict, dict):
                return
            stdout = out_dict.get("stdout") if isinstance(out_dict.get("stdout"), str) else ""
            stderr = out_dict.get("stderr") if isinstance(out_dict.get("stderr"), str) else ""
            if not stdout.strip():
                return
            bucket = structured_domain_outputs_by_job.setdefault(job_id, {})
            items = bucket.setdefault(tname, [])
            if isinstance(items, list):
                parsed: dict[str, Any] | None = None
                try:
                    tmp = json.loads(stdout.strip())
                    if isinstance(tmp, dict):
                        parsed = tmp
                except Exception:
                    parsed = None
                items.append(
                    {
                        "command": out_dict.get("command"),
                        "exit_code": out_dict.get("exit_code"),
                        "parsed_stdout": parsed,
                        # Always include text heads so the finalizer can still extract values
                        # even when stdout isn't strict JSON.
                        "stdout_head": stdout.strip()[:8000],
                        "stderr_head": stderr.strip()[:8000],
                    }
                )

        subagent_contexts: list[dict[str, Any]] = []
        # 서브 에이전트별 메타데이터/ExecutionContext를 준비한다.
        # job_id는 task() 호출마다 동적으로 부여되며, 이벤트/툴 로깅은 미들웨어에서 스코프된다.
        for idx, row in enumerate(agent_rows):
            # subagents[idx]["name"] is already processed through _openai_safe_agent_name,
            # which is what the LLM sees and calls in task(subagent_type=...).
            # name_to_context must be keyed by this exact name for middleware matching.
            if idx < len(subagents):
                agent_name = subagents[idx]["name"]
            else:
                agent_name = (
                    row.get("alias")
                    or row.get("username")
                    or row.get("name")
                    or str(row.get("id"))
                )
            agent_role = row.get("role") or ""
            agent_goal = row.get("goal") or ""
            agent_profile = row.get("agent_profile") or "/images/chat-icon.png"
            agent_id = row.get("id")
            subagent_job_id = str(uuid.uuid4())

            # 서브에이전트 도구 호출은 이 에이전트 이름으로 로깅되도록 전용 ExecutionContext 사용.
            # (동일 exec_ctx를 쓰면 서브가 호출한 도구가 모두 작업 실행 기획자로 기록됨)
            _role = (agent_role or "").strip() or None
            sub_exec_ctx = ExecutionContext(
                correlation_id=exec_ctx.correlation_id,
                agent_path=list(exec_ctx.agent_path) + [agent_name],
                task_id=exec_ctx.task_id,
                proc_inst_id=exec_ctx.proc_inst_id,
                agent_role=_role,
            )
            if idx < len(subagents):
                sub_tools = subagents[idx].get("tools", [])
                tool_input_augmenter = None
                # Domain/DB subagents: allow skill tools directly (no centralized discovery subagent).
                # Deterministic gating: read docs → materialize → run.
                domain_tier_state: dict[str, bool] = {
                    "tier1_attempted": False,
                    "mounted_skill_copied": False,
                    "skill_docs_read": False,
                }
                domain_has_memento_tool = any(
                    str(getattr(t, "name", "") or "") == "memento" for t in sub_tools
                )

                def _effective_skill_docs_read() -> bool:
                    return bool(domain_tier_state.get("skill_docs_read"))

                def _effective_tier1_attempted() -> bool:
                    return bool(domain_tier_state.get("tier1_attempted"))

                def _domain_tool_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                    """Token/robustness hygiene for domain subagent tool inputs."""
                    tool_name = str(getattr(base_tool, "name", "") or "")
                    merged = _coerce_tool_input_dict(tool_input)
                    if merged is None:
                        return tool_input
                    args = merged.get("args") if isinstance(merged.get("args"), dict) else merged
                    if not isinstance(args, dict):
                        return merged

                    # 1) Prevent huge context blowups from skill discovery outputs.
                    # `find_helpful_skills(list_documents=true)` can return full SKILL.md bodies ("Full Content"),
                    # which then gets re-injected into subsequent model calls.
                    if tool_name == "find_helpful_skills":
                        if args.get("list_documents") is True:
                            args["list_documents"] = False
                        # Keep discovery small; the model can re-run with a larger k if needed.
                        try:
                            tk = int(args.get("top_k") or 0)
                        except Exception:
                            tk = 0
                        if tk <= 0 or tk > 3:
                            args["top_k"] = 3

                        # (User requirement) Inject agent's profile skills into allowed_skill_names
                        agent_skills = allowed_skill_names_by_agent.get(agent_name) or []
                        if agent_skills:
                            existing = args.get("allowed_skill_names") or []
                            if isinstance(existing, str):
                                existing = [s.strip() for s in existing.split(",") if s.strip()]
                            elif not isinstance(existing, list):
                                existing = []
                            # Merge and unique
                            merged_skills = list(dict.fromkeys(list(existing) + list(agent_skills)))
                            args["allowed_skill_names"] = merged_skills

                        return merged

                    # 2) Fix common confusion: passing FS absolute paths to MCP doc readers.
                    if tool_name != "read_skill_document":
                        return merged
                    dp = str(args.get("document_path") or "").strip()
                    if dp.startswith("/") and ("/app/skills" in dp or "/skills/" in dp):
                        # Force listing mode; the agent must pick a valid MCP document path.
                        args["document_path"] = None
                    return merged

                def _domain_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                    tool_name = str(getattr(base_tool, "name", "") or "")

                    # HTTP SSE: tool_start 이벤트 스트리밍(호출 직전).
                    try:
                        if http_stream_event_emit is not None:
                            # "호출 직전 예고" 이벤트: plan_tools / plan_skills
                            try:
                                merged0 = _coerce_tool_input_dict(tool_input)
                                args0: dict[str, Any] = {}
                                tid0 = ""
                                if isinstance(merged0, dict):
                                    if isinstance(merged0.get("args"), dict):
                                        args0 = dict(merged0["args"])
                                    tid0 = str(merged0.get("id") or "")
                                _emit_http_plan_event(
                                    http_stream_event_emit,
                                    {
                                        "type": "plan_tools",
                                        "tools": [
                                            {
                                                "tool": http_chat_tool_display_name(base_tool, tool_name),
                                                "name": tool_name,
                                                "args": _json_safe(args0),
                                                "id": tid0,
                                            }
                                        ],
                                    },
                                )
                                skills0 = _planned_skills_from_tool_call(tool_name, args0)
                                if skills0:
                                    _emit_http_plan_event(
                                        http_stream_event_emit,
                                        {"type": "plan_skills", "skills": skills0},
                                    )
                            except Exception:
                                logger.exception("http_chat plan_tools emit failed; continuing", exc_info=True)
                            _emit_http_stream_event(
                                http_stream_event_emit,
                                {
                                    "type": "tool_start",
                                    "tool": http_chat_tool_display_name(base_tool, tool_name),
                                    "input": _coerce_tool_input_dict(tool_input) or tool_input,
                                },
                            )
                    except Exception:
                        logger.exception("http_chat tool_start emit failed", exc_info=True)

                        # Never allow domain agents to mutate the shared skill catalog.
                    if tool_name in ("delete_skill", "delete_skill_file", "update_skill_file"):
                            return {
                                "error": "skill_catalog_mutation_forbidden",
                                "message": (
                                    "도메인 서브에이전트는 스킬 카탈로그를 수정/삭제할 수 없습니다. "
                                "필요하면 오케스트레이터 레벨에서 처리하세요."
                                ),
                                "requires_human": False,
                            }

                        # Track Tier 1 attempt for deterministic fallback gating.
                    if tool_name == "skill_volume_probe":
                            domain_tier_state["tier1_attempted"] = True
                            return None

                        # Domain must consult skill docs (from handoff) before any execution/materialization.
                        # This enforces "read docs → materialize → run", instead of inventing endpoints/scripts.
                    if tool_name == "read_skill_document":
                            domain_tier_state["skill_docs_read"] = True
                            return None

                    # Domain subagents are allowed to use skill discovery.
                    # (User requirement) `find_helpful_skills` 제한 금지.
                    if tool_name == "find_helpful_skills":
                        # Treat discovery as a Tier1-attempt-equivalent for gating.
                        # Some tasks legitimately have no mounted skill; we still want a
                        # deterministic "discovery happened" signal before allowing sandbox execution.
                        domain_tier_state["tier1_attempted"] = True
                        return None

                        # Require skill materialization before running arbitrary sandbox code.
                        # This prevents "ignore skill discovery, generate random API script" failures.
                    if tool_name == "sandbox_copy_mounted_skill":
                            if not _effective_skill_docs_read():
                                return {
                                    "error": "skill_docs_required_before_materialization",
                                    "message": (
                                        "스킬을 샌드박스에 복사/물리화하기 전에 스킬 문서(엔드포인트/사용법) 확인이 필요합니다. "
                                        "`read_skill_document(skill_name=...)`로 확인하세요."
                                    ),
                                    "requires_human": False,
                                }
                            domain_tier_state["mounted_skill_copied"] = True
                            return None
                    if tool_name in ("sandbox_write_and_run", "sandbox_run_shell") or tool_name.startswith(
                            "sandbox_write_"
                        ):
                            if not _effective_skill_docs_read():
                                return {
                                    "error": "skill_docs_required_before_sandbox",
                                    "message": (
                                        "샌드박스 실행/작성 전에 스킬 문서 확인이 필요합니다. "
                                        "`read_skill_document` 후 `sandbox_copy_mounted_skill` 순으로 진행하세요."
                                    ),
                                    "requires_human": False,
                                }
                            # For FS-mount skills, require sandbox_copy_mounted_skill first.
                            # For MCP-document skills, allow sandbox_write_* materialization after docs are read.
                            if not domain_tier_state.get("mounted_skill_copied"):
                                # Allow sandbox execution when we already attempted skill discovery,
                                # but no mounted skill was materialized (common for pure lookup tasks).
                                if not _effective_tier1_attempted():
                                    return {
                                        "error": "skill_materialization_required_before_sandbox",
                                        "message": (
                                            "샌드박스에서 임의 코드 실행 전에 스킬을 먼저 물리화해야 합니다. "
                                            "`sandbox_copy_mounted_skill(workspace_id, tenant_id, skill_name, dest_subdir=...)`로 "
                                            "스킬 파일을 복사한 뒤 진행하세요."
                                        ),
                                        "requires_human": False,
                                    }

                        # NOTE: 과거 핸드오프 기반 required_env 강제는 제거됨.

                    # Skill MCP tools are allowed only as fallback, after Tier 1 was attempted.
                    is_skill_mcp = tool_name in (
                            "list_skills",
                            "read_skill_document",
                            "find_helpful_skills",
                        )
                    # Allow domain to read skill docs anytime (handoff-driven), even without Tier1 probe.
                    if tool_name == "read_skill_document":
                        return None
                    if is_skill_mcp and not _effective_tier1_attempted():
                            return {
                                "error": "tier1_required_before_fallback_skill_discovery",
                                "message": (
                                    "폴백으로 직접 MCP 탐색이 필요하면, 먼저 Tier1(볼륨) 확인을 위해 "
                                    "`skill_volume_probe`를 호출하세요. 이후 `list_skills`/`read_skill_document`를 진행할 수 있습니다."
                                ),
                                "requires_human": False,
                            }

                    return None

                tool_call_guard = _domain_tool_call_guard
                tool_input_augmenter = _domain_tool_input_augmenter

                _instrument_tools(
                    sub_tools,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    crew_type="deepagents",
                    job_id_getter=_subagent_tool_job_id_getter(subagent_job_id),
                    job_tool_log=subagent_tool_log,
                    job_skill_log=subagent_skill_log,
                    tool_result_hook=_domain_tool_result_hook,
                    exec_ctx=sub_exec_ctx,
                    event_queue=event_queue,
                    tool_input_augmenter=tool_input_augmenter,
                    tool_call_guard=tool_call_guard,
                    context_tool_tracker=context_tool_tracker,
                )
                logger.info(
                    "서브에이전트 도구 인스트루먼트 완료 | task_id=%s agent=%s tools=%d",
                    task_id,
                    agent_name,
                    len(sub_tools),
                )

            subagent_contexts.append(
                {
                    "job_id": subagent_job_id,
                    "role": agent_role,
                    "name": agent_name,
                    "goal": agent_goal,
                    "agent_profile": agent_profile,
                    "agent_id": agent_id,
                    "exec_ctx": sub_exec_ctx,
                    "subagent_proc_inst_id": row.get("proc_inst_id") or "",
                    "form_fields_json": row.get("form_fields_json") or None,
                }
            )

        # NOTE: 파이프라인 역할 서브에이전트(고정된 bpmn/pdf/xlsx 역할)는 코드에 존재하면 안 된다.
        # 도메인 실행 서브에이전트는 DB에서 동적으로 로드/구성한다.

        instrumented_hitl_tools = list(get_hitl_tools())
        _instrument_tools(
            instrumented_hitl_tools,
            todo_id=task_id or None,
            proc_inst_id=proc_inst_id or None,
            crew_type="deepagents",
            job_id_getter=lambda: job_uuid,
            job_tool_log=subagent_tool_log,
            job_skill_log=subagent_skill_log,
            exec_ctx=exec_ctx,
            event_queue=event_queue,
            tool_input_augmenter=None,
            context_tool_tracker=context_tool_tracker,
        )

        routing_cfg = resolve_model_routing(extras, default_model="anthropic:claude-opus-4-6")
        model_name = routing_cfg.model_name

        # 단일 DeepAgent 런타임(Planning + Delegation + Execution)
        feedback_summary = extras.get("summarized_feedback") or ""
        form_fields = extras.get("form_fields")
        form_html = extras.get("form_html")
        form_info = None
        if form_fields or form_html:
            form_info = {"fields": form_fields, "html": form_html}

        # 사용자가 한글로 입력한 경우 plans/result를 한글로 작성하도록 지시
        user_text_for_lang = (
            invoke_user_text.strip()
            or f"{description or ''} {instruction or ''}".strip()
        )
        output_language = "korean" if _is_korean_user_input(user_text_for_lang) else None

        collected_assets = collect_input_assets(
            input_data=input_data,
            extra_text=invoke_user_text,
            extra_assets=proc_inst_sources if proc_inst_sources else None,
        )
        pipeline_asset_roles = resolve_asset_roles(
            assets=collected_assets,
            user_text=f"{user_text_for_lang} {description} {instruction}".strip(),
        )
        asset_inventory = build_asset_inventory(collected_assets)
        # Chat policy flags (avoid prompt injection; enforce via routing/guardrails).
        http_chat_policy: dict[str, Any] = {}
        try:
            _has_assets = bool(
                isinstance(asset_inventory, dict) and int(asset_inventory.get("total_count") or 0) > 0
            )
            http_chat_policy = {
                "has_input_assets": _has_assets,
            }
            if is_chat:
                extras["http_chat_policy"] = dict(http_chat_policy)
        except Exception:
            http_chat_policy = {}
        # Prompt-size instrumentation: helps identify sudden growth from long URLs / many assets.
        try:
            roles_blob_preview = format_pipeline_asset_roles_for_system_prompt(pipeline_asset_roles or {})
            logger.info(
                "prompt_budget: assets=%d asset_roles_chars=%d asset_inventory_chars=%d",
                len(collected_assets or []),
                len(roles_blob_preview or ""),
                len(json.dumps(asset_inventory or {}, ensure_ascii=False)),
            )
        except Exception:
            logger.exception("prompt_budget instrumentation failed", exc_info=True)
        initial_pipeline_outputs: dict[str, Any] = {}
        if asset_inventory:
            initial_pipeline_outputs["asset_inventory"] = asset_inventory
        if isinstance(pipeline_asset_roles, dict) and pipeline_asset_roles:
            initial_pipeline_outputs["asset_roles"] = pipeline_asset_roles
        try:
            _inv_blob = json.dumps(asset_inventory, ensure_ascii=False)
            _state_contract = json.dumps(
                {
                    "standard_state_keys": [
                        "asset_inventory",
                        "asset_roles",
                        "template_schema",
                        "process_facts",
                        "reference_facts",
                        "filled_template",
                        "artifact_file",
                        "public_url",
                        "deliverable_files",
                        "execution_trace",
                    ],
                    "orchestration": (
                        "DeepAgents 그래프 내부에서 task()로 서브에이전트를 호출합니다. "
                        "템플릿 PDF(role=template_candidate)가 있으면 BPMN만으로 엑셀 양식 구조를 정의하지 마세요. "
                        "산출 JSON·파일 경로·public_url은 서브에이전트 실행 결과에 명시하세요."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception:
            _inv_blob, _state_contract = "{}", "{}"
        http_chat_has_assets = bool(is_chat and bool(http_chat_policy.get("has_input_assets")))
        base_rules = default_orchestrator_prompt()

        # Initial system_prompt should be minimal; per-step sections are injected by middleware.
        system_prompt = base_orchestrator_identity_prompt()
        try:
            logger.info("prompt_budget: system_prompt_chars=%d", len(system_prompt or ""))
        except Exception:
            pass
        # NOTE: HTTP rules are injected via prompt-sections middleware (per step).

        # NOTE: 루트(DeepAgent) task_started 는 서브에이전트 이벤트보다 먼저 발행되어야 한다.
        # (DB 인입 시점 기준으로 서브가 먼저 시작한 것처럼 보이는 문제 방지)
        enqueue_status_update(
            event_queue,
            "task_started",
            {
                "message": "단일 DeepAgent 실행을 시작했습니다.",
                "goal": "에이전트가 목표를 수립하고 작업 계획을 작성하는 중입니다.",
                "routing_mode": routing_cfg.route_mode,
                "model": model_name,
            },
            proc_inst_id,
            task_id,
            "deepagents",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="planning",
            agent_name="작업 실행 기획자",
        )

        # task() 실행 구간에서 skill 볼륨 FS 접근의 job_id/exec_ctx 를 서브 에이전트에 맞춘다.
        # 서브에이전트 started/completed 는 task() 호출 시점 미들웨어에서 발행하고,
        # 미들웨어가 동작하지 않은 런타임만 후단 fallback 이벤트를 발행한다.
        #
        # 플래닝 phase의 task_completed 는 write_todos 또는 write_pipeline_spec 성공 시점에 발행한다.
        planning_progress: dict[str, Any] = {
            "saw_write_todos": False,
            "saw_write_pipeline_spec": False,
            "planning_completed_emitted": False,
            "saw_task_delegation": False,
        }
        route_progress: dict[str, Any] = {"route": "", "reason": ""}
        # Policy-based default routing:
        # - 프로세스 모드에서도 서브에이전트가 있으면 "반드시 위임" 정책을 위해 needs_task로 둔다.
        #   (PromptSectionsMiddleware가 멀티에이전트 규칙을 주입하도록 하여 task() 호출을 유도)
        # - HTTP 채팅에서는 입력 자산이 있을 때도 needs_task를 선점한다.
        if subagent_contexts:
            route_progress["route"] = "needs_task"
            route_progress["reason"] = "subagents_present"
        elif is_chat and bool(http_chat_policy.get("has_input_assets")):
            route_progress["route"] = "needs_task"
            route_progress["reason"] = "input_assets_present"
        elif is_chat:
            route_progress["route"] = "needs_task"
            route_progress["reason"] = "chat_default_no_subagents_no_assets"
        pipeline_spec_mw = create_pipeline_spec_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=job_uuid,
            exec_ctx=exec_ctx,
            planning_gate=planning_progress,
            route_gate=route_progress,
        )
        write_todos_planning_mw = _create_write_todos_planning_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=job_uuid,
            exec_ctx=exec_ctx,
            planning_gate=planning_progress,
        )
        http_chat_route_mw = None
        # HTTP chat: when input assets exist, allow direct delegation to avoid hallucination loops.
        # For asset-free requests, keep the planning gate to prevent root from using non-MCP tools prematurely.
        enforce_write_todos_planning = bool(is_chat and not bool(http_chat_policy.get("has_input_assets")))

        def _planning_completed_flag() -> bool:
            # write_todos / write_pipeline_spec 미들웨어도 같은 dict 를 갱신한다(단일 소스).
            return bool(planning_progress.get("planning_completed_emitted"))

        async def _emit_planning_completed_fallback(message: str) -> None:
            """플랜 본문을 포착하지 못한 경우에도 planning 완료 이벤트를 보장한다."""
            if _planning_completed_flag():
                return
            enqueue_status_update(
                event_queue,
                "task_completed",
                {"message": message},
                proc_inst_id,
                task_id,
                "deepagents",
                job_id=job_uuid,
                exec_ctx=exec_ctx,
                logical_event_type="agent_finished",
                phase="planning",
                agent_name="작업 실행 기획자",
            )
            planning_progress["planning_completed_emitted"] = True

        async def _before_subagent_start_callback() -> None:
            await _emit_planning_completed_fallback("플래닝이 완료되어 실행 단계로 전환합니다.")

        name_to_context = {str(c["name"]): c for c in subagent_contexts}
        subagent_event_middleware, subagent_event_state = _create_subagent_event_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            name_to_context=name_to_context,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            on_before_subagent_start=_before_subagent_start_callback,
            planning_gate=planning_progress if enforce_write_todos_planning else None,
            route_gate=route_progress,
        )
        extra_middleware: list[Any] = [SubagentJobScopeMiddleware()]
        # Per-step prompt composition: inject only the sections needed for the current step.
        prompt_sections = build_orchestrator_prompt_sections(
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
            output_language=output_language,
            hitl_resume=hitl_resume,
            pipeline_asset_roles=pipeline_asset_roles,
        )
        psmw = create_prompt_sections_middleware(
            sections=prompt_sections,
            is_chat=is_chat,
            route_gate=route_progress,
            planning_gate=planning_progress,
            include_execution_clock=bool(
                any(
                    k in (invoke_user_text or "")
                    for k in (
                        "오늘",
                        "어제",
                        "전일",
                        "이번주",
                        "지난주",
                        "이번 달",
                        "지난달",
                        "이번달",
                        "지난 달",
                        "yesterday",
                        "today",
                        "last week",
                        "this week",
                        "last month",
                        "this month",
                    )
                )
            ),
        )
        if psmw is not None:
            extra_middleware.append(psmw)

        # Subagents: build per-agent middleware so agent-specific sections (role, skills,
        # form fields) are included alongside the shared sections.
        # NOTE: deepagents does not propagate root middleware into subagent graphs automatically.
        _include_clock = bool(
            any(
                k in (invoke_user_text or "")
                for k in (
                    "오늘",
                    "어제",
                    "전일",
                    "이번주",
                    "지난주",
                    "이번 달",
                    "지난달",
                    "이번달",
                    "지난 달",
                    "yesterday",
                    "today",
                    "last week",
                    "this week",
                    "last month",
                    "this month",
                )
            )
        )
        for sa in subagents:
            try:
                sub_sections = build_subagent_prompt_sections(
                    tenant_id=tenant_id or None,
                    skill_root_dir=str(os.environ.get("SKILLS_ROOT_DIR") or "/app/skills"),
                    output_language=output_language,
                    agent_info=sa.pop("_agent_info", None),
                )
                sub_psmw = create_subagent_prompt_sections_middleware(
                    sections=sub_sections,
                    include_execution_clock=_include_clock,
                )
                if sub_psmw is not None:
                    chain = sa.get("middleware")
                    if isinstance(chain, list):
                        chain.append(sub_psmw)
                    else:
                        sa["middleware"] = [sub_psmw]
            except Exception:
                logger.exception("Failed to attach subagent prompt sections middleware for agent=%s; continuing", sa.get("name"), exc_info=True)
        deep_gate_mw = None
        if is_chat:
            deep_gate_mw = _create_deep_required_root_gate_middleware(
                planning_gate=planning_progress,
                route_gate=route_progress,
            )
        if deep_gate_mw is not None:
            extra_middleware.append(deep_gate_mw)
        if http_chat_route_mw is not None:
            extra_middleware.append(http_chat_route_mw)
        if pipeline_spec_mw is not None:
            extra_middleware.append(pipeline_spec_mw)
        if write_todos_planning_mw is not None:
            extra_middleware.append(write_todos_planning_mw)
        if subagent_event_middleware is not None:
            extra_middleware.append(subagent_event_middleware)

        # gpt-oss 등 입력 상한이 빡봉한 프록시 모델: 도구 결과가 다음 턴 컨텍스트를 터뜨리지 않도록 축약.
        # deepagents는 루트 middleware= 를 서브에이전트 그래프로 전달하지 않으므로, 동일 인스턴스를 spec에도 붙인다.
        compact_tr_mw = create_compact_tool_result_middleware(model_name=model_name)
        if compact_tr_mw is not None:
            extra_middleware.append(compact_tr_mw)
            for sa in subagents:
                chain = sa.get("middleware")
                if isinstance(chain, list):
                    chain.append(compact_tr_mw)
                else:
                    sa["middleware"] = [compact_tr_mw]

        # gpt-oss(LiteLLM): 채널/텍스트 형태의 도구 호출을 AIMessage.tool_calls 로 정규화 (compact 다음 = 모델에 가장 가까운 쪽).
        oss_norm_mw = create_oss_channel_tool_call_middleware(model_name=model_name)
        if oss_norm_mw is not None:
            extra_middleware.append(oss_norm_mw)
            for sa in subagents:
                chain = sa.get("middleware")
                if isinstance(chain, list):
                    chain.append(oss_norm_mw)
                else:
                    sa["middleware"] = [oss_norm_mw]

        # Always-on projection: keep only necessary tool output fields for next turns.
        # This runs before token_hygiene (truncate) so we prefer structured shrinking over blind cutting.
        proj_rules = extras.get("tool_result_projection_rules") if isinstance(extras, dict) else None
        proj_mw = create_tool_result_projection_middleware_with_rules(
            rules=proj_rules if isinstance(proj_rules, dict) else None
        )
        if proj_mw is not None:
            extra_middleware.append(proj_mw)
            for sa in subagents:
                chain = sa.get("middleware")
                if isinstance(chain, list):
                    chain.append(proj_mw)
                else:
                    sa["middleware"] = [proj_mw]

        # Always-on token hygiene: truncate known huge tool results (skill discovery/catalog dumps, sandbox stdout).
        # This is independent of model routing; it protects long-running task() loops from context bloat.
        th_mw = create_token_hygiene_tool_result_middleware(max_chars=12_000)
        if th_mw is not None:
            extra_middleware.append(th_mw)
            for sa in subagents:
                chain = sa.get("middleware")
                if isinstance(chain, list):
                    chain.append(th_mw)
                else:
                    sa["middleware"] = [th_mw]

        # Root orchestrator should have access to the same tenant/MCP tools as subagents,
        # plus sandbox deliverable upload tools when the user requests file creation.
        root_tools_for_orchestrator: list[Any] = []
        if is_chat:
            # Chat (HTTP or SDK SSE): keep root MCP exposure bounded (tool schemas dominate input tokens).
            # Include only a subset of work-assistant tools; delegate heavy work via `task()`.
            hinted = extras.get("http_chat_work_assistant_allowlist")
            allowlist = hinted if isinstance(hinted, list) else None
            if allowlist:
                picked = select_http_chat_root_work_assistant_tools_allowlist(all_tools, allowlist=allowlist)
                if picked:
                    root_tools_for_orchestrator = picked
                else:
                    root_tools_for_orchestrator = select_http_chat_root_work_assistant_tools(all_tools)
            else:
                root_tools_for_orchestrator = select_http_chat_root_work_assistant_tools(all_tools)
            # memento는 채팅 모드 루트 오케스트레이터의 기본 도구로 항상 포함한다.
            _chat_root_tool_names = {str(getattr(t, "name", "") or "") for t in root_tools_for_orchestrator}
            for _t in (all_tools or []):
                if str(getattr(_t, "name", "") or "").strip() == "memento" and "memento" not in _chat_root_tool_names:
                    root_tools_for_orchestrator.append(_t)
                    _chat_root_tool_names.add("memento")
        else:
            # Token hygiene (process mode):
            # If activity fallback declares tools, keep root tool schemas slim as well
            # (even when the run completes without any task() delegation).
            if activity_fallback_tools:
                root_tools_for_orchestrator = _select_tools_for_activity_tools(
                    all_tools=list(all_tools or []),
                    activity_tools=activity_fallback_tools,
                )
            else:
                try:
                    root_tools_for_orchestrator.extend(all_tools or [])
                except Exception:
                    root_tools_for_orchestrator = list(all_tools) if all_tools else []
                try:
                    root_tools_for_orchestrator.extend(list(get_sandbox_tools()))
                except Exception:
                    logger.exception("Failed to attach sandbox tools to root orchestrator", exc_info=True)
                try:
                    root_tools_for_orchestrator.extend(list(get_skill_resolution_tools()))
                except Exception:
                    logger.exception("Failed to attach skill resolution tools to root orchestrator", exc_info=True)
                try:
                    root_tools_for_orchestrator.extend(list(get_skill_tools()))
                except Exception:
                    logger.exception("Failed to attach skill tools to root orchestrator", exc_info=True)

        # De-duplicate by tool name to avoid LangChain duplicate tool errors.
        deduped_root_tools: list[Any] = []
        seen_tool_names: set[str] = set()
        for t in root_tools_for_orchestrator:
            nm = getattr(t, "name", None) or type(t).__name__
            key = str(nm)
            if key in seen_tool_names:
                continue
            seen_tool_names.add(key)
            deduped_root_tools.append(t)

        plan_tools_by_name: dict[str, Any] = {}
        for t in deduped_root_tools:
            nm = str(getattr(t, "name", "") or "").strip()
            if nm and nm not in plan_tools_by_name:
                plan_tools_by_name[nm] = t

        # 루트 오케스트레이터 도구(MCP 등)도 서브와 동일하게 invoke 래핑 → HTTP tool_calls / SSE tool_* 에 남긴다.
        try:

            def _emit_http_plan_for_tool_start(base_tool: Any, tool_name: str, tool_input: Any) -> None:
                """Emit plan_tools/plan_skills/plan_todos immediately before tool_start (HTTP SSE)."""
                if http_stream_event_emit is None:
                    return
                merged = _coerce_tool_input_dict(tool_input)
                args0: dict[str, Any] = {}
                tid0 = ""
                if isinstance(merged, dict):
                    if isinstance(merged.get("args"), dict):
                        args0 = dict(merged["args"])
                    tid0 = str(merged.get("id") or "")
                display = http_chat_tool_display_name(base_tool, tool_name)
                _emit_http_plan_event(
                    http_stream_event_emit,
                    {
                        "type": "plan_tools",
                        "tools": [
                            {
                                "tool": display,
                                "name": tool_name,
                                "args": _json_safe(args0),
                                "id": tid0,
                            }
                        ],
                    },
                )
                skills0 = _planned_skills_from_tool_call(tool_name, args0)
                if skills0:
                    _emit_http_plan_event(
                        http_stream_event_emit,
                        {"type": "plan_skills", "skills": skills0},
                    )
                # For write_todos, emit plan_todos from the tool input directly (state stream may omit it).
                if tool_name == "write_todos":
                    todos0 = args0.get("todos")
                    if todos0 is not None:
                        _emit_http_plan_event(
                            http_stream_event_emit,
                            {"type": "plan_todos", "todos": _json_safe(todos0)},
                        )

            def _root_orchestrator_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                tool_name = str(getattr(base_tool, "name", "") or "")
                try:
                    if http_stream_event_emit is not None:
                        _emit_http_plan_for_tool_start(base_tool, tool_name, tool_input)
                        _emit_http_stream_event(
                            http_stream_event_emit,
                            {
                                "type": "tool_start",
                                "tool": http_chat_tool_display_name(base_tool, tool_name),
                                "input": _coerce_tool_input_dict(tool_input) or tool_input,
                            },
                        )
                except Exception:
                    logger.exception("http_chat root orchestrator tool_start emit failed", exc_info=True)
                return None

            # (User requirement) Inject root agent profile skills into find_helpful_skills calls.
            _chat_aug = None
            if is_chat:
                _chat_aug = make_http_chat_work_assistant_tool_input_augmenter(
                    input_data=input_data if isinstance(input_data, dict) else {},
                    row=row if isinstance(row, dict) else {},
                    extras=extras if isinstance(extras, dict) else {},
                    tenant_id=str(tenant_id or "").strip() or None,
                    chat_identity_tool_ids=frozenset(id(t) for t in deduped_root_tools),
                )

            def _root_orchestrator_tool_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                tool_name = str(getattr(base_tool, "name", "") or "")

                if tool_name == "find_helpful_skills":
                    merged = _coerce_tool_input_dict(tool_input)
                    if isinstance(merged, dict):
                        args = merged.get("args") if isinstance(merged.get("args"), dict) else merged
                        if isinstance(args, dict):
                            # Token hygiene (consistent with subagents)
                            if args.get("list_documents") is True:
                                args["list_documents"] = False
                            try:
                                tk = int(args.get("top_k") or 0)
                            except Exception:
                                tk = 0
                            if tk <= 0 or tk > 3:
                                args["top_k"] = 3

                            # Inject root agent's profile skills
                            agent_skills = allowed_skill_names_by_agent.get(root_agent_name) or []
                            if agent_skills:
                                existing = args.get("allowed_skill_names") or []
                                if isinstance(existing, str):
                                    existing = [s.strip() for s in existing.split(",") if s.strip()]
                                elif not isinstance(existing, list):
                                    existing = []
                                # Merge and unique
                                merged_skills = list(dict.fromkeys(list(existing) + list(agent_skills)))
                                args["allowed_skill_names"] = merged_skills
                        tool_input = merged

                if _chat_aug is not None:
                    return _chat_aug(base_tool, tool_input)
                return tool_input

            _instrument_tools(
                deduped_root_tools,
                todo_id=task_id or None,
                proc_inst_id=proc_inst_id or None,
                crew_type="deepagents",
                job_id_getter=lambda: job_uuid,
                job_tool_log=orchestrator_tool_log,
                job_skill_log=subagent_skill_log,
                tool_result_hook=_domain_tool_result_hook,
                exec_ctx=exec_ctx,
                event_queue=event_queue,
                tool_input_augmenter=_root_orchestrator_tool_input_augmenter,
                tool_call_guard=_root_orchestrator_tool_call_guard,
                context_tool_tracker=context_tool_tracker,
            )
        except Exception:
            logger.exception("root orchestrator tool instrumentation failed; continuing", exc_info=True)

        orchestrator = build_orchestrator(
            model_name=model_name,
            system_prompt=system_prompt,
            subagents=subagents,
            tools=deduped_root_tools,
            hitl_tools=instrumented_hitl_tools,
            middleware=extra_middleware,
            skills=tenant_skill_sources_for_middleware(tenant_id or None),
            memory=tenant_memory_sources_for_middleware(tenant_id or None),
            checkpointer=checkpointer,
            model_routing=routing_cfg,
        )
        logger.info(
            "오케스트레이터 빌드 완료 | task_id=%s model=%s subagents=%d middleware=%d",
            task_id,
            model_name,
            len(subagents),
            len(extra_middleware),
        )

        name_to_sub_ex = {str(c["name"]): c["exec_ctx"] for c in subagent_contexts}
        name_to_subagent_job_id = {
            str(c["name"]): str(c["job_id"])
            for c in subagent_contexts
            if str(c.get("job_id") or "").strip()
        }
        skill_vol_binding = SkillVolumeToolEventBinding(
            event_queue=event_queue,
            todo_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="deepagents",
            default_job_id=job_uuid,
            root_exec_ctx=exec_ctx,
            subagent_name_to_job_id=name_to_subagent_job_id,
            subagent_name_to_exec_ctx=name_to_sub_ex,
        )
        tok_skill_bind = skill_volume_tool_event_binding.set(skill_vol_binding)
        tok_skill_job = skill_volume_active_job_id.set(job_uuid)
        tok_skill_ex = skill_volume_active_exec_ctx.set(exec_ctx)
        try:
            logger.info("오케스트레이터 실행 시작 | task_id=%s", task_id)
            result = None

            async def _emit_planning_completed_if_ready(state: Any) -> None:
                if planning_progress.get("planning_completed_emitted"):
                    return
                if not isinstance(state, dict):
                    return

                todos_state = state.get("todos")
                plan_message = ""
                if todos_state is not None:
                    try:
                        plan_message = _join_todo_contents(todos_state)
                    except Exception:
                        plan_message = ""

                # 플랜이 아직 확정되지 않았으면 대기 (첫 관측 시점에 즉시 발행)
                if not plan_message and todos_state is None:
                    return

                enqueue_status_update(
                    event_queue,
                    "task_completed",
                    {"message": plan_message or "플래닝이 완료되었습니다."},
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_finished",
                    phase="planning",
                    agent_name="작업 실행 기획자",
                )
                planning_progress["planning_completed_emitted"] = True

            stream_emit = extras.get("_http_chat_stream_emit")

            run_config: dict[str, Any] | None = None
            if checkpointer is not None and checkpoint_thread_id:
                run_config = {"configurable": {"thread_id": checkpoint_thread_id}}

            if use_graph_resume:
                state_input: Any = Command(resume={"answer": human_answer_stripped})
            else:
                # Chat: optionally inject recent room history as prior messages.
                # The history is provided by the client via metadata.room_recent_history and
                # legacy parsing: extras["room_recent_messages"] used to be populated by the removed HTTP chat stack.
                recent_msgs: list[dict[str, Any]] = []
                if is_chat and isinstance(extras, dict):
                    rm = extras.get("room_recent_messages")
                    if isinstance(rm, list):
                        for it in rm:
                            if not isinstance(it, dict):
                                continue
                            role = str(it.get("role") or "").strip().lower()
                            if role not in ("user", "assistant"):
                                continue
                            content = str(it.get("content") or "").strip()
                            if not content:
                                continue
                            recent_msgs.append({"role": role, "content": content})
                # Prevent duplicate injection when room_recent_history already includes the current user message.
                if (
                    recent_msgs
                    and recent_msgs[-1].get("role") == "user"
                    and str(recent_msgs[-1].get("content") or "").strip() == str(invoke_user_text or "").strip()
                ):
                    state_msgs = list(recent_msgs)
                else:
                    state_msgs = [*recent_msgs, {"role": "user", "content": invoke_user_text}]
                state_input = {
                    "messages": state_msgs,
                    "pipeline_outputs": initial_pipeline_outputs,
                }

            result: Any = None
            strict_pipeline_violations: list[str] = []

            result = await _ainvoke_or_stream_values(
                orchestrator,
                state_input,
                stream_emit=stream_emit,
                http_stream_event_emit=http_stream_event_emit,
                plan_tools_by_name=plan_tools_by_name,
                run_config=run_config,
            )
            await _emit_planning_completed_if_ready(result)
            if not _planning_completed_flag():
                await _emit_planning_completed_fallback("에이전트 실행이 완료되었습니다.")
            # NOTE: chat/worker 실행 로직을 동일하게 유지하기 위해, HTTP 채팅만 재실행하는
            # follow-up(후속 실행) 메커니즘은 사용하지 않는다.

            logger.info(
                "오케스트레이터 실행 완료 | task_id=%s result_type=%s keys=%s",
                task_id,
                type(result).__name__,
                list(result.keys()) if isinstance(result, dict) else [],
            )
            try:
                _msgs = result.get("messages") if isinstance(result, dict) else None
                _audit = messages_to_audit_blob(_msgs)
                _po_run = result.get("pipeline_outputs") if isinstance(result, dict) else None
                for _w in validate_native_execution_contract(
                    pipeline_asset_roles=pipeline_asset_roles,
                    messages_blob=_audit,
                    pipeline_outputs=_po_run if isinstance(_po_run, dict) else None,
                ):
                    logger.warning(
                        "native_execution_contract | task_id=%s %s",
                        task_id,
                        _w,
                    )
                strict_pipeline_violations = strict_pipeline_state_violations(
                    pipeline_asset_roles=pipeline_asset_roles,
                    pipeline_outputs=_po_run if isinstance(_po_run, dict) else None,
                )
            except Exception:
                logger.exception("native execution contract audit failed | task_id=%s", task_id)
                strict_pipeline_violations = []
            # 스트리밍 경로에서 플래닝 완료 이벤트가 아직 안 나갔다면(플랜/투두가 끝까지 없던 케이스) 보정
            if not _planning_completed_flag():
                await _emit_planning_completed_if_ready(result)
            if not _planning_completed_flag():
                await _emit_planning_completed_fallback("플래닝 단계가 종료되었습니다.")
        except HitlInterrupt as hi:
            # A tool signalled that human input is required during worker execution.
            # Record a human_asked interrupt event and return waiting_for_human so the caller can pause.
            if not _planning_completed_flag():
                await _emit_planning_completed_fallback("플래닝이 완료되어 실행 중 사용자 입력이 필요해 중단되었습니다.")
            decision = hi.decision or {}
            pending_field = _resolve_pending_field_from_decision(decision)
            question = decision.get("question") or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            choices = decision.get("choices") or []
            hitl_agent_name = "작업 실행 기획자"
            interrupt_agent_path = list(exec_ctx.agent_path)
            for ctx in subagent_contexts:
                if ctx.get("job_id") == hi.job_id:
                    hitl_agent_name = ctx.get("name") or hitl_agent_name
                    sub_ctx = ctx.get("exec_ctx")
                    if sub_ctx is not None and hasattr(sub_ctx, "agent_path"):
                        interrupt_agent_path = list(sub_ctx.agent_path)
                    break
            if _is_duplicate_hitl_request(
                run_state,
                question=question,
                pending_field=pending_field,
            ):
                logger.info(
                    "Suppressing duplicate HITL question | task_id=%s field=%s",
                    task_id,
                    pending_field,
                )
                sandbox_output_paths_discard_task(task_id)
                rs_dup = dict(run_state) if isinstance(run_state, dict) else {}
                if checkpoint_thread_id:
                    rs_dup["checkpoint_thread_id"] = checkpoint_thread_id
                return {
                    "status": "waiting_for_human",
                    "run_state": rs_dup,
                }
            try:
                hitl_job_id = str(uuid.uuid4())
                enqueue_status_update(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": choices,
                        "reason": decision.get("reason"),
                        "checkpoint_thread_id": checkpoint_thread_id or None,
                        "tool_call_context": {
                            "job_id": hi.job_id,
                            "tool_name": hi.tool_name,
                        },
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
            except Exception:
                logger.exception("Failed to enqueue human_asked event from tool HITL", exc_info=True)

            sandbox_output_paths_discard_task(task_id)
            rs_hitl = {
                "reason": decision.get("reason") or "Tool requested human input.",
                "pending_field": pending_field,
                "correlation_id": exec_ctx.correlation_id,
                "agent_path": interrupt_agent_path,
                "tool_name": hi.tool_name,
                "last_question": question,
                "tool_call_context": {
                    "job_id": hi.job_id,
                    "tool_name": hi.tool_name,
                },
            }
            if checkpoint_thread_id:
                rs_hitl["checkpoint_thread_id"] = checkpoint_thread_id
            return {
                "status": "waiting_for_human",
                "run_state": rs_hitl,
            }
        except Exception:
            if not _planning_completed_flag():
                await _emit_planning_completed_fallback("플래닝 단계가 종료되었으나 실행 중 오류가 발생했습니다.")
            await _reconcile_partial_subagent_task_completed(
                event_queue=event_queue,
                proc_inst_id=proc_inst_id,
                task_id=task_id,
                subagent_contexts=subagent_contexts,
                subagent_tool_log=subagent_tool_log,
                subagent_skill_log=subagent_skill_log,
                subagent_event_state=subagent_event_state,
                root_exec_ctx=exec_ctx,
                from_orchestrator_error=True,
            )
            if not subagent_event_state.get("saw_task_call") and subagent_contexts:
                logger.info(
                    "서브에이전트 이벤트 fallback 발행(오류 경로) | task_id=%s subagents=%d",
                    task_id,
                    len(subagent_contexts),
                )
                await _emit_subagent_timeline_fallback(
                    event_queue=event_queue,
                    proc_inst_id=proc_inst_id,
                    task_id=task_id,
                    subagent_contexts=subagent_contexts,
                    subagent_tool_log=subagent_tool_log,
                    subagent_skill_log=subagent_skill_log,
                    root_exec_ctx=exec_ctx,
                    from_orchestrator_error=True,
                )
            logger.exception("Worker planner failed.", exc_info=True)
            sandbox_output_paths_discard_task(task_id)
            raise
        finally:
            skill_volume_active_exec_ctx.reset(tok_skill_ex)
            skill_volume_active_job_id.reset(tok_skill_job)
            skill_volume_tool_event_binding.reset(tok_skill_bind)

        # LangGraph DeepAgents 그래프 레벨에서 request_human_input 도구 호출로 인해
        # interrupt 가 발생한 경우(__interrupt__ 페이로드)를 처리한다.
        interrupt_payload = None
        try:
            interrupt_payload = extract_interrupt_payload(result)
        except Exception:
            logger.exception("Failed to extract graph-level interrupt payload", exc_info=True)

        if interrupt_payload:
            question = (
                interrupt_payload.get("question")
                if isinstance(interrupt_payload, dict)
                else None
            ) or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            openui_lang = (
                interrupt_payload.get("openui_lang")
                if isinstance(interrupt_payload, dict)
                else None
            )
            pending_field = "human_response"
            if _is_duplicate_hitl_request(
                run_state,
                question=question,
                pending_field=pending_field,
            ):
                logger.info(
                    "Suppressing duplicate graph-level HITL question | task_id=%s",
                    task_id,
                )
                sandbox_output_paths_discard_task(task_id)
                rs_dg = dict(run_state) if isinstance(run_state, dict) else {}
                if checkpoint_thread_id:
                    rs_dg["checkpoint_thread_id"] = checkpoint_thread_id
                return {
                    "status": "waiting_for_human",
                    "run_state": rs_dg,
                }
            hitl_agent_name = (
                (exec_ctx.agent_path[-1] if exec_ctx.agent_path else None)
                or "작업 실행 기획자"
            )
            try:
                hitl_job_id = str(uuid.uuid4())
                enqueue_status_update(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": [],
                        "interrupt_payload": interrupt_payload,
                        "checkpoint_thread_id": checkpoint_thread_id or None,
                    },
                    proc_inst_id,
                    task_id,
                    "deepagents",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
                try:
                    context = interrupt_payload.get("context")
                    interrupt_evt = new_artifact_update_event(
                        name="openui_request",
                        description="OpenUI 인터럽트 요청",
                        text=context,
                        proc_inst_id=proc_inst_id,
                        task_id=task_id,
                        exec_ctx=exec_ctx,
                    )
                    interrupt_evt.metadata.update({
                        "question": question,
                        **({"openuiLang": openui_lang} if openui_lang else {})
                    })
                    enqueue_event(event_queue, interrupt_evt)
                except Exception:
                    logger.exception("Failed to enqueue openui event for HITL", exc_info=True)

            except Exception:
                logger.exception(
                    "Failed to enqueue human_asked event from graph-level HITL", exc_info=True
                )

            sandbox_output_paths_discard_task(task_id)
            rs_graph = {
                "reason": "DeepAgents graph requested human input.",
                "pending_field": pending_field,
                "correlation_id": exec_ctx.correlation_id,
                "agent_path": list(exec_ctx.agent_path),
                "tool_name": "request_human_input",
                "last_question": question,
                "tool_call_context": {
                    "job_id": hitl_job_id,
                    "tool_name": "request_human_input",
                },
            }
            if isinstance(openui_lang, str) and openui_lang.strip():
                rs_graph["openui_lang"] = openui_lang.strip()
            if checkpoint_thread_id:
                rs_graph["checkpoint_thread_id"] = checkpoint_thread_id
            return {
                "status": "waiting_for_human",
                "run_state": rs_graph,
            }

        final_messages = result.get("messages", [])
        raw_text = ""
        if final_messages:
            # Some providers append tool/function messages at the end, or end with an
            # assistant tool-call message whose content is empty. For final display text,
            # prefer the last assistant message.
            last_assistant = _last_assistant_message(final_messages) or final_messages[-1]
            content = (
                last_assistant.get("content")
                if isinstance(last_assistant, dict)
                else getattr(last_assistant, "content", None)
            )
            raw_text = _extract_plain_text(content)
            if not (raw_text or "").strip():
                # Fallback: some runs end with ToolMessage/Command-like messages where the useful
                # text is not on the last assistant turn. Pick the last non-empty content from any message.
                for m in reversed(final_messages):
                    role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
                    if str(role or "").lower() in ("user", "human"):
                        continue
                    c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
                    t = _extract_plain_text(c)
                    if t.strip():
                        raw_text = t
                        break
        # gpt-oss 계열이 "tool call"을 텍스트 스캐폴딩으로 남기는 경우,
        # 이를 최종 답변으로 오인하면 사용자가 `<|start|>assistant...` 같은 쓰레기 패턴을 보게 된다.
        # 스트리밍 누적 텍스트/다른 메시지로 fallback 될 수 있게 여기서는 비워준다.
        if (raw_text or "").lstrip().startswith("<|") and "to=functions." in (raw_text or ""):
            raw_text = ""
        if not (raw_text or "").strip() and isinstance(result, dict):
            # Streaming path fallback: use the cumulative streamed assistant text captured in
            # `_ainvoke_or_stream_values` when the final state messages are not reliable.
            raw_text = str(result.get("_http_chat_streamed_text") or "")

        # 강제 위임 정책:
        # 서브에이전트가 구성되어 있는 런에서는 루트 오케스트레이터가 직접 최종 답변으로 끝내지 않는다.
        # (즉, 최소 1회 이상 task() 위임이 발생해야 한다.)
        delegation_policy_violation: str | None = None
        if subagent_contexts and not bool(planning_progress.get("saw_task_delegation")):
            delegation_policy_violation = (
                "서브에이전트가 구성되어 있으므로 루트 오케스트레이터가 직접 작업을 완료할 수 없습니다.\n"
                "반드시 다음 순서로 진행해야 합니다.\n\n"
                "1) (HTTP 채팅인 경우) `write_http_chat_route(route=\"needs_task\")`\n"
                "2) `write_todos` 또는 `write_pipeline_spec`로 계획 제출\n"
                "3) `task(...)`로 서브에이전트에 위임하여 실제 실행/조회 수행\n\n"
                "현재 실행에서는 `task()` 위임이 감지되지 않아 실패로 처리합니다."
            )

        # 미들웨어가 실제 task()를 감싸지 못한 런타임만 fallback으로 started/completed를 발행.
        # (fallback 내부에서 asyncio.sleep(1.2)로 최종 이벤트와 DB 시각 정렬을 분리한다.)
        if not subagent_event_state.get("saw_task_call"):
            logger.info(
                "서브에이전트 이벤트 fallback 발행 | task_id=%s subagents=%d",
                task_id,
                len(subagent_contexts),
            )
            await _emit_subagent_timeline_fallback(
                event_queue=event_queue,
                proc_inst_id=proc_inst_id,
                task_id=task_id,
                subagent_contexts=subagent_contexts,
                subagent_tool_log=subagent_tool_log,
                subagent_skill_log=subagent_skill_log,
                root_exec_ctx=exec_ctx,
                from_orchestrator_error=False,
            )

        await _reconcile_partial_subagent_task_completed(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            subagent_contexts=subagent_contexts,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            subagent_event_state=subagent_event_state,
            root_exec_ctx=exec_ctx,
            from_orchestrator_error=False,
        )

        form_fields = extras.get("form_fields")
        pure_form_data, wrapped_result, original_wo_form, _, _ = convert_agent_output(
            raw_text, form_id, form_fields
        )
        if pure_form_data is None:
            pure_form_data = {}
        async def _finalize_form_with_llm(
            *,
            model_name: str,
            form_id: str,
            form_fields: Any,
            evidence: dict[str, Any],
            model_routing: ModelRoutingConfig | None,
        ) -> dict[str, Any] | None:
            """Use an LLM-only finalizer to produce the exact form JSON deliverable.

            This is intentionally NOT heuristic matching: the LLM is responsible for mapping
            evidence into the required output form keys.
            """
            field_keys = _extract_form_field_keys(form_fields)
            if not field_keys:
                return None

            # Build a strict finalizer prompt: no tools, no subagents, no markdown.
            finalizer_prompt = (
                "You are a form output finalizer.\n"
                "You MUST return ONE valid JSON object and nothing else.\n"
                "Do NOT use markdown. Do NOT use code fences. Do NOT include explanations.\n\n"
                "You are given:\n"
                f"- form_id: {form_id}\n"
                f"- required field keys: {field_keys}\n\n"
                "Task:\n"
                "Populate ALL required keys with the correct values inferred from the provided EVIDENCE.\n"
                "If you cannot infer a value with high confidence, still fill it with the best available "
                "value from evidence; do not leave fields blank.\n"
                "Return ONLY the JSON object using exactly the required keys.\n"
            )

            # LLM-only runtime: no tools, no HITL tools, no subagents.
            finalizer = build_orchestrator(
                model_name=model_name,
                system_prompt=finalizer_prompt,
                subagents=[],
                tools=[],
                hitl_tools=[],
                middleware=[],
                skills=None,
                model_routing=model_routing,
            )
            msg = (
                "EVIDENCE (JSON):\n"
                + json.dumps(evidence, ensure_ascii=False)
                + "\n\n"
                "Return ONLY the final form JSON now."
            )
            out_state = await finalizer.ainvoke({"messages": [{"role": "user", "content": msg}]})
            # Extract the last message content
            text = ""
            if isinstance(out_state, dict):
                msgs = out_state.get("messages") or []
                if msgs:
                    last = msgs[-1]
                    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
                    text = _extract_plain_text(content)
            parsed = _loose_json_loads(text or "")
            return parsed if isinstance(parsed, dict) else None

        # 폼이 없거나 defaultform인 경우, 항상 JSON 파싱 가능한 고정 포맷으로 래핑
        has_form = bool(form_fields) and form_id not in (None, "", "defaultform")
        if not has_form:
            base_text = original_wo_form or wrapped_result.get("cleaned") or ""
            # 최종 텍스트는 "output 래퍼(JSON)" 형태로 올 수 있으므로, 먼저 래퍼를 벗겨낸다.
            # (OpenAI 계열은 `{ "output": { "status": ..., "result": "..." } }`만 반환하는 경우가 흔함)
            final_text = unwrap_output_wrapper_result_text(str(base_text)).strip()
            result_block = _extract_result_block(final_text)
            if result_block:
                final_text = result_block
            final_text = unwrap_output_wrapper_result_text(final_text).strip()

            # Deterministic rendering for factual "close price" queries (no extra LLM call).
            # If we can extract (date, close, symbol/name) from structured outputs, render a Korean sentence.
            try:
                facts0: dict[str, Any] | None = None
                parsed0 = _loose_json_loads(final_text) if (final_text or "").strip() else None
                if isinstance(parsed0, dict):
                    facts0 = _extract_close_facts_from_dict(parsed0)
                if facts0 is None and isinstance(structured_domain_outputs_by_job, dict):
                    for _job, bucket in structured_domain_outputs_by_job.items():
                        if not isinstance(bucket, dict):
                            continue
                        for _k, items in bucket.items():
                            if not isinstance(items, list):
                                continue
                            for it in reversed(items):
                                if not isinstance(it, dict):
                                    continue
                                ps = it.get("parsed_stdout")
                                if isinstance(ps, dict):
                                    facts0 = _extract_close_facts_from_dict(ps)
                                    if facts0:
                                        break
                            if facts0:
                                break
                        if facts0:
                            break
                if facts0:
                    sentence = _render_close_sentence_ko(
                        facts=facts0, user_text=str(invoke_user_text or "")
                    )
                    if sentence:
                        wrapped_result.setdefault("final_output_facts", facts0)
                        final_text = sentence
            except Exception:
                logger.exception("Failed to render deterministic close-price sentence; continuing", exc_info=True)

            result_status = "SUCCESS" if (final_text and not delegation_policy_violation) else "Fail"
            final_result_payload = {
                "output": {
                    "status": result_status,
                    "result": (
                        delegation_policy_violation
                        if (delegation_policy_violation and not is_chat)
                        else final_text
                    ),
                },
            }
        else:
            # For form outputs, the deliverable MUST be produced by an LLM, not heuristics.
            # If the orchestrator didn't emit a complete form JSON, run a tool-free finalizer LLM pass
            # using captured execution evidence (including structured subagent tool outputs).
            field_keys = _extract_form_field_keys(form_fields)
            if field_keys and isinstance(pure_form_data, dict):
                pure_form_data = _prefill_form_data_from_structured_outputs(
                    pure_form_data,
                    field_keys=field_keys,
                    structured_domain_outputs_by_job=structured_domain_outputs_by_job,
                )
            needs_finalize = False
            if not isinstance(pure_form_data, dict):
                needs_finalize = True
                pure_form_data = {}
            else:
                missing = [k for k in field_keys if pure_form_data.get(k) in (None, "", [])]
                if field_keys and missing:
                    needs_finalize = True

            if needs_finalize:
                evidence = {
                    "form_id": form_id,
                    "form_fields": form_fields,
                    "orchestrator_last_message": raw_text,
                    "subagent_tool_outputs": structured_domain_outputs_by_job,
                }
                finalized = await _finalize_form_with_llm(
                    model_name=model_name,
                    form_id=str(form_id),
                    form_fields=form_fields,
                    evidence=evidence,
                    model_routing=routing_cfg,
                )
                if isinstance(finalized, dict):
                    pure_form_data = finalized

            # Soft validation only: keep empty values but record which keys are missing.
            remaining_missing = [k for k in field_keys if pure_form_data.get(k) in (None, "", [])]
            if remaining_missing:
                pure_form_data = {k: (pure_form_data.get(k) or "") for k in field_keys}
                wrapped_result.setdefault("final_output_warnings", [])
                if isinstance(wrapped_result.get("final_output_warnings"), list):
                    wrapped_result["final_output_warnings"].append(
                        {
                            "type": "missing_form_fields",
                            "missing_keys": remaining_missing,
                        }
                    )
            # UI/consumer-friendly contract:
            # - Always use `result` key for display extraction
            # - Carry `form_id` as separate metadata for identification
            final_result_payload = {
                "form_id": str(form_id or ""),
                "result": pure_form_data,
            }

        # 이벤트 테이블: result 키에는 최종 도메인 결과만 저장한다.
        # - no-form: 평문 문자열
        # - form: {"form_id": "...", "result": { 필드키: 값, ... } }
        if not has_form:
            final_event_result = final_text
        else:
            _wrapped = _build_form_event_result(
                form_id=form_id,
                form_fields=form_fields,
                pure_form_data=pure_form_data,
                fallback_text=(original_wo_form or wrapped_result.get("cleaned") or ""),
            )
            fid = str(form_id or "").strip()
            inner = _wrapped.get(fid) if (fid and isinstance(_wrapped, dict)) else _wrapped
            final_event_result = {
                "form_id": fid,
                "result": inner if isinstance(inner, dict) else {},
            }

        tracked_paths = take_sandbox_output_paths_task(task_id)
        # IMPORTANT: Restrict artifact scanning to sandbox workspaces used by this task.
        # Previously we scanned *all* still-open workspaces, which could leak unrelated
        # files (e.g. PDFs from prior requests) into the current chat response.
        # NOTE: For chat we must not treat downloaded *input* files as deliverables.
        # We therefore disable broad workspace glob scanning and only rely on explicitly
        # tracked sandbox outputs (note_sandbox_tool_output_paths) and explicit upload tool outputs.
        scanned_paths: list[str] = []
        if not is_chat:
            scanned_paths = collect_task_open_workspace_paths_for_globs(str(task_id), default_artifact_globs())
        loaded_files = collect_readable_artifacts(tracked_paths, extra_paths=scanned_paths)
        file_manifest = enqueue_sandbox_file_artifacts(
            event_queue,
            proc_inst_id or "",
            str(task_id),
            loaded_files,
            tenant_id=str(tenant_id).strip() if tenant_id else None,
        )
        # Merge artifacts discovered by scanner with explicit upload tool outputs.
        combined_manifest: list[dict[str, Any]] = []
        if file_manifest:
            combined_manifest.extend(file_manifest)
        if deliverable_files_from_tools:
            combined_manifest.extend(deliverable_files_from_tools)

        if combined_manifest:
            wrapped_result["deliverable_files"] = combined_manifest
            if has_form and isinstance(pure_form_data, dict):
                pure_form_data = sanitize_deliverable_form_fields(
                    pure_form_data,
                    file_manifest=combined_manifest,
                )
            link_entries = [
                {"name": f.get("name"), "public_url": f.get("public_url")}
                for f in combined_manifest
                if isinstance(f.get("public_url"), str) and str(f["public_url"]).strip()
            ]
            if not has_form:
                # Do not automatically append download links to the user-facing text.
                # Links (when present) remain available in structured payloads / deliverable manifests.
                pass
            elif link_entries and (not is_chat):
                final_result_payload = dict(final_result_payload)
                final_result_payload["file_download_links"] = link_entries
                if isinstance(final_event_result, dict):
                    final_event_result = dict(final_event_result)
                    final_event_result["file_download_links"] = link_entries

        execution_evidence = _build_execution_evidence(
            raw_text=raw_text,
            wrapped_result=wrapped_result,
            subagent_contexts=subagent_contexts,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            structured_domain_outputs_by_job=structured_domain_outputs_by_job,
            deliverable_files=combined_manifest or None,
        )
        # 2-layer output:
        # - final_output: user-facing final deliverable
        # - execution_evidence: internal/verifiable execution details
        wrapped_result["final_output"] = final_result_payload
        wrapped_result["execution_evidence"] = execution_evidence
        # Count scanner-found artifacts AND upload-tool deliverables (e.g. sandbox_upload_file_to_public_storage).
        # Previously only `file_manifest` was checked, so runs that produced public_url solely via the upload
        # hook (`deliverable_files_from_tools`) were wrongly treated as missing evidence.
        orchestrator_used_tools = False
        try:
            orchestrator_used_tools = bool(orchestrator_tool_log.get(str(job_uuid), []))
        except Exception:
            orchestrator_used_tools = False

        has_execution_evidence = orchestrator_used_tools or any(
            bool((run.get("tools_used") or []))
            for run in (execution_evidence.get("subagent_runs") or [])
        ) or bool(combined_manifest)
        if not has_execution_evidence:
            evidence_error = (
                "실행 증거(도구 호출/위임 결과)가 부족하여 결과를 확정할 수 없습니다. "
                "반드시 최소 1회 이상 MCP 도구 호출 또는 `task()` 위임을 수행한 뒤 결과를 종합해 주세요."
            )
            final_result_payload = {
                "output": {
                    "status": "Fail",
                    "result": (evidence_error if not is_chat else final_text),
                }
            }
            wrapped_result["final_output"] = final_result_payload
            if not has_form:
                final_event_result = final_result_payload["output"]["result"]
            else:
                final_event_result = {
                    "status": "Fail",
                    "reason": "missing_execution_evidence",
                    "result": (evidence_error if not is_chat else final_event_result),
                }

        _inv_mode = (os.environ.get("DEEPAGENTS_PIPELINE_INVARIANTS") or "log").strip().lower()
        if strict_pipeline_violations:
            _plog = logger.error if _inv_mode == "fail" else logger.warning
            for _pv in strict_pipeline_violations:
                _plog("pipeline_state_invariant | task_id=%s %s", task_id, _pv)
            if _inv_mode == "fail":
                vtext = "\n".join(strict_pipeline_violations)
                fail_msg = (
                    "파이프라인 상태 불변식이 충족되지 않아 결과를 성공으로 확정할 수 없습니다.\n" + vtext
                )
                if not has_form:
                    final_result_payload = {
                        "output": {
                            "status": "Fail",
                            "result": fail_msg,
                        }
                    }
                    wrapped_result["final_output"] = final_result_payload
                    final_event_result = final_result_payload["output"]["result"]
                else:
                    wrapped_result.setdefault("final_output_warnings", [])
                    if isinstance(wrapped_result.get("final_output_warnings"), list):
                        wrapped_result["final_output_warnings"].append(
                            {
                                "type": "pipeline_invariant_violation",
                                "violations": list(strict_pipeline_violations),
                            }
                        )
                    _pf = pure_form_data if isinstance(pure_form_data, dict) else {}
                    final_event_result = {
                        "status": "Fail",
                        "reason": "pipeline_invariant_violation",
                        "violations": list(strict_pipeline_violations),
                        "result": _pf,
                    }
                    final_result_payload = dict(final_result_payload)
                    final_result_payload["pipeline_invariant_violations"] = list(strict_pipeline_violations)
                    wrapped_result["final_output"] = final_result_payload

        end_job = str(uuid.uuid4())
        enqueue_status_update(
            event_queue,
            "task_started",
            {
                "message": "최종 결과 생성을 시작했습니다.",
            },
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="final",
            agent_name="최종 결과 작성자",
        )
        enqueue_status_update(
            event_queue,
            "task_completed",
            # NOTE: For crew_type='result' we want the event payload itself to be the deliverable.
            # - no-form: keep wrapper with message + result string
            # - form: {"form_id": "...", "result": {...}} for UI extraction
            (
                final_event_result
                if has_form
                else {
                    "message": (final_event_result or "").strip() or "최종 결과 생성이 완료되었습니다.",
                    "result": final_event_result,
                }
            ),
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="final",
            agent_name="최종 결과 작성자",
        )

        # Persisted artifact payload policy (todolist.output / todolist.draft):
        # - DB RPC save_task_result stores p_payload into:
        #   - output when agent_mode='COMPLETE' and p_final=true
        #   - draft otherwise
        # Contract requirements:
        # - output MUST be: { "<form_id>": { "<field_key>": <value>, ... } }  (no other top-level keys)
        # - draft should also include that mapping, but may contain extra metadata keys.
        agent_mode = str(row.get("agent_mode") or "").strip().upper()
        artifact_payload: Any = wrapped_result
        if has_form:
            fid = str(form_id or "").strip()
            # Ensure we always write a JSON object for form payloads.
            form_payload = pure_form_data if isinstance(pure_form_data, dict) else {}
            base_form_mapping: dict[str, Any] = {fid: form_payload} if fid else {"": form_payload}
            if agent_mode == "COMPLETE":
                artifact_payload = base_form_mapping
            else:
                artifact_payload = {
                    **base_form_mapping,
                    "_meta": wrapped_result,
                }

        # SDK chat(SSE) contract: final response should be a terminal artifact with the plain text.
        # The SDK ChatEventQueue uses this artifact text as the `done` content.
        if is_chat:
            artifact_payload = (final_text or "").strip() or unwrap_output_wrapper_result_text(
                str(raw_text or "")
            ).strip()
            # 보안/UX: 서브에이전트 구성 위반 등의 기술적 에러 메시지가 채팅 본문에 절대 노출되지 않도록 강제 필터링
            forbidden_start = "서브에이전트가 구성되어 있으므로"
            if artifact_payload.startswith(forbidden_start):
                artifact_payload = (
                    "작업 수행을 위해 필요한 서브에이전트 위임이 발생하지 않았습니다. "
                    "오케스트레이터의 실행 계획을 확인해 주세요."
                )

        enqueue_artifact_update(
            event_queue,
            name="deepagents_action_result",
            description="DeepAgents 멀티 에이전트 실행 결과",
            # wrapped_result는 dict/list 형태의 JSON-직렬화 가능한 객체여야 하며,
            # 그대로 events.data 및 todolist.draft(jsonb)에 저장된다.
            text=artifact_payload,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=exec_ctx,
        )

        logger.info("DeepAgents 멀티 에이전트 실행 완료")
        result_box = extras.get("_http_chat_result_box")
        if is_chat and isinstance(result_box, dict):
            if has_form:
                chat_body = (
                    json.dumps(pure_form_data, ensure_ascii=False)
                    if isinstance(pure_form_data, dict)
                    else ""
                )
            else:
                chat_body = (final_text or "").strip()
            # Guardrail (HTTP chat + attachments):
            # If we detected input assets but the run produced zero tool usage,
            # any "summary" is almost certainly hallucinated (file was never downloaded/read).
            # In this case, fail fast with a message that acknowledges the received file metadata
            # instead of returning fabricated numbers or asking the user to re-upload.
            try:
                has_assets = bool(asset_inventory and isinstance(asset_inventory, dict) and asset_inventory.get("total_count"))
                any_tool_usage = bool(orchestrator_tool_log) or any(bool(v) for v in (subagent_tool_log or {}).values())
                if (not has_form) and has_assets and (not any_tool_usage):
                    assets0 = asset_inventory.get("assets") if isinstance(asset_inventory, dict) else None
                    # Keep this short to avoid token blowups; include only the first few paths.
                    paths: list[str] = []
                    if isinstance(assets0, list):
                        for a in assets0[:5]:
                            if isinstance(a, dict):
                                p = a.get("path")
                                if isinstance(p, str) and p.strip():
                                    paths.append(p.strip())
                    received_hint = "\n".join(f"- {p}" for p in paths) if paths else "(경로 정보 없음)"
                    chat_body = (
                        "첨부파일 메타데이터는 전달되었지만, 이번 실행에서 파일을 다운로드/읽는 도구 실행이 0회라서 "
                        "내용 기반 요약을 생성할 수 없습니다.\n\n"
                        "수신한 파일(일부):\n"
                        f"{received_hint}\n\n"
                        "다시 시도할 때는 `write_http_chat_route(route=\"needs_task\")` → `write_todos` → `task(...)`로 "
                        "첨부파일을 실제로 읽은 뒤, 그 근거로 요약을 작성해야 합니다."
                    )
            except Exception:
                pass
            if not has_form and not (chat_body or "").strip():
                # Prefer the actual streamed assistant text over a generic fallback message.
                # This preserves partial answers even when the run fails to produce a final_text.
                streamed = ""
                try:
                    streamed = str(result.get("_http_chat_streamed_text") or "").strip() if isinstance(result, dict) else ""
                except Exception:
                    streamed = ""
                raw_fallback = (raw_text or "").strip()
                candidate = unwrap_output_wrapper_result_text(streamed or raw_fallback).strip()
                chat_body = candidate or "요청 작업을 수행했지만 최종 답변을 생성하지 못했습니다."
            result_box["response"] = ensure_trailing_newline(chat_body)
            result_box["tool_calls"] = _http_chat_merge_tool_calls_summary(
                orchestrator_tool_log,
                subagent_tool_log,
                orchestrator_job_id=job_uuid,
            )
            result_box["skill_calls"] = _http_chat_skill_calls_summary(
                subagent_skill_log,
                orchestrator_job_id=job_uuid,
            )
            result_box["final_output"] = final_result_payload
            # (의도적으로) 요청 분류/케이스 분기 스냅샷은 남기지 않는다.
            # 정책은 도구 호출 게이트로만 강제한다.
        reset_executor_asyncio_loop_for_enqueue(tok)
        return None

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("작업 취소 요청됨")
