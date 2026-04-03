"""Subagent task() event middleware and timeline fallbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from app.skills.discovery_subagent import SKILL_DISCOVERY_SUBAGENT_NAME

from .a2a_events import _enqueue_status
from .context import ExecutionContext
from .tool_calls import _request_with_merged_tool_args, _tool_call_args_from_request
from .workitem_text import _extract_plain_text

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]
    ToolCallRequest = None  # type: ignore[misc, assignment]


def _create_subagent_event_middleware(
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    name_to_context: dict[str, dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    structured_skill_handoff_by_job: dict[str, dict[str, Any]] | None = None,
    on_before_subagent_start: Any | None = None,
    planning_gate: dict[str, Any] | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """Create middleware that emits task_started/task_completed around each subagent (task tool) run.

    Only used when AgentMiddleware is available; otherwise returns (None, state) and executor
    falls back to emitting subagent events after the run.
    """
    # 미들웨어가 실제로 subagent task() 호출을 감쌌는지 추적하기 위한 상태.
    # 도구 호출(log only)만 발생하고 task() 자체는 다른 계층에서 실행되는 경우,
    # 이 플래그가 False 로 남아 있어 fallback 이벤트 발행 로직을 트리거한다.
    state: dict[str, Any] = {
        "saw_task_call": False,
        # task() 미들웨어가 task_started를 enqueue한 job_id / task_completed까지 보낸 job_id (보정용).
        "subagent_task_jobs_started": set(),
        "subagent_task_jobs_completed": set(),
        # Captured from `스킬 탐색 도우미` output; injected into subsequent domain task() calls.
        "skill_handoff": None,
        "skill_handoff_raw": "",
        "skill_handoff_by_agent": {},
        "skill_handoff_structured": structured_skill_handoff_by_job or {},
    }

    if AgentMiddleware is None or ToolCallRequest is None:
        return None, state

    def _derive_subagent_goal_from_task_args(args: dict[str, Any], default_goal: str) -> str:
        """Prefer delegated task() instruction text over static DB-config goal."""
        if not isinstance(args, dict):
            return default_goal or ""
        for key in ("goal", "instruction", "description", "prompt", "query", "task"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            text = _extract_plain_text(value).strip()
            if text:
                return text
        return default_goal or ""

    def _goal_for_task_started_event(raw: str, *, max_chars: int = 1200) -> str:
        """UI/이벤트용 goal: 내부 주입 없이 짧게 유지."""
        s = (raw or "").strip()
        if not s:
            return ""
        if len(s) <= max_chars:
            return s
        return s[: max_chars - 1].rstrip() + "…"

    def _extract_skill_handoff_from_text(text: str) -> dict[str, Any] | None:
        """Extract a structured skill handoff JSON object from tool output text.

        Expected format (anywhere in text):
          [SKILL_HANDOFF_V1]
          { ... valid JSON ... }
        """
        if not isinstance(text, str) or not text.strip():
            return None
        blob = text
        m = re.search(r"\[SKILL_HANDOFF_V1\]\s*(\{[\s\S]*?\})\s*(?:$|\n\[)", blob)
        if not m:
            # Fallback: try to capture to end of text.
            m = re.search(r"\[SKILL_HANDOFF_V1\]\s*(\{[\s\S]*\})\s*$", blob)
        if not m:
            return None
        raw = (m.group(1) or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        # Minimal sanity keys; keep permissive to avoid brittle failures.
        parsed.setdefault("version", 1)
        return parsed

    def _build_skill_handoff_directive(handoff: dict[str, Any]) -> str:
        """Build a deterministic directive block to prepend to domain subagent instructions."""
        try:
            compact = json.dumps(handoff, ensure_ascii=False)
        except Exception:
            compact = "{}"
        selected = ""
        try:
            selected = str(handoff.get("selected_primary") or "").strip()
        except Exception:
            selected = ""
        primary_source = ""
        try:
            ranked = handoff.get("ranked") if isinstance(handoff.get("ranked"), list) else []
            if ranked and isinstance(ranked[0], dict):
                ev = ranked[0].get("evidence") if isinstance(ranked[0].get("evidence"), dict) else {}
                primary_source = str(ev.get("source") or "").strip().lower()
        except Exception:
            primary_source = ""
        return (
            "[SKILL_HANDOFF_REQUIRED_V1]\n"
            "필수: 아래 `스킬 핸드오프`(스킬 탐색 도우미 결과)를 작업 시작 시 최우선으로 참고해야 합니다.\n"
            "- 스킬 탐색 도우미가 이미 `read_skill_document`로 문서를 조회·기록했다면, 실행 단계에서는 "
            "동일 작업을 반복하지 말고 핸드오프·탐색 결과를 그대로 사용해 샌드박스 물리화·실행으로 진행하세요.\n"
            "- 작업 계획/실행 시, 이 핸드오프에 명시된 스킬/문서/엔드포인트를 우선 사용하세요.\n"
            "- 실행이 필요하면, 아래 2가지 케이스 중 해당하는 방식으로 스킬을 샌드박스에 물리화한 뒤 진행하세요.\n"
            "  - (파일형/FS 마운트형) Tier1 볼륨(/app/skills)에 스킬이 있으면: "
            "`sandbox_copy_mounted_skill(workspace_id, tenant_id, skill_name, dest_subdir='skill')`\n"
            "  - (문서형/MCP형) Tier1에 없고 MCP(claude-skills)로만 존재하면:\n"
            "    1) 탐색 단계에서 문서 목록/본문을 확보하지 않았다면 "
            "`read_skill_document(skill_name=<name>, document_path=null)`로 목록을 받고\n"
            "    2) 핸드오프의 `documents[].path`(또는 목록에서 고른 경로들)를 `read_skill_document`로 읽어온 뒤\n"
            "    3) `sandbox_write_files(workspace_id, files_json)`로 `skill_docs/<skill_name>/<path>`에 저장하세요.\n"
            "    4) 필요하면 위 저장된 문서를 근거로 실행 스크립트를 생성해 `sandbox_write_and_run`으로 실행하세요.\n"
            + (f"- 권장 primary skill_name: {selected}\n" if selected else "")
            + (f"- 권장 primary evidence.source: {primary_source}\n" if primary_source else "")
            + "\n[SKILL_HANDOFF_V1]\n"
            + compact
            + "\n"
        )

    def _select_skill_handoff_for_domain(subagent_type: str) -> dict[str, Any] | None:
        """Pick the best available handoff for a given domain subagent."""
        if not isinstance(subagent_type, str) or not subagent_type.strip():
            return None
        try:
            by_agent = state.get("skill_handoff_by_agent")
            if isinstance(by_agent, dict):
                per = by_agent.get(subagent_type)
                if isinstance(per, dict) and per:
                    return per
        except Exception:
            pass
        h = state.get("skill_handoff")
        return h if isinstance(h, dict) and h else None

    def _missing_skill_discovery_checklist_tools(*, job_id: str) -> list[str]:
        used = set(subagent_tool_log.get(job_id, []) or [])
        required_fs = {"skill_volume_probe"}
        required_mcp = {"find_helpful_skills", "read_skill_document"}
        missing = sorted((required_fs | required_mcp) - used)
        return missing

    def _inject_skill_discovery_retry_directive(
        *,
        args: dict[str, Any],
        request: Any,
        job_id: str,
        attempt: int,
        missing: list[str],
    ) -> Any:
        marker = "[SKILL_DISCOVERY_RETRY_V1]"
        used = sorted(set(subagent_tool_log.get(job_id, []) or []))
        directive = (
            f"{marker}\n"
            "필수: 직전 스킬 탐색이 체크리스트를 충족하지 못했습니다. 지금 즉시 재시도하세요.\n"
            "- 이번 재시도에서는 아래 누락 도구를 반드시 최소 1회씩 호출해야 합니다.\n"
            f"- missing_tools: {missing}\n"
            f"- used_tools_so_far: {used}\n"
            f"- attempt: {attempt}\n"
            "추가 규칙:\n"
            "- `skill_volume_probe`는 allowed_skill_names 전체를 대상으로 호출하세요.\n"
            "- `find_helpful_skills`는 tenant_id + allowed_skill_names를 포함해서 호출하세요.\n"
            "- `read_skill_document`는 최소 1개 후보 스킬에 대해 document_path=null로 목록을 받고, "
            "그 다음 실제 문서를 1개 이상 읽어(또는 핵심 레퍼런스를 확인해) 엔드포인트/사용법 근거를 확보하세요.\n"
            "- 마지막 출력에는 반드시 `[SKILL_HANDOFF_V1]` JSON을 포함하세요.\n"
        )
        if not isinstance(args, dict):
            return request
        for key in ("instruction", "goal", "description", "prompt", "query", "task"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                if marker not in v:
                    args[key] = directive + "\n" + v
                break
        else:
            args["instruction"] = directive
        return _request_with_merged_tool_args(request, args)

    def _observations_substantive_read_skill_document(ent: Any) -> bool:
        """목록 응답이 아니라 실제 문서(경로 지정 또는 충분한 본문)를 read_skill_document로 읽었는지."""
        if not isinstance(ent, dict):
            return False
        obs = ent.get("_observations")
        if not isinstance(obs, dict):
            return False
        rsd = obs.get("read_skill_document")
        if not isinstance(rsd, dict) or not rsd:
            return False
        list_marker = "Available documents for skill"
        for _sk, docs in rsd.items():
            if not isinstance(docs, list):
                continue
            for d in docs:
                if not isinstance(d, dict):
                    continue
                dp = d.get("document_path")
                text = str(d.get("text") or "")
                dp_s = str(dp).strip().lower() if dp is not None else ""
                if dp is not None and dp_s and dp_s not in ("null", "none"):
                    return True
                if len(text) >= 400 and list_marker not in text[:300]:
                    return True
        return False

    def _merge_synthetic_skill_handoff_from_observations(ent: dict[str, Any]) -> dict[str, Any] | None:
        """모델이 [SKILL_HANDOFF_V1]를 출력하지 않았을 때 tool_result_hook 관측으로 핸드오프 필드를 채운다."""
        if not _observations_substantive_read_skill_document(ent):
            return None
        out = dict(ent)
        allow = [str(x).strip() for x in (out.get("allowed_skill_names") or []) if str(x).strip()]
        rsd = (out.get("_observations") or {}).get("read_skill_document")
        list_marker = "Available documents for skill"

        def _docs_substantive(sk_docs: Any) -> bool:
            if not isinstance(sk_docs, list):
                return False
            for d in sk_docs:
                if not isinstance(d, dict):
                    continue
                dp = d.get("document_path")
                text = str(d.get("text") or "")
                dp_s = str(dp).strip().lower() if dp is not None else ""
                if dp is not None and dp_s and dp_s not in ("null", "none"):
                    return True
                if len(text) >= 400 and list_marker not in text[:300]:
                    return True
            return False

        primary = str(out.get("selected_primary") or "").strip()
        if isinstance(rsd, dict):
            if primary and not _docs_substantive(rsd.get(primary)):
                primary = ""
            if not primary:
                for name in list(allow) + [str(k) for k in rsd.keys()]:
                    nk = str(name).strip()
                    if nk and _docs_substantive(rsd.get(nk)):
                        primary = nk
                        break
        if not primary and allow:
            primary = allow[0]
        if not primary and isinstance(rsd, dict) and rsd:
            primary = str(next(iter(rsd.keys()))).strip()
        out["selected_primary"] = primary or str(out.get("selected_primary") or "")
        if not out.get("ranked"):
            sk = str(out["selected_primary"] or "").strip() or "unknown"
            out["ranked"] = [
                {
                    "skill_name": sk,
                    "evidence": {
                        "source": "mcp",
                        "notes": (
                            "Synthesized from read_skill_document tool traces "
                            "(no [SKILL_HANDOFF_V1] in model text)."
                        ),
                    },
                    "documents": [],
                    "endpoints": [],
                    "sandbox_materialization": {"required": True, "suggested_calls": []},
                }
            ]
        out["synthetic_handoff_from_observations"] = True
        return out

    def _handoff_dict_for_state(ent: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ent.items() if k != "_observations"}

    class SubagentEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            tool_name = getattr(getattr(request, "tool", None), "name", None)
            if tool_name != "task":
                return await handler(request)

            if planning_gate is not None and not planning_gate.get("saw_write_todos"):
                return {
                    "error": "write_todos_required",
                    "message": (
                        "실행 단계(task)로 넘어가기 전에 write_todos 도구로 작업 계획을 "
                        "한 번 이상 기록해야 합니다. 먼저 write_todos를 호출하세요."
                    ),
                    "requires_human": False,
                }

            # 서브에이전트 시작 직전 콜백: 플래닝 완료 이벤트를 선발행할 수 있게 한다.
            if on_before_subagent_start is not None:
                try:
                    callback_result = on_before_subagent_start()
                    if hasattr(callback_result, "__await__"):
                        await callback_result
                except Exception:
                    logger.exception(
                        "on_before_subagent_start callback failed; continuing task() execution",
                        exc_info=True,
                    )

            args = dict(_tool_call_args_from_request(request))
            subagent_type = args.get("subagent_type") or args.get("subagent_name") or ""
            ctx = name_to_context.get(subagent_type) if subagent_type else None
            if not ctx:
                return await handler(request)

            # Fail-fast: do not allow domain subagents to run before we have a valid skill handoff.
            # This prevents low-quality "invent random endpoints" behavior when skill discovery is incomplete.
            try:
                if subagent_type and subagent_type != SKILL_DISCOVERY_SUBAGENT_NAME:
                    handoff = _select_skill_handoff_for_domain(str(subagent_type))
                    if not isinstance(handoff, dict) or not handoff:
                        return {
                            "error": "skill_handoff_missing",
                            "message": (
                                "도메인 서브 에이전트를 실행하기 전에 `스킬 탐색 도우미`가 "
                                "`[SKILL_HANDOFF_V1]` JSON을 포함한 결과를 먼저 반환해야 합니다. "
                                "현재 런에서는 유효한 스킬 핸드오프가 없어 도메인 실행을 차단합니다."
                            ),
                            "requires_human": False,
                        }
            except Exception:
                logger.exception("Failed while enforcing skill_handoff_missing fail-fast", exc_info=True)

            # task_started.goal 은 주입([SKILL_HANDOFF_*], [TENANT_CONTEXT_*]) 전의 위임 문구만 쓴다.
            # 주입 후 args 에서 다시 파생하면 UI에 raw 마커·JSON 전체가 노출된다.
            event_goal_pre_injection = _derive_subagent_goal_from_task_args(
                dict(args), str(ctx.get("goal") or "")
            )

            # Inject skill handoff into domain subagent task text (if available).
            # This makes cross-subagent sharing deterministic, rather than relying on prompt-following.
            try:
                if subagent_type and subagent_type != SKILL_DISCOVERY_SUBAGENT_NAME:
                    handoff = _select_skill_handoff_for_domain(str(subagent_type))
                    if isinstance(handoff, dict) and handoff:
                        marker = "[SKILL_HANDOFF_REQUIRED_V1]"
                        directive = _build_skill_handoff_directive(handoff)
                        for key in ("instruction", "goal", "description", "prompt", "query", "task"):
                            v = args.get(key)
                            if isinstance(v, str) and v.strip():
                                if marker not in v:
                                    args[key] = directive + "\n" + v
                                break
                        else:
                            args["instruction"] = directive
                        request = _request_with_merged_tool_args(request, args)
            except Exception:
                logger.exception("Failed to inject skill handoff into domain subagent task args", exc_info=True)

            # Nudge domain subagents to use tenant context tools (mem0 / memento / dmn_rule).
            # 스킬 탐색 도우미는 전담 프롬프트·체크리스트(skill_volume_probe / MCP / read_skill_document)가
            # 있으므로 여기서 목표/메모리 유도 문구를 붙이지 않는다(오케스트레이터가 넘긴 instruction만 유지).
            try:
                if subagent_type and subagent_type != SKILL_DISCOVERY_SUBAGENT_NAME:
                    marker = "[TENANT_CONTEXT_TOOLS_V1]"
                    delegated_goal_ctx = _derive_subagent_goal_from_task_args(
                        args, str(ctx.get("goal") or "")
                    )
                    tenant_context_directive = (
                        f"{marker}\n"
                        "테넌트 컨텍스트 도구 `mem0`, `memento`, `dmn_rule`을 작업에 맞게 **적극적으로** 활용하세요.\n"
                        "- `mem0`: 장기 기억·과거 피드백·선호·자격/비밀 단서 검색\n"
                        "- `memento`: 이 프로세스/인스턴스 문맥의 문서·RAG 조회\n"
                        "- `dmn_rule`: 비즈니스 규칙·판단·승인·라우팅 조건이 관련될 때\n"
                        f"- 작업 목표(참고): {delegated_goal_ctx}\n"
                        "관련이 명확히 없다고 판단되면 생략해도 되나, 불확실하면 먼저 조회·검증하는 편을 권장합니다.\n"
                    )

                    if isinstance(args, dict):
                        for key in ("instruction", "goal", "description", "prompt", "query", "task"):
                            v = args.get(key)
                            if isinstance(v, str) and v.strip():
                                if marker not in v:
                                    args[key] = tenant_context_directive + "\n" + v
                                break
                        else:
                            args["instruction"] = tenant_context_directive
                        request = _request_with_merged_tool_args(request, args)
            except Exception:
                logger.exception(
                    "Failed to inject tenant context tools directive into subagent task args",
                    exc_info=True,
                )

            # 실제 매핑 가능한 subagent task()를 감싼 경우만 True.
            # unknown task()만 있었던 런에서는 fallback pair를 유지해야 한다.
            state["saw_task_call"] = True

            job_id = ctx["job_id"]
            display_goal = _goal_for_task_started_event(
                event_goal_pre_injection.strip() or str(ctx.get("goal") or "")
            )
            _enqueue_status(
                event_queue,
                "task_started",
                {
                    "role": ctx["role"],
                    "name": ctx["name"],
                    "goal": display_goal,
                    "agent_profile": ctx["agent_profile"],
                    "agent_id": ctx.get("agent_id"),
                },
                proc_inst_id,
                task_id,
                "action",
                job_id=job_id,
                exec_ctx=ctx.get("exec_ctx"),
            )
            try:
                state["subagent_task_jobs_started"].add(job_id)
            except Exception:
                logger.exception("Failed to record subagent task_started job_id", exc_info=True)
            try:
                # Skill discovery must not "finish early". Retry until checklist is satisfied.
                result = None
                if subagent_type == SKILL_DISCOVERY_SUBAGENT_NAME:
                    max_attempts = 3
                    last_raw = ""
                    last_handoff: dict[str, Any] | None = None
                    last_missing: list[str] = []
                    for attempt in range(1, max_attempts + 1):
                        result = await handler(request)
                        raw = ""
                        if isinstance(result, dict):
                            raw = _extract_plain_text(result.get("content"))
                        else:
                            raw = _extract_plain_text(getattr(result, "content", None))
                        last_raw = raw
                        handoff = _extract_skill_handoff_from_text(raw)
                        last_handoff = handoff if isinstance(handoff, dict) else None
                        missing = _missing_skill_discovery_checklist_tools(job_id=job_id)
                        last_missing = missing
                        # 필수 도구 체크리스트 + (모델 [SKILL_HANDOFF_V1] 또는 structured/관측 기반 합성).
                        root_map: dict[str, Any] = (
                            structured_skill_handoff_by_job
                            if isinstance(structured_skill_handoff_by_job, dict)
                            else (state.get("skill_handoff_structured") or {})
                        )
                        if not isinstance(root_map, dict):
                            root_map = {}
                        ent: dict[str, Any] = {}
                        try:
                            raw_ent = root_map.get(job_id)
                            if isinstance(raw_ent, dict):
                                ent = raw_ent
                        except Exception:
                            ent = {}
                        chosen: dict[str, Any] | None = None
                        if not missing:
                            if isinstance(last_handoff, dict) and last_handoff:
                                chosen = last_handoff
                            elif _observations_substantive_read_skill_document(ent):
                                merged = _merge_synthetic_skill_handoff_from_observations(ent)
                                if isinstance(merged, dict) and merged:
                                    root_map[job_id] = merged
                                    chosen = _handoff_dict_for_state(merged)
                            elif isinstance(ent, dict) and ent:
                                # 훅이 채운 structured 항목만으로도 성공(과거 동작). _observations 는 state 에서 제외.
                                chosen = _handoff_dict_for_state(ent)
                        if chosen:
                            state["skill_handoff"] = chosen
                            try:
                                by_agent = chosen.get("by_agent") if isinstance(chosen, dict) else None
                                if isinstance(by_agent, dict) and by_agent:
                                    state["skill_handoff_by_agent"] = by_agent
                            except Exception:
                                pass
                            state["skill_handoff_raw"] = raw[:12000] if raw else ""
                            break
                        if attempt < max_attempts:
                            request = _inject_skill_discovery_retry_directive(
                                args=args,
                                request=request,
                                job_id=job_id,
                                attempt=attempt + 1,
                                missing=missing or ["[SKILL_HANDOFF_V1]"],
                            )
                            continue
                        raise RuntimeError(
                            "스킬 탐색 도우미가 재시도 후에도 필수 체크리스트를 충족하지 못했습니다. "
                            f"missing={last_missing} handoff_ok={bool(last_handoff)} "
                            f"used={sorted(set(subagent_tool_log.get(job_id, []) or []))} "
                            f"raw_tail={(last_raw or '')[-800:]}"
                        )
                else:
                    result = await handler(request)

                used_tools = subagent_tool_log.get(job_id, [])
                used_skills = subagent_skill_log.get(job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                if used_skills:
                    작업_텍스트 += f" | 참고/사용한 스킬: {', '.join(sorted(set(used_skills)))}"
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": 작업_텍스트,
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
                try:
                    state["subagent_task_jobs_completed"].add(job_id)
                except Exception:
                    logger.exception("Failed to record subagent task_completed job_id", exc_info=True)
                return result
            except Exception:
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "수행한_작업": "작업 실행 중 오류가 발생했습니다.",
                        "role": ctx.get("role"),
                        "name": ctx.get("name"),
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                    exec_ctx=ctx.get("exec_ctx"),
                )
                try:
                    state["subagent_task_jobs_completed"].add(job_id)
                except Exception:
                    logger.exception("Failed to record subagent task_completed job_id (error path)", exc_info=True)
                raise

    return SubagentEventMiddleware(), state


def _emit_subagent_task_started(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    exec_ctx: ExecutionContext | None,
    ctx: dict[str, Any],
) -> None:
    _enqueue_status(
        event_queue,
        "task_started",
        {
            "message": f"서브 에이전트 '{ctx['name']}' 실행을 시작했습니다.",
            "role": ctx.get("role"),
            "goal": ctx.get("goal"),
            "agent_profile": ctx.get("agent_profile"),
            "agent_id": ctx.get("agent_id"),
        },
        proc_inst_id,
        task_id,
        "action",
        job_id=ctx["job_id"],
        exec_ctx=exec_ctx,
        logical_event_type="agent_started",
        phase="reasoning",
        agent_name=ctx["name"],
    )


def _emit_subagent_task_completed(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    exec_ctx: ExecutionContext | None,
    ctx: dict[str, Any],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    message_override: str | None = None,
) -> None:
    sub_job_id = ctx["job_id"]
    used_tools = subagent_tool_log.get(sub_job_id, [])
    used_skills = subagent_skill_log.get(sub_job_id, [])
    작업_텍스트 = (
        f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
        if used_tools
        else "도구 호출 이벤트 없이 작업을 수행했습니다."
    )
    if used_skills:
        작업_텍스트 += f" | 참고/사용한 스킬: {', '.join(sorted(set(used_skills)))}"
    done_message = message_override or f"서브 에이전트 '{ctx['name']}' 실행이 완료되었습니다."
    _enqueue_status(
        event_queue,
        "task_completed",
        {
            "message": done_message,
            "summary": 작업_텍스트,
            "role": ctx.get("role"),
            "name": ctx.get("name"),
        },
        proc_inst_id,
        task_id,
        "action",
        job_id=sub_job_id,
        exec_ctx=exec_ctx,
        logical_event_type="agent_finished",
        phase="reasoning",
        agent_name=ctx["name"],
    )


async def _emit_subagent_timeline_fallback(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    root_exec_ctx: ExecutionContext | None,
    from_orchestrator_error: bool,
) -> None:
    """Emit task_started/task_completed for each configured subagent (middleware miss path).

    When the orchestrator raises before the success tail runs, the normal fallback is skipped;
    this path ensures the UI still receives a per-subagent timeline for auditing.
    """
    for ctx in subagent_contexts:
        _emit_subagent_task_started(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
        )
    for ctx in subagent_contexts:
        msg_override = None
        if from_orchestrator_error:
            msg_override = (
                f"서브 에이전트 '{ctx['name']}'에 대해 오케스트레이터 오류로 실행이 중단된 것으로 "
                "타임라인을 보정합니다."
            )
        _emit_subagent_task_completed(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            message_override=msg_override,
        )
    await asyncio.sleep(1.2)


async def _reconcile_partial_subagent_task_completed(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    subagent_event_state: dict[str, Any],
    root_exec_ctx: ExecutionContext | None,
    from_orchestrator_error: bool,
) -> None:
    """If middleware emitted task_started for a job but never task_completed, emit completed now."""
    started = subagent_event_state.get("subagent_task_jobs_started")
    completed = subagent_event_state.get("subagent_task_jobs_completed")
    if not isinstance(started, set) or not isinstance(completed, set):
        return
    pending_ctxs: list[dict[str, Any]] = []
    for ctx in subagent_contexts:
        jid = ctx.get("job_id")
        if isinstance(jid, str) and jid in started and jid not in completed:
            pending_ctxs.append(ctx)
    if not pending_ctxs:
        return
    logger.info(
        "서브에이전트 task_completed 누락 보정 | task_id=%s jobs=%s",
        task_id,
        [c.get("job_id") for c in pending_ctxs],
    )
    for ctx in pending_ctxs:
        msg_override = None
        if from_orchestrator_error:
            msg_override = (
                f"서브 에이전트 '{ctx['name']}' 실행 중 상위 오케스트레이터가 실패하여 "
                "task_completed를 보정 발행합니다."
            )
        _emit_subagent_task_completed(
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=ctx.get("exec_ctx") or root_exec_ctx,
            ctx=ctx,
            subagent_tool_log=subagent_tool_log,
            subagent_skill_log=subagent_skill_log,
            message_override=msg_override,
        )
        try:
            completed.add(str(ctx["job_id"]))
        except Exception:
            pass
    await asyncio.sleep(0.6)
