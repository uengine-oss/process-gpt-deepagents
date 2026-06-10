"""Load agent definitions from Supabase and build deepagents subagent configs."""

from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

from app.database import (
    get_supabase,
    get_adhoc_todolist_by_root_proc_inst,
    get_form_def_fields_json,
    _load_proc_def_activity_capabilities
)
from app.agent.backend import tenant_skill_sources_for_middleware
from app.runtime_config import normalize_litellm_slash_model_name
from app.sandbox.tools import get_sandbox_tools
from app.skills.resolution_tools import get_skill_resolution_tools

# Tier 2: MCP tools are attached to every subagent via DEFAULT_MCP_SERVER_NAMES
# (when tenant_mcp includes that server). computer-use remains virtual (sandbox replaces shell).
# NOTE: work-assistant MCP는 "루트(딥 에이전트) 전용"으로 사용하며 서브에이전트에는 기본 부착하지 않는다.
DEFAULT_MCP_SERVER_NAMES: tuple[str, ...] = ()
SKILLS_ROOT_DIR = (os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")
VIRTUAL_TOOL_NAMES: set[str] = {"computer-use"}
ALWAYS_ATTACH_LOCAL_TOOL_NAMES: tuple[str, ...] = ("mem0", "memento", "dmn_rule")
_BLOCKED_SUBAGENT_MCP_SERVERS: set[str] = {"work-assistant"}

logger = logging.getLogger(__name__)

_OPENAI_FORBIDDEN_MESSAGE_NAME_CHARS = re.compile(r'[\s<|\\/>"]+')


def _openai_safe_agent_name(display_name: str, *, fallback: str) -> str:
    """Return OpenAI ChatCompletions-safe token for message/agent names.

    OpenAI requires messages[*].name to match: ^[^\\s<|\\\\/>]+$
    """
    s = (display_name or "").strip()
    if not s:
        s = fallback
    s = _OPENAI_FORBIDDEN_MESSAGE_NAME_CHARS.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or fallback


def _wrap_tools_langchain(tools: list[Any]) -> list[Any]:
    """Match :func:`executor.langchain_tools._ensure_langchain_tools` for freshly built local tools."""
    if not tools:
        return tools
    try:
        from app.executor.langchain_tools import _ensure_langchain_tools

        return _ensure_langchain_tools(tools)
    except Exception:
        logger.debug("LangChain tool wrap skipped for tenant context tools", exc_info=True)
        return tools


def _clone_tool_for_user_id(base_tool: Any, user_id: str) -> Any:
    """Reuse the same underlying client/memory, but bind user_id per subagent.

    IMPORTANT: This avoids per-subagent initialization (e.g. DB connections) while still
    keeping `mem0`/`dmn_rule` scoped to each subagent's user_id.
    """
    t = copy.copy(base_tool)
    uid = (user_id or "").strip()
    # Mem0Tool / DMNRuleTool in processgpt_agent_utils keep user binding in private attrs.
    if uid:
        for attr in ("_user_id", "_namespace", "user_id", "namespace"):
            if hasattr(t, attr):
                try:
                    setattr(t, attr, uid)
                except Exception:
                    pass
    return t


def _sandbox_tool_names() -> set[str]:
    """Return the tool names provided by sandbox_tools.get_sandbox_tools()."""
    try:
        return {getattr(t, "name", type(t).__name__) for t in get_sandbox_tools()}
    except Exception:
        # Defensive: never block subagent config due to sandbox tool introspection.
        return set()


def _baseline_mcp_tools(server_to_tools: dict[str, list[Any]]) -> list[Any]:
    """Match DEFAULT_MCP_SERVER_NAMES to server_to_tools keys case-insensitively."""
    wanted = {b.lower().replace("_", "-") for b in DEFAULT_MCP_SERVER_NAMES}
    out: list[Any] = []
    for skey, tools in server_to_tools.items():
        normalized = str(skey).lower().replace("_", "-")
        if normalized in wanted or any(normalized.endswith(w) for w in wanted):
            out.extend(tools)
    return out


def _skill_resolution_tool_names() -> set[str]:
    try:
        return {getattr(t, "name", type(t).__name__) for t in get_skill_resolution_tools()}
    except Exception:
        return set()


def load_agents_from_db(
    agent_ids: list[str],
    tenant_id: str,
) -> list[dict[str, Any]]:
    """Query the users table for the given agent IDs (is_agent=true)."""
    if not agent_ids:
        return []

    resp = (
        get_supabase()
        .table("users")
        .select("*")
        .in_("id", agent_ids)
        .eq("tenant_id", tenant_id)
        .eq("is_agent", True)
        .execute()
    )
    agents = resp.data or []
    logger.info("Loaded %d agent(s) from DB for tenant=%s", len(agents), tenant_id)
    return agents


def load_tenant_agents_for_chat(
    tenant_id: str,
    *,
    limit: int = 64,
) -> list[dict[str, Any]]:
    """List agent rows (is_agent=true) for SDK chat/SSE when no explicit agent_ids are given."""
    tid = (tenant_id or "").strip()
    if not tid:
        return []
    lim = max(1, min(int(limit), 256))
    resp = (
        get_supabase()
        .table("users")
        .select("*")
        .eq("tenant_id", tid)
        .eq("is_agent", True)
        .limit(lim)
        .execute()
    )
    agents = resp.data or []
    logger.info("Loaded %d chat agent row(s) for tenant=%s", len(agents), tid)
    return agents


def _parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated string, stripping whitespace."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def parse_skills_csv(value: str | None) -> list[str]:
    """Public helper for parsing users.skills CSV values."""
    return _parse_csv(value)


# Skill-folder style slugs in user text (e.g. global-investment-analyzer).
_SKILL_SLUG_IN_TEXT = re.compile(
    r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b",
    flags=re.IGNORECASE,
)


def hinted_skill_slugs_from_query(
    text: str,
    *,
    allowed_skill_names: list[str] | None = None,
) -> list[str]:
    """Extract skill name candidates from the work-item text (kebab-case + allowed list matches).

    Used to steer the skill-discovery subagent and find_helpful_skills allowlists
    when the user names a skill directly or it appears in assigned agent skills.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    lower_blob = raw.lower()

    for name in allowed_skill_names or []:
        n = (name or "").strip()
        if not n:
            continue
        key = n.lower()
        if key in lower_blob or key.replace("_", "-") in lower_blob:
            if n not in seen:
                seen.add(n)
                out.append(n)

    for m in _SKILL_SLUG_IN_TEXT.finditer(raw):
        slug = m.group(0).strip()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)

    return out


def _extract_bracketed_section(text: str, section_name: str) -> str:
    """Extract the body of a bracketed section like [Instruction] from text."""
    raw = (text or "").strip()
    name = (section_name or "").strip()
    if not raw or not name:
        return ""

    pattern = re.compile(
        rf"(?ims)^\[{re.escape(name)}\]\s*(.*?)\s*(?=^\[[^\]]+\]\s*$|\Z)"
    )
    match = pattern.search(raw)
    if not match:
        return ""
    return match.group(1).strip()


def _build_description(agent_row: dict[str, Any], name: str) -> str:
    """Build subagent description from row description, persona, and goal."""
    base = agent_row.get("description") or f"Agent: {name}"
    parts = [base.strip()]
    persona = (agent_row.get("persona") or "").strip()
    if persona:
        parts.append(f"Persona: {persona}")
    goal = (agent_row.get("goal") or "").strip()
    if goal:
        parts.append(f"Goal: {goal}")
    return "\n\n".join(parts)



def build_subagent_configs(
    agent_rows: list[dict[str, Any]],
    all_tools: list[Any] | None = None,
    tenant_id: str | None = None,
    always_attach_local_tool_names: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build deepagents subagent dicts from DB rows.

    all_tools defaults to [] (no MCP/process-gpt tool loading). Assigned skill names are
    passed in the prompt; subagents load skill content at runtime from mounted
    filesystem paths (tenant-scoped).
    """
    if all_tools is None:
        all_tools = []
    local_tool_names = ALWAYS_ATTACH_LOCAL_TOOL_NAMES if always_attach_local_tool_names is None else always_attach_local_tool_names
    # NOTE:
    # 도구(MCP 클라이언트 포함)는 루트에서 1회 로드 후 서브에이전트가 재사용한다.
    # 서브에이전트에는 "요구사항에 맞는 최소 도구셋"만 필터링해서 붙인다.
    all_tools = list(all_tools) + list(get_sandbox_tools()) + list(get_skill_resolution_tools())
    tools_map: dict[str, Any] = {getattr(t, "name", str(t)): t for t in all_tools}
    server_to_tools: dict[str, list[Any]] = {}
    for t in all_tools:
        server = getattr(t, "_mcp_server", None)
        if server:
            server_to_tools.setdefault(server, []).append(t)
    sandbox_names = _sandbox_tool_names()
    skill_resolution_names = _skill_resolution_tool_names()
    skill_sources = tenant_skill_sources_for_middleware(tenant_id)

    subagents: list[dict[str, Any]] = []
    for row in agent_rows:
        display_name = row.get("alias") or row.get("username") or str(row.get("id"))
        # IMPORTANT: deepagents/langchain may propagate agent identifiers into messages[*].name.
        # When routing through LiteLLM->OpenAI, invalid names (spaces) cause 400.
        fallback = str(row.get("id") or "agent")
        name = _openai_safe_agent_name(str(display_name), fallback=fallback)
        description = _build_description(row, display_name)
        declared_skill_names_for_info = _parse_csv(row.get("skills"))
        declared_tool_names_for_info = _parse_csv(row.get("tools"))
        agent_info = {
            "name": str(display_name),
            "role": row.get("role") or "역할 미지정",
            "description": (row.get("description") or "").strip(),
            "persona": (row.get("persona") or "").strip(),
            "goal": (row.get("goal") or "").strip(),
            "skills": declared_skill_names_for_info,
            "declared_tools": [t for t in declared_tool_names_for_info if t not in VIRTUAL_TOOL_NAMES],
            "form_fields": row.get("form_fields_json"),
        }

        agent_type = str(row.get("agent_type") or "").strip()
        is_a2a = agent_type.lower() == "a2a"

        # users.tools 컬럼 해석:
        # - 일반 서브에이전트: MCP 서버명 목록
        # - a2a 서브에이전트: "도구명" 목록 (서버명이 아님)
        declared_tool_names = _parse_csv(row.get("tools"))
        declared_skill_names = _parse_csv(row.get("skills"))
        # computer-use 같은 가상 툴 이름은 무시한다.
        agent_tool_or_server_names = [t for t in declared_tool_names if t not in VIRTUAL_TOOL_NAMES]
        # 기본 정책:
        # - 일반 서브에이전트: users.tools에 명시된 MCP 서버
        # - a2a 서브에이전트: users.tools에 명시된 MCP 서버만 (그 외 모든 도구 부착 금지)
        wanted_servers_norm: set[str] = set()
        baseline_servers = [] if is_a2a else list(DEFAULT_MCP_SERVER_NAMES)
        for s in [*agent_tool_or_server_names, *baseline_servers]:
            key = str(s or "").strip()
            if not key:
                continue
            norm = key.lower().replace("_", "-")
            if norm in _BLOCKED_SUBAGENT_MCP_SERVERS:
                continue
            wanted_servers_norm.add(norm)

        # IMPORTANT:
        # Subagents reuse already-loaded tool instances (root-loaded).
        # job_id separation is handled by executor-side instrumentation/contextvars, not by copying tools.
        filtered_tools: list[Any] = []
        seen_ids: set[int] = set()

        def _server_matches(tool_server: str, wanted_norm: set[str]) -> bool:
            # TaggingSafeToolLoader 가 붙이는 _mcp_server 키는 테넌트 설정에 따라 alias가 있을 수 있어
            # canonical/endswith 매칭으로 흡수한다.
            norm = str(tool_server or "").strip().lower().replace("_", "-")
            if not norm:
                return False
            if norm in wanted_norm:
                return True
            return any(norm.endswith(w) for w in wanted_norm)

        # 1차: MCP 서버 필터링 (users.tools)
        for server_key, tools in server_to_tools.items():
            if not _server_matches(str(server_key), wanted_servers_norm):
                continue
            for t in tools:
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))

        # a2a 에이전트는 users.tools에 있는 도구만 사용한다(기존 동작 유지; variant 분리 금지).
        # 따라서 아래 "기본 도구" 부착 단계(로컬 3개/샌드박스/스킬 해석)는 모두 스킵한다.
        if is_a2a:
            filtered_tools: list[Any] = []
            seen_ids: set[int] = set()
            for tn in agent_tool_or_server_names:
                t = tools_map.get(tn)
                if t is None:
                    continue
                # work-assistant는 루트 전용 정책 유지(서브에이전트에는 차단).
                srv = str(getattr(t, "_mcp_server", "") or "").strip().lower().replace("_", "-")
                if srv and srv in _BLOCKED_SUBAGENT_MCP_SERVERS:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))

            subagent: dict[str, Any] = {
                "name": name,
                "description": _build_description(row, str(display_name)),
                "system_prompt": "",
                "_agent_info": agent_info,
                "tools": filtered_tools,
                "skills": skill_sources,
                "capabilities": {
                    "skills": list(declared_skill_names),
                    "tools": list(declared_tool_names),
                    "agent_type": agent_type,
                    "agent_id": str(row.get("id") or ""),
                },
            }
            model_raw = row.get("model")
            model: str | None = None
            if model_raw is not None and str(model_raw).strip():
                model = normalize_litellm_slash_model_name(str(model_raw).strip())
            if model:
                subagent["model"] = model
                try:
                    caps = subagent.get("capabilities")
                    if isinstance(caps, dict):
                        caps["model"] = model
                except Exception:
                    pass
            subagents.append(subagent)
            logger.info(
                "Built subagent config (a2a strict): name=%s tools=%d model=%s",
                name,
                len(filtered_tools),
                model or "(inherit)",
            )
            continue

        # 2b차: 테넌트 컨텍스트 지식/기억/규칙 도구(mem0/memento/dmn_rule)는
        #      agent_rows[*].tools 에 명시되지 않아도 "항상" 서브에이전트에 부착한다.
        #
        # CRITICAL 원칙:
        # - 로컬 도구는 루트에서 1회 로드된 인스턴스를 재사용한다. (서브에이전트마다 새로 로드 금지)
        # - 서브에이전트별 user_id만 바꿔서 동일 인스턴스(내부 클라이언트/스토어)를 재사용한다.
        agent_uid = str(row.get("id") or "").strip()
        local_added = 0
        if agent_uid:
            for tn in local_tool_names:
                base = tools_map.get(tn)
                if base is None:
                    continue
                # 얕은 복사로 user_id만 바인딩(내부 클라이언트/스토어는 공유)
                t = _clone_tool_for_user_id(base, agent_uid)
                t = _wrap_tools_langchain([t])[0]
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    local_added += 1
        if local_added > 0:
            logger.info(
                "Attached per-subagent tenant context tools %s to agent '%s' (agent_uid=%s, added=%d, total_tools=%d)",
                ", ".join(ALWAYS_ATTACH_LOCAL_TOOL_NAMES),
                name,
                agent_uid,
                local_added,
                len(filtered_tools),
            )

        # 3차: 샌드박스 도구는 "모든" 서브 에이전트에 기본 포함한다.
        sandbox_added = 0
        if sandbox_names:
            for tn in sorted(sandbox_names):
                t = tools_map.get(tn)
                if t is None:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    sandbox_added += 1
        if sandbox_added > 0:
            logger.info(
                "Attached default sandbox tools to agent '%s' (added=%d, total_tools=%d)",
                name,
                sandbox_added,
                len(filtered_tools),
            )

        # 4차: 스킬 해석/볼륨 탐색 도구(find_helpful_skills 등과 별개)도 기본 포함한다.
        sr_added = 0
        if skill_resolution_names:
            for tn in sorted(skill_resolution_names):
                t = tools_map.get(tn)
                if t is None:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(t)
                    seen_ids.add(id(t))
                    sr_added += 1
        if sr_added > 0:
            logger.info(
                "Attached skill resolution tools to agent '%s' (added=%d, total_tools=%d)",
                name,
                sr_added,
                len(filtered_tools),
            )

        subagent: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": "",
            "_agent_info": agent_info,
            "tools": filtered_tools,
            "skills": skill_sources,
            # Capability metadata: routing must NOT depend on agent name/role.
            # Use only DB-stored capabilities (skills/tools/model) + attached tool names.
            "capabilities": {
                "skills": list(declared_skill_names),
                "tools": list(declared_tool_names),
                "agent_type": agent_type,
                "agent_id": str(row.get("id") or ""),
            },
        }

        model_raw = row.get("model")
        model: str | None = None
        if model_raw is not None and str(model_raw).strip():
            model = normalize_litellm_slash_model_name(str(model_raw).strip())
        if model:
            subagent["model"] = model
            try:
                caps = subagent.get("capabilities")
                if isinstance(caps, dict):
                    caps["model"] = model
            except Exception:
                pass

        subagents.append(subagent)
        logger.info(
            "Built subagent config: name=%s tools=%d model=%s",
            name,
            len(filtered_tools),
            model or "(inherit)",
        )

    return subagents


