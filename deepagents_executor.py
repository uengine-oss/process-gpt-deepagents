"""DeepAgents executor: ProcessGPT A2A 래퍼 + DeepAgents orchestrator."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from typing_extensions import override
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue

from app.database import get_latest_human_response
from app.agent.backend import tenant_skill_sources_for_middleware
from app.agent.factory import (
    build_orchestrator,
    build_orchestrator_system_prompt,
    default_orchestrator_prompt,
)
from app.agent.hitl import extract_interrupt_payload, get_hitl_tools
from app.output_utils import convert_agent_output
from app.sandbox.output_delivery import (
    collect_readable_artifacts,
    default_artifact_globs,
    enqueue_sandbox_file_artifacts,
    sandbox_output_paths_begin_task,
    sandbox_output_paths_discard_task,
    take_sandbox_output_paths_task,
)
from app.sandbox.tools import collect_open_workspace_paths_for_globs
from app.runtime_config import resolve_model_routing
from app.skills.discovery_subagent import (
    SKILL_DISCOVERY_SUBAGENT_NAME,
    build_skill_discovery_subagent,
    synthetic_skill_discovery_agent_row,
)
from app.skills.volume_subagent_scope_middleware import SubagentJobScopeMiddleware
from app.skills.volume_tool_events import (
    SkillVolumeToolEventBinding,
    skill_volume_active_exec_ctx,
    skill_volume_active_job_id,
    skill_volume_tool_event_binding,
)
from app.agent.subagents import (
    build_subagent_configs,
    hinted_skill_slugs_from_query,
    parse_skills_csv,
)
from app.tooling.tagging_safe_tool_loader import TaggingSafeToolLoader
from processgpt_agent_utils.utils.context_manager import set_context

from app.executor.a2a_events import (
    _enqueue_artifact,
    _enqueue_status,
    _ensure_required_root_task_started_fields,
)
from app.executor.context import ExecutionContext, build_event_payload
from app.executor.evidence import _build_execution_evidence
from app.executor.exceptions import HitlInterrupt, MissingSubagentsConfigurationError
from app.executor.hitl_utils import (
    _classify_tenant_context_tool,
    _is_duplicate_hitl_request,
    _make_request_human_input_context_guard,
    _required_context_categories_from_tools,
    _resolve_pending_field_from_decision,
    _should_defer_hitl_interrupt,
)
from app.executor.langchain_tools import _ensure_langchain_tools
from app.executor.planning_middleware import _create_write_todos_planning_middleware
from app.executor.subagent_middleware import (
    _create_subagent_event_middleware,
    _emit_subagent_timeline_fallback,
    _reconcile_partial_subagent_task_completed,
)
from app.executor.tenant_context import (
    _mcp_config_without_blocked_servers,
    _prefetch_tenant_context_once,
    _truncate_text,
)
from app.executor.tool_instrumentation import (
    _coerce_tool_output_for_tool_node,
    _instrument_tools,
)
from app.executor.workitem_text import (
    _build_form_event_result,
    _coerce_tool_input_dict,
    _extract_form_field_keys,
    _extract_plain_text,
    _extract_plan_block,
    _extract_result_block,
    _is_korean_user_input,
    _join_todo_contents,
    _strip_plan_blocks,
    parse_workitem_query,
)

logger = logging.getLogger(__name__)

class DeepAgentsExecutor(AgentExecutor):
    """Runs todolist query via create_deep_agent(subagents); emits A2A Task*Events."""

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("DeepAgents 멀티 에이전트 실행 시작")

        query = context.get_user_input()
        context_data = context.get_context_data() or {}
        row = context_data.get("row", {})
        extras = context_data.get("extras", {})

        proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id") or ""
        task_id = str(row.get("id") or "")
        tenant_id = row.get("tenant_id") or ""
        form_id = extras.get("form_id")

        logger.info("task_id=%s proc_inst_id=%s form_id=%s", task_id, proc_inst_id, form_id)
        sandbox_output_paths_begin_task(task_id)

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

        # Parse work-item query into structured parts for downstream logic / HITL.
        parsed_query = parse_workitem_query(query or "")
        description = parsed_query.get("description") or ""
        instruction = parsed_query.get("instruction") or ""
        input_data = parsed_query.get("input_data") or {}

        # Resume: if we have run_state and human_response, merge answer into input_data.
        if isinstance(human_response_answer, str) and human_response_answer.strip():
            pending_field = run_state.get("pending_field") or "human_response"
            input_data = dict(input_data)
            input_data[pending_field] = human_response_answer.strip()
            parsed_query["input_data"] = input_data
            hitl_resume = {
                "field": str(pending_field),
                "answer": human_response_answer.strip(),
            }

            # Emit a resume event for tracing via SDK EventQueue.
            try:
                _enqueue_status(
                    event_queue,
                    "task_working",
                    {
                        "resume_source": "human_response",
                        "human_answer": human_response_answer.strip(),
                        "previous_interrupt_agent_path": run_state.get("agent_path"),
                        "previous_interrupt_tool_name": run_state.get("tool_name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="resume",
                    phase="resume",
                    agent_name=None,
                )
            except Exception:
                logger.exception("Failed to enqueue resume event", exc_info=True)

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
            crew_type="action",
            users_email=extras.get("notify_user_emails", []),
        )

        domain_agent_rows = extras.get("agents", []) or []
        all_tools: list[Any] = []
        # SafeToolLoader local tools(mem0/dmn_rule)는 user_id가 필요하다.
        # 이 시스템에서는 에이전트의 id가 곧 user_id이므로 agents[].id를 최우선 사용한다.
        first_agent_user_id = ""
        if isinstance(domain_agent_rows, list):
            for ag in domain_agent_rows:
                if isinstance(ag, dict):
                    aid = str(ag.get("id") or "").strip()
                    if aid:
                        first_agent_user_id = aid
                        break
        raw_user_id = first_agent_user_id or extras.get("user_id") or extras.get("request_user_id") or row.get("user_id") or ""
        # todolist.user_id can be CSV in some flows; mem0/dmn_rule expect a single user id.
        tool_user_id = str(raw_user_id).split(",")[0].strip() if raw_user_id else ""
        mcp_cfg_raw = extras.get("tenant_mcp")
        mcp_cfg: dict[str, Any] = _mcp_config_without_blocked_servers(mcp_cfg_raw)
        mcp_servers = mcp_cfg.get("mcpServers") or {}
        # mem0 / memento / dmn_rule 은 SafeToolLoader 로컬 도구(언더스코어 이름). MCP 서버가 아니다.
        # 하이픈 키(dmn-rule 등)로 넘기면 MCP 조회만 시도하고 실패하므로 여기서는 요청하지 않는다.
        local_tenant_context_tool_names = ("mem0", "memento", "dmn_rule")
        # 테넌트 MCP 설정에서 해석할 **실제 MCP** 서버(예: claude-skills).
        desired_mcp_servers = ("claude-skills",)
        resolved_server_keys: dict[str, list[str]] = {name: [] for name in desired_mcp_servers}
        if isinstance(mcp_servers, dict):
            for key in mcp_servers:
                raw = str(key).strip()
                normalized = raw.lower().replace("_", "-")
                for canonical in desired_mcp_servers:
                    if normalized == canonical or normalized.endswith(canonical):
                        resolved_server_keys[canonical].append(raw)

            # SafeToolLoader 는 _get_mcp_config 에서 dict key exact match를 사용하므로,
            # alias 키만 있는 경우에도 표준 키로 조회되도록 mcp_config에 canonical alias를 주입한다.
            for canonical, found_keys in resolved_server_keys.items():
                if found_keys and canonical not in mcp_servers:
                    primary_key = found_keys[0]
                    cfg = mcp_servers.get(primary_key)
                    if isinstance(cfg, dict):
                        mcp_servers[canonical] = dict(cfg)

        # 로컬 컨텍스트 도구를 먼저 요청한 뒤, MCP(claude-skills 등)를 이어서 요청한다.
        mcp_request: list[str] = []
        for name in local_tenant_context_tool_names:
            if name not in mcp_request:
                mcp_request.append(name)
        for canonical in desired_mcp_servers:
            for candidate in [*resolved_server_keys.get(canonical, []), canonical]:
                if candidate and candidate not in mcp_request:
                    mcp_request.append(candidate)
        logger.info(
            "테넌트 연결 도구 로드 준비 | task_id=%s tenant_id=%s mcp_servers=%s request=%s",
            task_id,
            tenant_id or "(empty)",
            list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else [],
            mcp_request,
        )
        try:
            # tenant_mcp 병합 설정 + claude-skills 명시 요청으로 Tier 2 MCP 도구 로드.
            # TaggingSafeToolLoader 가 각 MCP 툴에 _mcp_server 를 붙여 서브 에이전트 baseline 라우팅을 가능하게 함.
            tool_loader = TaggingSafeToolLoader(
                tenant_id=tenant_id or None,
                user_id=tool_user_id or None,
                agent_name="작업 실행 기획자",
                mcp_config=mcp_cfg,
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

        context_tool_tracker: dict[str, Any] = {
            "required_global": _required_context_categories_from_tools(all_tools),
            "attempted_global": set(),
        }
        if context_tool_tracker["required_global"]:
            logger.info(
                "HITL 선행 조건: 다음 테넌트 지식 도구 카테고리는 최소 1회 호출 후 human 개입 허용 | task_id=%s required=%s",
                task_id,
                sorted(context_tool_tracker["required_global"]),
            )

        # Preflight: attempt tenant context tools ONCE (domain-agnostic).
        # This reduces avoidable failures caused by skipping "basic" tools, while
        # keeping cost bounded (1 call per category).
        try:
            preflight = await _prefetch_tenant_context_once(
                tools=all_tools,
                description=description,
                instruction=instruction,
                input_data=input_data if isinstance(input_data, dict) else {},
                tenant_id=str(tenant_id or ""),
                context_tool_tracker=context_tool_tracker,
            )
        except Exception:
            logger.exception("tenant context preflight failed; continuing", exc_info=True)
            preflight = {}
        if preflight:
            try:
                # Keep it compact; the model can choose to re-query tools if needed.
                mem0_txt = _truncate_text((preflight.get("mem0") or {}).get("output") or "", max_chars=1600)
                memento_txt = _truncate_text((preflight.get("memento") or {}).get("output") or "", max_chars=1600)
                dmn_txt = _truncate_text((preflight.get("dmn_rule") or {}).get("output") or "", max_chars=1600)
                injected = (
                    "\n\n=== Preflight tenant context (basic tools) ===\n"
                    "Below is optional context retrieved automatically BEFORE execution. Use it to avoid avoidable failures.\n"
                    f"[mem0]\n{mem0_txt}\n\n"
                    f"[memento]\n{memento_txt}\n\n"
                    f"[dmn_rule]\n{dmn_txt}\n"
                )
                invoke_user_text = (invoke_user_text or "").rstrip() + injected
            except Exception:
                logger.exception("Failed to inject preflight context into user text; continuing", exc_info=True)

        # 서브 에이전트별 도구 호출은 각 서브 에이전트 전용 job_id로 분리된다.
        subagent_tool_log: dict[str, list[str]] = {}
        subagent_skill_log: dict[str, list[str]] = {}
        # Structured skill discovery signals captured from tool results (no text parsing).
        structured_skill_handoff_by_job: dict[str, dict[str, Any]] = {}
        # Structured domain execution outputs captured from tool results (e.g. sandbox stdout JSON).
        # Used to backfill output forms when the orchestrator forgets to emit JSON.
        structured_domain_outputs_by_job: dict[str, dict[str, Any]] = {}

        allowed_skill_names: list[str] = []
        allowed_skill_names_by_agent: dict[str, list[str]] = {}
        seen_skill_names: set[str] = set()
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
        logger.info(
            "서브에이전트 구성 시작 | task_id=%s domain_agent_rows=%d allowed_skill_names=%d",
            task_id,
            len(domain_agent_rows),
            len(allowed_skill_names),
        )

        hinted_skill_slugs = hinted_skill_slugs_from_query(
            invoke_user_text,
            allowed_skill_names=allowed_skill_names,
        )
        find_helpful_allowlist = list(
            dict.fromkeys([*allowed_skill_names, *hinted_skill_slugs])
        )
        logger.info(
            "스킬 탐색 힌트 | task_id=%s hinted_slugs=%s find_helpful_allowlist_len=%d",
            task_id,
            hinted_skill_slugs,
            len(find_helpful_allowlist),
        )

        subagents_domain = build_subagent_configs(
            domain_agent_rows,
            all_tools=all_tools,
            query=invoke_user_text,
            tenant_id=tenant_id or None,
        )
        skill_discovery_sa = build_skill_discovery_subagent(
            all_tools=all_tools,
            query=invoke_user_text,
            tenant_id=tenant_id or None,
            context_tools_user_id=tool_user_id or None,
            allowed_skill_names=allowed_skill_names,
            allowed_skill_names_by_agent=allowed_skill_names_by_agent,
            hinted_skill_slugs=hinted_skill_slugs,
        )
        subagents = [skill_discovery_sa] + subagents_domain
        agent_rows = [synthetic_skill_discovery_agent_row()] + list(domain_agent_rows)
        logger.info(
            "서브에이전트 구성 결과 | task_id=%s built_subagents=%d (includes skill discovery)",
            task_id,
            len(subagents),
        )
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
        if not domain_agent_rows or not subagents_domain:
            logger.error(
                "Subagents are required: domain_agent_rows=%d subagents_domain=%d task_id=%s",
                len(domain_agent_rows),
                len(subagents_domain),
                task_id,
            )
            raise MissingSubagentsConfigurationError()

        # 동일 런에서 스킬 탐색(0번) job_id를 고정해, 탐색 단계 관측을 실행 단계 가드와 공유한다.
        subagent_job_ids = [str(uuid.uuid4()) for _ in agent_rows]
        discovery_job_id = subagent_job_ids[0] if subagent_job_ids else ""

        def _discovery_obs_read_skill_docs_ok(handoff: dict[str, Any] | None) -> bool:
            """스킬 탐색 도우미 job의 tool_result_hook 기록으로 문서 조회가 있었는지."""
            ent = structured_skill_handoff_by_job.get(discovery_job_id) or {}
            obs = ent.get("_observations") if isinstance(ent.get("_observations"), dict) else {}
            rsd = obs.get("read_skill_document")
            if not isinstance(rsd, dict) or not rsd:
                return False
            primary = ""
            if isinstance(handoff, dict) and handoff:
                primary = str(handoff.get("selected_primary") or "").strip()
                if not primary:
                    ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
                    if ranked and isinstance(ranked[0], dict):
                        primary = str(ranked[0].get("skill_name") or "").strip()
            if primary:
                docs = rsd.get(primary)
                return isinstance(docs, list) and len(docs) > 0
            for _sk, docs in rsd.items():
                if isinstance(docs, list) and len(docs) > 0:
                    return True
            return False

        def _discovery_obs_tier1_probe_ok() -> bool:
            ent = structured_skill_handoff_by_job.get(discovery_job_id) or {}
            obs = ent.get("_observations") if isinstance(ent.get("_observations"), dict) else {}
            return bool(obs.get("skill_volume_probe"))

        subagent_contexts: list[dict[str, Any]] = []
        # 서브 에이전트별 job_id와 메타데이터를 준비하고,
        # 각 서브 에이전트의 MCP 도구를 해당 job_id로 인스트루먼트한다.
        for idx, row in enumerate(agent_rows):
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

            sub_job_id = subagent_job_ids[idx]
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
                if agent_name == SKILL_DISCOVERY_SUBAGENT_NAME:
                    # Captured per-run allowlist (crew skills + query slugs) for find_helpful_skills.
                    _find_allowed = list(find_helpful_allowlist)

                    skill_discovery_tier_state: dict[str, bool] = {"tier2_attempted": False}
                    skill_discovery_has_tier2 = any(
                        "claude-skills" in str(getattr(st, "_mcp_server", "") or "").lower()
                        for st in sub_tools
                    )

                    def _skill_discovery_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        if tool_name not in ("find_helpful_skills", "read_skill_document"):
                            return tool_input
                        merged = _coerce_tool_input_dict(tool_input)
                        if merged is None:
                            return tool_input
                        if tool_name == "find_helpful_skills":
                            # LangChain tool calls are usually shaped like:
                            # {"name": "...", "args": {...}, "id": "...", "type": "tool_call"}
                            # Ensure allowlist + tenant_id are passed inside "args" so MCP tools receive them.
                            args = merged.get("args")
                            if not isinstance(args, dict):
                                args = {}
                                merged["args"] = args

                            # Back-compat: if previous code injected at top-level, migrate it into args.
                            top_allowed = merged.get("allowed_skill_names")
                            if isinstance(top_allowed, list) and "allowed_skill_names" not in args:
                                args["allowed_skill_names"] = list(top_allowed)
                                merged.pop("allowed_skill_names", None)
                            top_tenant = merged.get("tenant_id")
                            if isinstance(top_tenant, str) and top_tenant.strip() and "tenant_id" not in args:
                                args["tenant_id"] = top_tenant
                                merged.pop("tenant_id", None)

                            if _find_allowed and "allowed_skill_names" not in args:
                                args["allowed_skill_names"] = list(_find_allowed)
                            if tenant_id and "tenant_id" not in args:
                                args["tenant_id"] = str(tenant_id)
                            return merged
                        # read_skill_document: forcing SKILL.md often fails for tenant skills.
                        # Convert to listing mode first so the subagent can choose an actual file.
                        doc_path = str(merged.get("document_path") or "").strip()
                        if doc_path in ("SKILL.md", "/SKILL.md"):
                            merged["document_path"] = None
                        return merged

                    def _skill_discovery_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        server = str(getattr(base_tool, "_mcp_server", "") or "").lower()
                        if "claude-skills" in server or tool_name in (
                            "list_skills",
                            "read_skill_document",
                            "find_helpful_skills",
                        ):
                            skill_discovery_tier_state["tier2_attempted"] = True
                            return None
                        # Tier 1 (filesystem via mounted skill volume) is allowed before Tier 2:
                        # sandbox_create + sandbox_copy_mounted_skill + sandbox_read_workspace_files are
                        # considered "materialization / inspection", not arbitrary execution.
                        if tool_name in (
                            "sandbox_create",
                            "sandbox_copy_mounted_skill",
                            "sandbox_read_workspace_files",
                            "sandbox_read_artifacts",
                        ):
                            return None
                        if tool_name.startswith("sandbox_") and skill_discovery_has_tier2:
                            if not skill_discovery_tier_state.get("tier2_attempted"):
                                return {
                                    "error": "tier2_required_before_tier3",
                                    "message": "Run claude-skills MCP discovery tools before sandbox execution.",
                                    "requires_human": False,
                                }
                        return None
                    tool_input_augmenter = _skill_discovery_input_augmenter
                    tool_call_guard = _skill_discovery_tool_call_guard
                    def _skill_discovery_tool_result_hook(
                        *,
                        base_tool: Any,
                        job_id: str,
                        tool_input: Any,
                        tool_output: Any,
                        error: Exception | None,
                    ) -> None:
                        if error is not None:
                            return
                        tname = str(getattr(base_tool, "name", "") or "")
                        raw_in = tool_input if isinstance(tool_input, dict) else _coerce_tool_input_dict(tool_input)
                        raw_in = raw_in if isinstance(raw_in, dict) else {}
                        text_out = _extract_plain_text(tool_output)
                        entry = structured_skill_handoff_by_job.setdefault(
                            job_id,
                            {
                                "version": 1,
                                "tenant_id": tenant_id or "",
                                "allowed_skill_names": list(find_helpful_allowlist),
                                "selected_primary": (find_helpful_allowlist[0] if find_helpful_allowlist else ""),
                                "ranked": [],
                                "_observations": {},
                                "required_env": {},
                            },
                        )
                        obs = entry.setdefault("_observations", {})
                        if tname == "skill_volume_probe":
                            obs["skill_volume_probe"] = text_out
                        elif tname == "find_helpful_skills":
                            obs["find_helpful_skills"] = text_out
                        elif tname == "read_skill_document":
                            sk = ""
                            args = raw_in.get("args") if isinstance(raw_in.get("args"), dict) else raw_in
                            if isinstance(args, dict):
                                sk = str(args.get("skill_name") or "").strip()
                                docp = args.get("document_path")
                            else:
                                docp = None
                            if sk:
                                docs = obs.setdefault("read_skill_document", {}).setdefault(sk, [])
                                docs.append({"document_path": docp, "text": text_out[:20000]})
                                # Opportunistically extract env var names from the doc text.
                                try:
                                    env_names = set()
                                    # Common patterns: $VAR, ${VAR}, os.environ["VAR"], export VAR=...
                                    for m in re.finditer(r"(?:\\$\\{?|os\\.environ\\[\"|os\\.environ\\['|export\\s+)([A-Z][A-Z0-9_]{2,})", text_out):
                                        env_names.add(m.group(1))
                                    if env_names:
                                        req = entry.setdefault("required_env", {})
                                        for nm in sorted(env_names):
                                            req.setdefault(nm, {"type": "text", "source": "doc"})
                                except Exception:
                                    pass
                        # Keep a minimal ranked list even if the model forgets to print handoff JSON.
                        if not entry.get("ranked"):
                            primary = str(entry.get("selected_primary") or "").strip()
                            if primary:
                                entry["ranked"] = [
                                    {
                                        "skill_name": primary,
                                        "evidence": {"source": "mcp", "notes": "Captured from tool results"},
                                        "documents": [],
                                        "endpoints": [],
                                        "sandbox_materialization": {"required": True, "suggested_calls": []},
                                    }
                                ]
                else:
                    # Domain/DB subagents: prefer centralized skill discovery, but allow fallback discovery
                    # only after Tier 1 probe is attempted (volume/FS) to avoid redundant MCP calls.
                    domain_tier_state: dict[str, bool] = {
                        "tier1_attempted": False,
                        "mounted_skill_copied": False,
                        "skill_docs_read": False,
                    }
                    domain_has_memento_tool = any(
                        str(getattr(t, "name", "") or "") == "memento" for t in sub_tools
                    )

                    def _effective_skill_docs_read() -> bool:
                        """도메인이 직접 읽었거나, 스킬 탐색 도우미가 같은 런에서 이미 기록한 경우."""
                        if domain_tier_state.get("skill_docs_read"):
                            return True
                        ho: dict[str, Any] | None = None
                        try:
                            st = subagent_event_state
                            if isinstance(st, dict):
                                ho = st.get("skill_handoff")
                        except Exception:
                            ho = None
                        if _discovery_obs_read_skill_docs_ok(ho if isinstance(ho, dict) else None):
                            domain_tier_state["skill_docs_read"] = True
                            return True
                        ent_full = structured_skill_handoff_by_job.get(discovery_job_id)
                        if _discovery_obs_read_skill_docs_ok(
                            ent_full if isinstance(ent_full, dict) else None
                        ):
                            domain_tier_state["skill_docs_read"] = True
                            return True
                        return False

                    def _effective_tier1_attempted() -> bool:
                        if domain_tier_state.get("tier1_attempted"):
                            return True
                        if _discovery_obs_tier1_probe_ok():
                            domain_tier_state["tier1_attempted"] = True
                            return True
                        return False

                    def _domain_tool_input_augmenter(base_tool: Any, tool_input: Any) -> Any:
                        """Fix common confusion: passing FS absolute paths to MCP doc readers."""
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        if tool_name != "read_skill_document":
                            return tool_input
                        merged = _coerce_tool_input_dict(tool_input)
                        if merged is None:
                            return tool_input
                        args = merged.get("args") if isinstance(merged.get("args"), dict) else merged
                        if not isinstance(args, dict):
                            return merged
                        dp = str(args.get("document_path") or "").strip()
                        if dp.startswith("/") and ("/app/skills" in dp or "/skills/" in dp):
                            # Force listing mode; the agent must pick a valid MCP document path.
                            args["document_path"] = None
                        return merged

                    def _domain_tool_call_guard(base_tool: Any, tool_input: Any) -> Any | None:
                        tool_name = str(getattr(base_tool, "name", "") or "")
                        server = str(getattr(base_tool, "_mcp_server", "") or "").lower()

                        def _memento_preflight_required_for_sandbox() -> dict[str, Any] | None:
                            """도메인이 memento 를 갖춘 경우, 샌드박스 물리화/실행 전 1회 조회를 강제."""
                            if not domain_has_memento_tool:
                                return None
                            if os.environ.get("DISABLE_DOMAIN_MEMENTO_GATE", "").strip().lower() in (
                                "1",
                                "true",
                                "yes",
                                "on",
                            ):
                                return None
                            try:
                                seen = subagent_tool_log.get(sub_job_id) or []
                            except Exception:
                                seen = []
                            if "memento" in seen:
                                return None
                            return {
                                "error": "memento_required_before_sandbox",
                                "message": (
                                    "프로세스/인스턴스 문맥 조회를 위해 `memento` 도구를 최소 1회 호출한 뒤 "
                                    "스킬 물리화(`sandbox_copy_mounted_skill` 등)나 샌드박스 실행을 진행하세요. "
                                    "작업 주제·키워드로 질의하면 됩니다."
                                ),
                                "requires_human": False,
                            }

                        # Never allow domain agents to mutate the shared skill catalog.
                        if tool_name in ("delete_skill", "delete_skill_file", "update_skill_file"):
                            return {
                                "error": "skill_catalog_mutation_forbidden",
                                "message": (
                                    "도메인 서브에이전트는 스킬 카탈로그를 수정/삭제할 수 없습니다. "
                                    "필요하면 `스킬 탐색 도우미` 또는 오케스트레이터 레벨에서 처리하세요."
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

                        # Prevent domain subagents from re-running skill discovery search.
                        # Skill discovery is centralized in `스킬 탐색 도우미`; domain should only read docs.
                        if tool_name == "find_helpful_skills":
                            return {
                                "error": "domain_skill_search_forbidden",
                                "message": (
                                    "도메인 서브에이전트는 `find_helpful_skills`로 스킬 탐색을 수행할 수 없습니다. "
                                    "`스킬 탐색 도우미`의 핸드오프를 사용하고, 필요한 경우 `read_skill_document`로 "
                                    "해당 스킬 문서를 확인한 뒤 실행 단계로 진행하세요."
                                ),
                                "requires_human": False,
                            }

                        # Require skill materialization before running arbitrary sandbox code.
                        # This prevents "ignore skill discovery, generate random API script" failures.
                        if tool_name == "sandbox_copy_mounted_skill":
                            if not _effective_skill_docs_read():
                                return {
                                    "error": "skill_docs_required_before_materialization",
                                    "message": (
                                        "스킬을 샌드박스에 복사/물리화하기 전에 스킬 문서(엔드포인트/사용법) 확인이 필요합니다. "
                                        "`스킬 탐색 도우미`가 이미 `read_skill_document`로 기록했다면 그대로 진행 가능합니다. "
                                        "그렇지 않으면 `read_skill_document(skill_name=...)`로 확인하세요."
                                    ),
                                    "requires_human": False,
                                }
                            mem_err = _memento_preflight_required_for_sandbox()
                            if mem_err is not None:
                                return mem_err
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
                                        "`스킬 탐색 도우미`가 이미 문서를 읽고 기록했다면 그대로 진행하고, "
                                        "아니면 `read_skill_document` 후 `sandbox_copy_mounted_skill` 순으로 진행하세요."
                                    ),
                                    "requires_human": False,
                                }
                            mem_err = _memento_preflight_required_for_sandbox()
                            if mem_err is not None:
                                return mem_err
                            # For FS-mount skills, require sandbox_copy_mounted_skill first.
                            # For MCP-document skills, allow sandbox_write_* materialization after docs are read.
                            if not domain_tier_state.get("mounted_skill_copied"):
                                # Best-effort: infer materialization route from injected skill handoff.
                                handoff = None
                                try:
                                    handoff = (
                                        subagent_event_state.get("skill_handoff")
                                        if isinstance(subagent_event_state, dict)
                                        else None
                                    )
                                except Exception:
                                    handoff = None
                                # If skill discovery produced per-domain handoffs, prefer this agent's slice.
                                if isinstance(handoff, dict):
                                    try:
                                        by_agent = handoff.get("by_agent")
                                        if isinstance(by_agent, dict):
                                            per = by_agent.get(agent_name)
                                            if isinstance(per, dict) and per:
                                                handoff = per
                                    except Exception:
                                        pass
                                primary_source = ""
                                if isinstance(handoff, dict):
                                    ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
                                    if ranked and isinstance(ranked[0], dict):
                                        ev = ranked[0].get("evidence") if isinstance(ranked[0].get("evidence"), dict) else {}
                                        primary_source = str(ev.get("source") or "").strip().lower()
                                if primary_source != "mcp":
                                    return {
                                        "error": "skill_materialization_required_before_sandbox",
                                        "message": (
                                            "샌드박스에서 임의 코드 실행 전에 스킬을 먼저 물리화해야 합니다. "
                                            "파일형/FS 마운트형이면 `sandbox_copy_mounted_skill(workspace_id, tenant_id, skill_name, dest_subdir=...)`로 "
                                            "스킬 파일을 복사한 뒤 진행하세요. "
                                            "문서형/MCP형이면 `read_skill_document`로 문서를 읽고 "
                                            "`sandbox_write_files`로 `skill_docs/<skill_name>/...`에 저장해 물리화한 뒤 진행하세요."
                                        ),
                                        "requires_human": False,
                                    }

                        # If the structured handoff indicates required env vars, require env_json to be supplied
                        # for sandbox execution calls (so config/credentials are injected deterministically).
                        if tool_name in ("sandbox_write_and_run", "sandbox_write_files_and_run"):
                            try:
                                handoff = subagent_event_state.get("skill_handoff") if isinstance(subagent_event_state, dict) else None
                            except Exception:
                                handoff = None
                            # If skill discovery produced per-domain handoffs, prefer this agent's slice.
                            if isinstance(handoff, dict):
                                try:
                                    by_agent = handoff.get("by_agent")
                                    if isinstance(by_agent, dict):
                                        per = by_agent.get(agent_name)
                                        if isinstance(per, dict) and per:
                                            handoff = per
                                except Exception:
                                    pass
                            req_env = {}
                            if isinstance(handoff, dict):
                                req_env = handoff.get("required_env") if isinstance(handoff.get("required_env"), dict) else {}
                            if req_env:
                                raw = tool_input if isinstance(tool_input, dict) else _coerce_tool_input_dict(tool_input)
                                raw = raw if isinstance(raw, dict) else {}
                                args = raw.get("args") if isinstance(raw.get("args"), dict) else raw
                                env_json = ""
                                if isinstance(args, dict):
                                    env_json = str(args.get("env_json") or "").strip()
                                if not env_json:
                                    return {
                                        "error": "env_injection_required",
                                        "message": (
                                            "스킬 문서에서 환경변수 요구사항이 감지되었습니다(required_env). "
                                            "샌드박스 실행 시 `env_json`으로 환경변수를 텍스트 형태로 주입해야 합니다. "
                                            "또한 필요한 경우 `sandbox_write_files_and_run`로 컨텍스트 원재료를 파일로 생성한 뒤 실행하세요."
                                        ),
                                        "requires_human": False,
                                    }

                        # Tier 2 (claude-skills MCP) is allowed only as fallback, after Tier 1 was attempted.
                        is_skill_mcp = ("claude-skills" in server) or tool_name in (
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
                                    "스킬 탐색은 기본적으로 `스킬 탐색 도우미`가 전담합니다. "
                                    "폴백으로 직접 MCP 탐색이 필요하면, 먼저 Tier1(볼륨) 확인을 위해 "
                                    "`skill_volume_probe`를 호출하세요(스킬 탐색 도우미가 이미 호출했다면 생략 가능). "
                                    "이후 `list_skills`/`find_helpful_skills`/`read_skill_document`를 진행할 수 있습니다."
                                ),
                                "requires_human": False,
                            }

                        return None

                    tool_call_guard = _domain_tool_call_guard
                    tool_input_augmenter = _domain_tool_input_augmenter

                    def _domain_tool_result_hook(
                        *,
                        base_tool: Any,
                        job_id: str,
                        tool_input: Any,
                        tool_output: Any,
                        error: Exception | None,
                    ) -> None:
                        """Capture structured stdout from domain executions for deterministic form backfill."""
                        if error is not None:
                            return
                        tname = str(getattr(base_tool, "name", "") or "")
                        if tname not in (
                            "sandbox_run_shell",
                            "sandbox_write_and_run",
                            "sandbox_write_files_and_run",
                        ):
                            return
                        if not isinstance(tool_output, dict):
                            return
                        stdout = tool_output.get("stdout") if isinstance(tool_output.get("stdout"), str) else ""
                        stderr = tool_output.get("stderr") if isinstance(tool_output.get("stderr"), str) else ""
                        if not stdout.strip():
                            return
                        try:
                            parsed = json.loads(stdout.strip())
                        except Exception:
                            return
                        if not isinstance(parsed, dict):
                            return
                        bucket = structured_domain_outputs_by_job.setdefault(job_id, {})
                        items = bucket.setdefault(tname, [])
                        if isinstance(items, list):
                            items.append(
                                {
                                    "command": tool_output.get("command"),
                                    "exit_code": tool_output.get("exit_code"),
                                    "parsed_stdout": parsed,
                                    "stdout_head": stdout.strip()[:8000],
                                    "stderr_head": stderr.strip()[:8000],
                                }
                            )
                _instrument_tools(
                    sub_tools,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    crew_type="action",
                    job_id_getter=lambda sj=sub_job_id: sj,
                    job_tool_log=subagent_tool_log,
                    job_skill_log=subagent_skill_log,
                    tool_result_hook=(
                        _skill_discovery_tool_result_hook
                        if agent_name == SKILL_DISCOVERY_SUBAGENT_NAME
                        else _domain_tool_result_hook
                    ),
                    exec_ctx=sub_exec_ctx,
                    event_queue=event_queue,
                    tool_input_augmenter=tool_input_augmenter,
                    tool_call_guard=tool_call_guard,
                    context_tool_tracker=context_tool_tracker,
                )
                logger.info(
                    "서브에이전트 도구 인스트루먼트 완료 | task_id=%s agent=%s job_id=%s tools=%d",
                    task_id,
                    agent_name,
                    sub_job_id,
                    len(sub_tools),
                )

            subagent_contexts.append(
                {
                    "job_id": sub_job_id,
                    "role": agent_role,
                    "name": agent_name,
                    "goal": agent_goal,
                    "agent_profile": agent_profile,
                    "agent_id": agent_id,
                    "exec_ctx": sub_exec_ctx,
                }
            )

        instrumented_hitl_tools = list(get_hitl_tools())
        _instrument_tools(
            instrumented_hitl_tools,
            todo_id=task_id or None,
            proc_inst_id=proc_inst_id or None,
            crew_type="action",
            job_id_getter=lambda: job_uuid,
            job_tool_log=subagent_tool_log,
            job_skill_log=subagent_skill_log,
            exec_ctx=exec_ctx,
            event_queue=event_queue,
            tool_input_augmenter=None,
            tool_call_guard=_make_request_human_input_context_guard(context_tool_tracker),
            context_tool_tracker=context_tool_tracker,
        )

        routing_cfg = resolve_model_routing(extras, default_model="openai:gpt-4o")
        model_name = routing_cfg.model_name

        # 단일 DeepAgent 런타임(Planning + Delegation + Execution)
        base_rules = default_orchestrator_prompt()
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

        system_prompt = build_orchestrator_system_prompt(
            base_rules=base_rules,
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
            plan_text=None,
            output_language=output_language,
            hitl_resume=hitl_resume,
        )

        # NOTE: 루트(DeepAgent) task_started 는 서브에이전트 이벤트보다 먼저 발행되어야 한다.
        # (DB 인입 시점 기준으로 서브가 먼저 시작한 것처럼 보이는 문제 방지)
        _enqueue_status(
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
            "action",
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
        # 플래닝 phase의 task_completed 는 (가능하면) write_todos 도구 호출 성공 시점에만 발행한다.
        planning_progress: dict[str, Any] = {
            "saw_write_todos": False,
            "planning_completed_emitted": False,
        }
        write_todos_planning_mw = _create_write_todos_planning_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            job_uuid=job_uuid,
            exec_ctx=exec_ctx,
            planning_gate=planning_progress,
        )
        enforce_write_todos_planning = write_todos_planning_mw is not None
        planning_completed_emitted = False

        def _planning_completed_flag() -> bool:
            if enforce_write_todos_planning:
                return bool(planning_progress.get("planning_completed_emitted"))
            return planning_completed_emitted

        async def _emit_planning_completed_fallback(message: str) -> None:
            """플랜 본문을 포착하지 못한 경우에도 planning 완료 이벤트를 보장한다."""
            nonlocal planning_completed_emitted
            if _planning_completed_flag():
                return
            _enqueue_status(
                event_queue,
                "task_completed",
                {"message": message},
                proc_inst_id,
                task_id,
                "action",
                job_id=job_uuid,
                exec_ctx=exec_ctx,
                logical_event_type="agent_finished",
                phase="planning",
                agent_name="작업 실행 기획자",
            )
            if enforce_write_todos_planning:
                planning_progress["planning_completed_emitted"] = True
            else:
                planning_completed_emitted = True

        async def _before_subagent_start_legacy() -> None:
            # write_todos 게이트 미사용 시에만: 첫 task() 직전 플래닝 완료를 state 기반으로 보정한다.
            await _emit_planning_completed_fallback("플래닝이 완료되어 실행 단계로 전환합니다.")

        name_to_context = {str(c["name"]): c for c in subagent_contexts}
        subagent_event_middleware, subagent_event_state = _create_subagent_event_middleware(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            name_to_context=name_to_context,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            structured_skill_handoff_by_job=structured_skill_handoff_by_job,
            on_before_subagent_start=(
                None if enforce_write_todos_planning else _before_subagent_start_legacy
            ),
            planning_gate=planning_progress if enforce_write_todos_planning else None,
        )
        extra_middleware: list[Any] = [SubagentJobScopeMiddleware()]
        if write_todos_planning_mw is not None:
            extra_middleware.append(write_todos_planning_mw)
        if subagent_event_middleware is not None:
            extra_middleware.append(subagent_event_middleware)

        orchestrator = build_orchestrator(
            model_name=model_name,
            system_prompt=system_prompt,
            subagents=subagents,
            tools=[],
            hitl_tools=instrumented_hitl_tools,
            middleware=extra_middleware,
            skills=tenant_skill_sources_for_middleware(tenant_id or None),
        )
        logger.info(
            "오케스트레이터 빌드 완료 | task_id=%s model=%s subagents=%d middleware=%d",
            task_id,
            model_name,
            len(subagents),
            len(extra_middleware),
        )

        name_to_sub_job = {str(c["name"]): str(c["job_id"]) for c in subagent_contexts}
        name_to_sub_ex = {str(c["name"]): c["exec_ctx"] for c in subagent_contexts}
        skill_vol_binding = SkillVolumeToolEventBinding(
            event_queue=event_queue,
            todo_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="action",
            default_job_id=job_uuid,
            root_exec_ctx=exec_ctx,
            subagent_name_to_job_id=name_to_sub_job,
            subagent_name_to_exec_ctx=name_to_sub_ex,
        )
        tok_skill_bind = skill_volume_tool_event_binding.set(skill_vol_binding)
        tok_skill_job = skill_volume_active_job_id.set(job_uuid)
        tok_skill_ex = skill_volume_active_exec_ctx.set(exec_ctx)
        try:
            logger.info("오케스트레이터 실행 시작 | task_id=%s", task_id)
            result = None

            async def _extract_plan_message_from_state(state: Any) -> str:
                """Try best-effort extraction of [PLAN] from a LangGraph state."""
                if not isinstance(state, dict):
                    return ""
                msgs = state.get("messages", []) or []
                try:
                    for msg in reversed(msgs):
                        content = (
                            msg.get("content")
                            if isinstance(msg, dict)
                            else getattr(msg, "content", None)
                        )
                        raw_text_for_plan = _extract_plain_text(content)
                        candidate = _extract_plan_block(raw_text_for_plan) or ""
                        if candidate:
                            return candidate
                except Exception:
                    logger.exception("Failed while scanning messages for [PLAN] block", exc_info=True)
                return ""

            async def _emit_planning_completed_if_ready(state: Any) -> None:
                nonlocal planning_completed_emitted
                if enforce_write_todos_planning:
                    return
                if planning_completed_emitted:
                    return
                if not isinstance(state, dict):
                    return

                todos_state = state.get("todos")
                plan_message = ""
                try:
                    plan_message = await _extract_plan_message_from_state(state)
                except Exception:
                    logger.exception("Failed to extract plan message from state", exc_info=True)

                if not plan_message and todos_state is not None:
                    try:
                        plan_message = _join_todo_contents(todos_state)
                    except Exception:
                        plan_message = ""

                # 플랜이 아직 확정되지 않았으면 대기 (첫 관측 시점에 즉시 발행)
                if not plan_message and todos_state is None:
                    return

                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"message": plan_message or "플래닝이 완료되었습니다."},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_uuid,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_finished",
                    phase="planning",
                    agent_name="작업 실행 기획자",
                )
                planning_completed_emitted = True

            # 가능한 경우 스트리밍으로 플래닝 완료 시점을 조기에 포착
            stream_input = {"messages": [{"role": "user", "content": invoke_user_text}]}
            if hasattr(orchestrator, "astream"):
                async for state in orchestrator.astream(stream_input, stream_mode="values"):
                    result = state
                    await _emit_planning_completed_if_ready(state)
            else:
                result = await orchestrator.ainvoke(stream_input)
                await _emit_planning_completed_if_ready(result)

            logger.info(
                "오케스트레이터 실행 완료 | task_id=%s result_type=%s keys=%s",
                task_id,
                type(result).__name__,
                list(result.keys()) if isinstance(result, dict) else [],
            )
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
                return {
                    "status": "waiting_for_human",
                    "run_state": run_state,
                }
            try:
                hitl_job_id = str(uuid.uuid4())
                _enqueue_status(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": choices,
                        "reason": decision.get("reason"),
                        "tool_call_context": {
                            "job_id": hi.job_id,
                            "tool_name": hi.tool_name,
                        },
                    },
                    proc_inst_id,
                    task_id,
                    "action",
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
            return {
                "status": "waiting_for_human",
                "run_state": {
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
                },
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
                return {
                    "status": "waiting_for_human",
                    "run_state": run_state,
                }
            hitl_agent_name = (
                (exec_ctx.agent_path[-1] if exec_ctx.agent_path else None)
                or "작업 실행 기획자"
            )
            try:
                hitl_job_id = str(uuid.uuid4())
                _enqueue_status(
                    event_queue,
                    "human_asked",
                    {
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": [],
                        "interrupt_payload": interrupt_payload,
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=hitl_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    agent_name=hitl_agent_name,
                    status="ASKED",
                )
            except Exception:
                logger.exception(
                    "Failed to enqueue human_asked event from graph-level HITL", exc_info=True
                )

            sandbox_output_paths_discard_task(task_id)
            return {
                "status": "waiting_for_human",
                "run_state": {
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
                },
            }

        final_messages = result.get("messages", [])
        raw_text = ""
        if final_messages:
            last = final_messages[-1]
            content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
            raw_text = _extract_plain_text(content)

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
            parsed = None
            try:
                parsed = json.loads((text or "").strip())
            except Exception:
                parsed = None
            return parsed if isinstance(parsed, dict) else None

        # 폼이 없거나 defaultform인 경우, 항상 JSON 파싱 가능한 고정 포맷으로 래핑
        has_form = bool(form_fields) and form_id not in (None, "", "defaultform")
        if not has_form:
            base_text = original_wo_form or wrapped_result.get("cleaned") or ""
            # 모델이 리스트/딕셔너리 구조를 문자열로 반환한 경우에도
            # 내부 text/content 필드만 추출하여 순수 텍스트로 만든다.
            final_text = _extract_plain_text(base_text).strip()
            # 최종 사용자 출력에는 PLAN 블록을 절대 포함하지 않는다.
            final_text = _strip_plan_blocks(final_text)
            result_block = _extract_result_block(final_text)
            if result_block:
                final_text = result_block
            result_status = "SUCCESS" if final_text else "Fail"
            final_result_payload = {
                "output": {
                    "status": result_status,
                    "result": final_text,
                },
            }
        else:
            # For form outputs, the deliverable MUST be produced by an LLM, not heuristics.
            # If the orchestrator didn't emit a complete form JSON, run a tool-free finalizer LLM pass
            # using captured execution evidence (including structured subagent tool outputs).
            field_keys = _extract_form_field_keys(form_fields)
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
                    "skill_handoff": structured_skill_handoff_by_job,
                }
                finalized = await _finalize_form_with_llm(
                    model_name=model_name,
                    form_id=str(form_id),
                    form_fields=form_fields,
                    evidence=evidence,
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
        scanned_paths = collect_open_workspace_paths_for_globs(default_artifact_globs())
        loaded_files = collect_readable_artifacts(tracked_paths, extra_paths=scanned_paths)
        file_manifest = enqueue_sandbox_file_artifacts(
            event_queue,
            proc_inst_id or "",
            str(task_id),
            loaded_files,
            tenant_id=str(tenant_id).strip() if tenant_id else None,
        )
        if file_manifest:
            wrapped_result["deliverable_files"] = file_manifest
            link_entries = [
                {"name": f.get("name"), "public_url": f.get("public_url")}
                for f in file_manifest
                if isinstance(f.get("public_url"), str) and str(f["public_url"]).strip()
            ]
            if not has_form:
                names = ", ".join(str(f.get("name") or "") for f in file_manifest if f.get("name"))
                extra = f"\n\n[첨부 파일] {len(file_manifest)}개: {names}" if names else f"\n\n[첨부 파일] {len(file_manifest)}개"
                link_lines = [f"- {e['name']}: {e['public_url']}" for e in link_entries]
                if link_lines:
                    extra += "\n\n다운로드(공개 링크):\n" + "\n".join(link_lines)
                final_text = (final_text or "") + extra
                final_result_payload["output"]["result"] = final_text.strip()
                final_event_result = final_text.strip()
            elif link_entries:
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
            structured_skill_handoff_by_job=structured_skill_handoff_by_job,
            deliverable_files=file_manifest or None,
        )
        # 2-layer output:
        # - final_output: user-facing final deliverable
        # - execution_evidence: internal/verifiable execution details
        wrapped_result["final_output"] = final_result_payload
        wrapped_result["execution_evidence"] = execution_evidence
        has_execution_evidence = any(
            bool((run.get("tools_used") or []))
            for run in (execution_evidence.get("subagent_runs") or [])
        ) or bool(file_manifest)
        if not has_execution_evidence:
            final_result_payload = {
                "output": {
                    "status": "Fail",
                    "result": "실행 증거가 부족하여 결과를 확정할 수 없습니다. 자동 탐색/실행 단계를 재시도해 주세요.",
                }
            }
            wrapped_result["final_output"] = final_result_payload
            if not has_form:
                final_event_result = final_result_payload["output"]["result"]
            else:
                final_event_result = {
                    "status": "Fail",
                    "reason": "missing_execution_evidence",
                    "result": final_event_result,
                }

        end_job = str(uuid.uuid4())
        _enqueue_status(
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
        _enqueue_status(
            event_queue,
            "task_completed",
            # NOTE: For crew_type='result' we want the event payload itself to be the deliverable.
            # - no-form: keep wrapper with message + result string
            # - form: {"form_id": "...", "result": {...}} for UI extraction
            (
                final_event_result
                if has_form
                else {"message": "최종 결과 생성이 완료되었습니다.", "result": final_event_result}
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

        _enqueue_artifact(
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
        return None

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("작업 취소 요청됨")
