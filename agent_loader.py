"""Load agent definitions from Supabase and build deepagents subagent configs."""

from __future__ import annotations

import copy
import logging
from typing import Any

from database import get_supabase
from mcp_loader import DEFAULT_MCP_SERVER_NAMES

logger = logging.getLogger(__name__)


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


def _parse_csv(value: str | None) -> list[str]:
    """Split a comma-separated string, stripping whitespace."""
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


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


def _build_system_prompt(
    agent_row: dict[str, Any],
    query: str = "",
    tenant_id: str | None = None,
) -> str:
    """Compose a richer, sectioned system prompt for a subagent.

    Skill content is not inlined; subagents load it at runtime via MCP
    (e.g. read_skill_document), and may also use mem0/dmn_rule or other
    tools that have been attached to them.
    """
    parts: list[str] = []

    name = agent_row.get("alias") or agent_row.get("username") or agent_row.get("name") or str(agent_row.get("id"))
    role = agent_row.get("role") or "Unspecified role"
    base_desc = agent_row.get("description") or ""
    persona = (agent_row.get("persona") or "").strip()
    goal = (agent_row.get("goal") or "").strip()

    # 섹션 1: 역할 / 도메인
    parts.append("=== Section 1: Your role and domain ===")
    parts.append(f"- Name: {name}")
    parts.append(f"- Role: {role}")
    if base_desc:
        parts.append(f"- Description: {base_desc.strip()}")
    if persona:
        parts.append(f"- Persona: {persona}")
    if goal:
        parts.append(f"- Goal: {goal}")

    # 섹션 2: 현재 워크아이템 컨텍스트 (요약 형태로)
    q = (query or "").strip()
    if q:
        parts.append("\n=== Section 2: Current work-item context ===")
        parts.append(
            "The following text is the current work-item query (including [Description], "
            "[Instruction], [InputData]). Focus on the parts that are relevant to your "
            "role when deciding how to act:\n"
        )
        parts.append(q)

    # 섹션 3: 테넌트 컨텍스트
    t_id = (tenant_id or "").strip() or "(unknown)"
    parts.append("\n=== Section 3: Tenant context ===")
    parts.append(f"- Current tenant_id: {t_id}")
    parts.append(
        "- When you call any tools or skills that accept a tenant_id argument "
        "(for example, claude-skills tools such as find_helpful_skills), you MUST "
        "pass exactly this tenant_id value and MUST NOT invent or modify it. "
        "Do NOT use values like 'finance_department' unless they are identical "
        "to the current tenant_id shown above."
    )

    # 섹션 4: 스킬 / 도구 사용 규칙
    agent_skills = _parse_csv(agent_row.get("skills"))
    tools = _parse_csv(agent_row.get("tools"))
    parts.append("\n=== Section 4: Skills and tools usage ===")
    if agent_skills:
        names = ", ".join(agent_skills)
        parts.append(f"- Assigned skills: {names}")
        parts.append(
            "- Use read_skill_document (or find_helpful_skills) via the claude-skills MCP "
            "server to load skill documentation when needed, then follow those procedures "
            "when executing your tasks."
        )
    else:
        parts.append("- Assigned skills: (none explicitly assigned)")

    if tools:
        parts.append(f"- Declared tools (by name/server): {', '.join(tools)}")
    else:
        parts.append("- Declared tools: (none explicitly declared; use default MCP tools if available)")

    parts.append(
        "- If mem0 or dmn_rule tools are attached to you, use them to retrieve your "
        "own past knowledge and decision rules before making important decisions."
    )

    # 섹션 5: 협업 및 보고 방식
    parts.append("\n=== Section 5: Collaboration and reporting ===")
    parts.append(
        "- You are part of a multi-agent team orchestrated by a top-level agent. "
        "When delegated a subtask, focus on your domain expertise, use your tools "
        "appropriately, and return clear, concise results that the orchestrator can "
        "combine with outputs from other agents."
    )
    parts.append(
        "- When using tools, prefer higher-level or safer tools first (e.g. read-only "
        "queries, mem0/memento, dmn_rule) before performing irreversible mutations."
    )

    # 섹션 6: 자격 증명 및 placeholder 금지
    parts.append("\n=== Section 6: Credentials and execution policy ===")
    parts.append(
        "- If you need an API key, token, or other credential that is not in the "
        "work-item InputData or context, do NOT respond with only placeholder code or "
        "'insert your API key here'. The workflow will ask the user for the value and "
        "you will receive it to continue; then perform the actual tool/API call."
    )
    parts.append(
        "- Never return only code snippets or example responses when the task requires "
        "real execution (e.g. fetching KOSPI, calling an external API). Perform the "
        "actual tool calls once the required credential is available."
    )

    return "\n".join(parts)


def build_subagent_configs(
    agent_rows: list[dict[str, Any]],
    all_tools: list[Any] | None = None,
    query: str = "",
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build deepagents subagent dicts from DB rows.

    all_tools defaults to [] (no MCP/process-gpt tool loading). query is the todolist
    user request, included in each subagent system prompt. Assigned skill names are
    passed in the prompt; subagents load skill content at runtime via claude-skills MCP
    (read_skill_document, etc.) from the MCP server's mounted volume.
    """
    if all_tools is None:
        all_tools = []
    tools_map: dict[str, Any] = {getattr(t, "name", str(t)): t for t in all_tools}
    server_to_tools: dict[str, list[Any]] = {}
    for t in all_tools:
        server = getattr(t, "_mcp_server", None)
        if server:
            server_to_tools.setdefault(server, []).append(t)

    subagents: list[dict[str, Any]] = []
    for row in agent_rows:
        name = row.get("alias") or row.get("username") or str(row.get("id"))
        description = _build_description(row, name)
        system_prompt = _build_system_prompt(row, query=query, tenant_id=tenant_id)

        agent_tool_names = _parse_csv(row.get("tools"))
        if not agent_tool_names and _parse_csv(row.get("skills")):
            agent_tool_names = list(DEFAULT_MCP_SERVER_NAMES)
        # 각 서브 에이전트는 고유한 MCP 툴 인스턴스를 가져야 한다.
        # 그렇지 않으면 도구 호출 로깅 시 job_id를 서브 에이전트별로 분리할 수 없다.
        filtered_tools: list[Any] = []
        seen_ids: set[int] = set()
        for tn in agent_tool_names:
            if tn in tools_map:
                t = tools_map[tn]
                if id(t) not in seen_ids:
                    filtered_tools.append(copy.copy(t))
                    seen_ids.add(id(t))
            if tn in server_to_tools:
                for t in server_to_tools[tn]:
                    if id(t) not in seen_ids:
                        filtered_tools.append(copy.copy(t))
                        seen_ids.add(id(t))

        if agent_tool_names and not filtered_tools:
            logger.warning(
                "Agent '%s' requested tools %s but none matched loaded tools",
                name,
                agent_tool_names,
            )

        subagent: dict[str, Any] = {
            "name": name,
            "description": description,
            "system_prompt": system_prompt,
            "tools": filtered_tools,
        }

        model = row.get("model")
        if model:
            subagent["model"] = model

        subagents.append(subagent)
        logger.info(
            "Built subagent config: name=%s tools=%d model=%s",
            name,
            len(filtered_tools),
            model or "(inherit)",
        )

    return subagents