def load_adhoc_agent_rows(
    task_id: str | None = None,
    root_proc_inst_id: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load adhoc todolist items and return agent_rows ready for build_subagent_configs."""
    adhoc_items: list[dict[str, Any]] = []
    if tenant_id and root_proc_inst_id:
        try:
            adhoc_items = get_adhoc_todolist_by_root_proc_inst(
                tenant_id=tenant_id,
                root_proc_inst_id=root_proc_inst_id,
            )
            logger.info("Found %d adhoc work items for proc_inst_id=%s", len(adhoc_items), root_proc_inst_id)
        except Exception:
            logger.exception("Failed to load adhoc work items | tenant_id=%s proc_inst_id=%s", tenant_id, root_proc_inst_id)

    curr_task_id_str = str(task_id).strip() if task_id else ""
    agent_rows: list[dict[str, Any]] = []

    for item in adhoc_items:
        item_id = str(item.get("id") or "").strip()
        if curr_task_id_str and item_id == curr_task_id_str:
            logger.info("Excluding current task item from subagents: id=%s", item_id)
            continue

        activity_id = str(item.get("activity_id") or "").strip()
        proc_def_id = str(item.get("proc_def_id") or "").strip()
        name = (item.get("activity_name") or "").strip() or activity_id or "unnamed_adhoc_subagent"
        desc = (item.get("description") or "").strip() or f"Adhoc subagent for activity: {name}"

        declared_tool_names: list[str] = []
        declared_skill_names: list[str] = []
        if tenant_id and proc_def_id and activity_id:
            try:
                declared_tool_names, declared_skill_names = _load_proc_def_activity_capabilities(
                    tenant_id=tenant_id,
                    proc_def_id=proc_def_id,
                    activity_id=activity_id,
                )
            except Exception:
                logger.exception(
                    "Failed to load capabilities for activity_id=%s | tenant_id=%s proc_def_id=%s",
                    activity_id,
                    tenant_id,
                    proc_def_id,
                )

        # Extract form output schema from tool column (e.g. "formHandler:<form-id>")
        form_fields_json: list[dict[str, Any]] | None = None
        tool_col = str(item.get("tool") or "").strip()
        if tool_col.startswith("formHandler:"):
            form_id = tool_col[len("formHandler:"):].strip()
            if form_id:
                try:
                    form_fields_json = get_form_def_fields_json(form_id)
                    if form_fields_json is not None:
                        logger.info(
                            "Loaded form fields for adhoc subagent | form_id=%s fields=%d item_id=%s",
                            form_id,
                            len(form_fields_json),
                            item_id,
                        )
                    else:
                        logger.warning("form_def not found for form_id=%s item_id=%s", form_id, item_id)
                except Exception:
                    logger.exception("Failed to load form_def fields | form_id=%s item_id=%s", form_id, item_id)

        agent_rows.append({
            "id": item_id,
            "alias": name,
            "role": name,
            "description": desc,
            "goal": _extract_bracketed_section(item.get("query") or "", "Instruction")
            or (item.get("query") or ""),
            "skills": ",".join(declared_skill_names),
            "tools": ",".join(declared_tool_names),
            "agent_type": "agent",
            "proc_inst_id": str(item.get("proc_inst_id") or item.get("root_proc_inst_id") or "").strip(),
            "form_fields_json": form_fields_json,
        })
        logger.info(
            "Queued adhoc agent_row: name=%s tools=%s skills=%s",
            name,
            declared_tool_names,
            declared_skill_names,
        )

    return agent_rows


def build_adhoc_subagent_configs(
    task_id: str | None = None,
    root_proc_inst_id: str | None = None,
    tenant_id: str | None = None,
    all_tools: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Thin wrapper: load_adhoc_agent_rows + build_subagent_configs."""
    agent_rows = load_adhoc_agent_rows(
        task_id=task_id,
        root_proc_inst_id=root_proc_inst_id,
        tenant_id=tenant_id,
    )
    if not agent_rows:
        return []
    return build_subagent_configs(
        agent_rows,
        all_tools=all_tools,
        tenant_id=tenant_id,
        always_attach_local_tool_names=("memento",),
    )
