"""Load agent definitions from Supabase and build deepagents subagent configs."""

from __future__ import annotations

import copy
import logging
from typing import Any

from database import get_supabase

# 기본 MCP 서버 이름 (SafeToolLoader 기본 설정과 동일하게 유지)
DEFAULT_MCP_SERVER_NAMES: tuple[str, str] = ("claude-skills", "computer-use")

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
            "- Default document path for each skill follows this template (do NOT invent paths): "
            "\"/app/skills/{tenant_id}/{skill_name}/SKILL.md\"."
        )
        for s in agent_skills:
            parts.append(
                f"  * {s} → primary document_path: "
                "\"/app/skills/{tenant_id}/"
                f'{s}'
                "/SKILL.md\""
            )
        parts.append(
            "- When solving tasks, you MUST first use your assigned skills above. "
            "For each assigned skill, call read_skill_document with all of the following "
            "fallbacks IN ORDER (stop as soon as one returns a non-empty result):"
        )
        parts.append(
            "  1) read_skill_document("
            "skill_name=\"<name>\", "
            "document_path=\"/app/skills/{tenant_id}/<name>/SKILL.md\", "
            "include_base64=true)"
        )
        parts.append(
            "  2) If (1) returns no documents or a not-found error, retry with a wildcard "
            "pattern that matches the full path such as "
            "document_path=\"*/<name>/SKILL.md\"."
        )
        parts.append(
            "  3) If (2) also fails, finally retry with a simple relative path "
            "document_path=\"SKILL.md\"."
        )
        parts.append(
            "- You MUST NOT give up after a single failed call to read_skill_document for "
            "an assigned skill. Always apply the above fallback sequence before concluding "
            "that the skill has no readable documents."
        )
        parts.append(
            "- Only when none of your assigned skills are suitable may you call "
            "find_helpful_skills to discover additional skills."
        )
        parts.append(
            "- You MUST NOT start a task by calling find_helpful_skills when you already "
            "have assigned skills. Always attempt to use your own skills first (by reading "
            "their SKILL.md and following their guidance); treat find_helpful_skills purely "
            "as a fallback when your assigned skills are insufficient to complete the work."
        )
        parts.append(
            "- Whenever you do call find_helpful_skills (fallback case), you MUST perform "
            "two separate searches for the same task: first with a Korean task_description "
            "and then with an English task_description. If the user's query is in Korean, "
            "use it directly for the Korean call and translate it to English for the second "
            "call. If the query is in English, translate it into natural Korean for the first "
            "call, then reuse or refine the English version for the second call. Merge and "
            "deduplicate the returned skill candidates from both calls before deciding which "
            "skills to use."
        )
        parts.append(
            "- When a skill's documents include Python scripts (*.py files), you MUST execute "
            "them using the computer-use MCP tool run_shell. First read the assigned skill "
            "(e.g. global-investment-analyzer via read_skill_document with document_path "
            "\"SKILL.md\"); if the skill lists or references .py "
            "files, run those scripts with run_shell—do not only return code or instructions. "
            "Actually invoke run_shell to execute the Python file (e.g. python <path from skill>)."
        )
    else:
        parts.append("- Assigned skills: (none explicitly assigned)")
        parts.append(
            "- Because you have no assigned skills, you SHOULD start by calling "
            "find_helpful_skills with an appropriate task_description and tenant_id "
            "to discover relevant skills before attempting any complex domain work."
        )
        parts.append(
            "- In all cases when you use find_helpful_skills, you MUST call it twice per "
            "task: first with a Korean task_description and then with an English "
            "task_description for the same task. Use Korean if the user's request is in "
            "Korean (or translate the request into Korean), and create or refine an English "
            "version for the second call. Combine the results from both calls so that skills "
            "whose descriptions are written only in Korean or only in English can still be "
            "discovered."
        )

    if tools:
        parts.append(f"- Declared tools (by name/server): {', '.join(tools)}")
    else:
        parts.append("- Declared tools: (none explicitly declared; use default MCP tools if available)")

    parts.append(
        "- If mem0 or dmn_rule tools are attached to you, use them to retrieve your "
        "own past knowledge and decision rules before making important decisions."
    )
    parts.append(
        "- When you need to run a Python script (*.py), use the computer-use MCP tool "
        "run_shell to execute it (e.g. run_shell with the appropriate command). Do not "
        "only show or suggest the command—actually call run_shell."
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

    # 섹션 6: 자격 증명 및 사용자 개입 요청
    parts.append("\n=== Section 6: Credentials and user intervention ===")
    parts.append(
        "- If you need an API key, token, or other credential that is not in the "
        "work-item InputData or context, do NOT respond with only placeholder code or "
        "'insert your API key here'. Request user intervention (e.g. via request_human_input "
        "or the tool that signals requires_human); the workflow will ask the user for the "
        "value and you will receive it to continue; then perform the actual tool/API call."
    )
    parts.append(
        "- Whenever any information required to proceed is missing (API keys, tokens, "
        "file paths, parameters, or other inputs), you MUST request user intervention "
        "instead of guessing or inventing values. Do not assume defaults for credentials "
        "or sensitive data."
    )
    parts.append(
        "- Never return only code snippets or example responses when the task requires "
        "real execution (e.g. fetching KOSPI, calling an external API). Perform the "
        "actual tool calls once the required credential or information is available."
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
        has_skills = bool(_parse_csv(row.get("skills")))
        if not agent_tool_names and has_skills:
            # 스킬만 지정된 에이전트는 기본 MCP 서버(claude-skills, computer-use)를 우선적으로 사용하도록 한다.
            agent_tool_names = list(DEFAULT_MCP_SERVER_NAMES)

        # 각 서브 에이전트는 고유한 MCP 툴 인스턴스를 가져야 한다.
        # 그렇지 않으면 도구 호출 로깅 시 job_id를 서브 에이전트별로 분리할 수 없다.
        filtered_tools: list[Any] = []
        seen_ids: set[int] = set()

        # 1차: 에이전트가 선언한 tools 기준으로 매핑
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

        # 2차: 어떤 이유로든 filtered_tools 가 비어 있으면,
        #     기본 MCP 서버(claude-skills, computer-use)의 툴을 fallback 으로 붙인다.
        if not filtered_tools:
            fallback_tools: list[Any] = []
            for server_name in DEFAULT_MCP_SERVER_NAMES:
                for t in server_to_tools.get(server_name, []):
                    if id(t) not in seen_ids:
                        fallback_tools.append(copy.copy(t))
                        seen_ids.add(id(t))
            if fallback_tools:
                logger.warning(
                    "Agent '%s' had no resolved tools (requested=%s); "
                    "attaching default MCP tools from %s instead",
                    name,
                    agent_tool_names or "[]",
                    ", ".join(DEFAULT_MCP_SERVER_NAMES),
                )
                filtered_tools.extend(fallback_tools)
            elif agent_tool_names:
                # fallback 도 실패한 경우에만 경고를 남긴다.
                logger.warning(
                    "Agent '%s' requested tools %s but none matched loaded tools or default MCP servers",
                    name,
                    agent_tool_names,
                )

        # 3차: 기본 MCP 서버(claude-skills, computer-use)의 툴은
        #    "모든" 서브 에이전트에 항상 포함되도록 보장한다.
        #    (이미 포함된 경우에는 seen_ids 체크로 중복을 방지한다.)
        baseline_added = 0
        for server_name in DEFAULT_MCP_SERVER_NAMES:
            for t in server_to_tools.get(server_name, []):
                if id(t) not in seen_ids:
                    filtered_tools.append(copy.copy(t))
                    seen_ids.add(id(t))
                    baseline_added += 1
        if baseline_added > 0:
            logger.info(
                "Attached default MCP tools %s to agent '%s' (added=%d, total_tools=%d)",
                ", ".join(DEFAULT_MCP_SERVER_NAMES),
                name,
                baseline_added,
                len(filtered_tools),
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

