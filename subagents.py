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
            "- When solving tasks, you MUST first try to use your assigned skills above. "
            "However, for built-in/global skills (like the default Anthropic skills), their "
            "SKILL.md may not be directly readable via read_skill_document even though the "
            "skill itself is loaded. Therefore you MUST discover and access skill documents "
            "through find_helpful_skills before calling read_skill_document."
        )
        parts.append(
            "- For each assigned skill name <name>, follow this workflow IN ORDER:"
        )
        parts.append(
            "  1) Call find_helpful_skills with:"
            "     - tenant_id set to the tenant_id from Section 3,"
            "     - task_description that includes ALL of the following, in a single string:"
            "       * the user's current task (in their original language),"
            "       * the exact skill name <name>,"
            "       * both Korean and English mentions of the skill name if possible"
            "         (e.g. \"xlsx 스킬\", \"xlsx skill\"),"
            "       * a short description of why you want to use that skill."
        )
        parts.append(
            "     - IMPORTANT: You MUST pass allowed_skill_names=[\"<name>\"] for this call. "
            "This ensures tenant-local skills are included in the search scope. "
            "You may include additional skill names in allowed_skill_names only when the "
            "task explicitly requires multiple skills, but you MUST include <name>."
        )
        parts.append(
            "  2) You MUST call find_helpful_skills TWICE per task for each assigned "
            "skill you are trying to use: once with a Korean task_description and once "
            "with an English task_description. If the user's query is in Korean, use it "
            "directly for the Korean version and translate it to English for the second "
            "call while still including the skill name <name> in both versions. If the "
            "query is in English, translate it into natural Korean for the first call and "
            "reuse/refine the English version for the second call, again explicitly "
            "including the skill name <name> in both descriptions."
        )
        parts.append(
            "  3) Merge and deduplicate the returned skill candidates from the Korean and "
            "English calls. Identify the entries whose skill_name or description clearly "
            "matches your assigned skill <name>."
        )
        parts.append(
            "  4) For each such matched skill, inspect its \"Available Documents\" list in "
            "the find_helpful_skills result. You MUST NOT invent filesystem-style paths like "
            "\"/app/skills/{tenant_id}/<name>/SKILL.md\". Instead, call read_skill_document "
            "using exactly the document_path values that appear in the Available Documents "
            "list (for example \"SKILL.md\", \"LICENSE.txt\", or \"scripts/recalc.py\")."
        )
        parts.append(
            "  5) If a SKILL.md is not listed for that skill, still proceed by reading any "
            "other .md, .txt, or relevant script files listed there, and base your behavior "
            "on the guidance found in those documents."
        )
        parts.append(
            "- You MUST NOT assume that SKILL.md lives under any particular on-disk path "
            "or that read_skill_document can open arbitrary filesystem locations. Only use "
            "document_path values that are explicitly listed by find_helpful_skills or "
            "directly documented by that skill."
        )
        parts.append(
            "- You MUST NOT give up after a single failed call to read_skill_document for "
            "an assigned skill. Try other document_path entries from the Available Documents "
            "list (e.g. other .md or .txt files) before concluding that the skill has no "
            "usable documentation."
        )
        parts.append(
            "- Tenant-local skill HTTP fallback (MANDATORY when needed): Some skills may appear "
            "in list_skills with sources like \"/app/skills/{tenant_id}/...\" but still be "
            "unavailable via find_helpful_skills/read_skill_document (e.g. read_skill_document "
            "returns 'skill not found'). In that case you MUST use the HTTP fallback tools:\n"
            "  * list_skill_files_http(skill_name=\"<name>\") to get the file list\n"
            "  * read_skill_file_http(skill_name=\"<name>\", file_path=\"<one of the returned paths>\") "
            "to read file content\n"
            "You MUST first list files, then choose which file(s) to read dynamically based on "
            "the returned list (do NOT hardcode file paths in advance)."
        )
        parts.append(
            "- When a skill's documents include Python scripts (*.py files), you MUST execute "
            "them using the computer-use MCP tool run_shell. Follow this MANDATORY workflow:\n"
            "  1) First, read the skill's SKILL.md (or other documentation) via read_skill_document.\n"
            "  2) Identify ALL .py files mentioned or listed in the skill documentation.\n"
            "  3) Read the actual .py file content using read_skill_document with the .py file path "
            "(e.g. document_path=\"scripts/fetch_kospi.py\" or similar).\n"
            "  4) Determine the FULL execution path of the .py file. If the skill is located at "
            "\"/app/skills/{tenant_id}/{skill_name}/\", then the .py file path would be "
            "\"/app/skills/{tenant_id}/{skill_name}/{relative_path_to_py}\".\n"
            "  5) Execute the script using run_shell with: python /full/path/to/script.py [arguments]\n"
            "  6) Pass required parameters (API tokens, dates, etc.) as command-line arguments or "
            "environment variables. For example:\n"
            "     - python /app/skills/tenant/skill/script.py --api-key=TOKEN --date=2024-01-01\n"
            "     - Or: API_KEY=TOKEN python /app/skills/tenant/skill/script.py\n"
            "  7) NEVER just show code snippets or explain how to run—ACTUALLY execute with run_shell.\n"
            "  8) If the script requires input data from InputData (e.g. api_token, access_token), "
            "extract those values and pass them to the script execution command."
        )
        parts.append(
            "- Package installation: If the skill's .py script requires additional Python packages, "
            "install them BEFORE running the script. Follow this order:\n"
            "  1) First, try: pip install --user <package_name>\n"
            "     (This installs to user directory and doesn't require root permissions)\n"
            "  2) If that fails, try: pip install <package_name> (may work in some environments)\n"
            "  3) For system packages (apt), if 'apt install' fails due to permission denied:\n"
            "     - Do NOT give up. Try alternative approaches:\n"
            "     - Use pip to install Python equivalents when possible\n"
            "     - Check if the package is already available under a different name\n"
            "     - Use 'python -m pip install' as an alternative\n"
            "  4) After installation, proceed to run the actual skill script.\n"
            "  5) IMPORTANT: Package installation is just preparation. The main goal is to "
            "EXECUTE the skill's .py file and return the actual result."
        )
        parts.append(
            "- When executing a skill's .py script, if the script path from the skill document is "
            "relative (e.g. 'scripts/main.py' or just 'main.py'), construct the full path by prepending "
            "the skill's base directory. Use list_skill_files_http to discover actual file paths "
            "if needed, then execute with the full absolute path."
        )
        parts.append(
            "- IMPORTANT: Python code may be embedded inside .md files (e.g., SKILL.md) as code blocks "
            "(```python ... ```). When you find Python code in a .md file, you MUST still execute it:\n"
            "  1) Extract the Python code from the markdown code block.\n"
            "  2) Execute it using one of these methods:\n"
            "     a) Save to a temp file and run: echo '<code>' > /tmp/skill_script.py && python /tmp/skill_script.py [args]\n"
            "     b) Use python -c: python -c '<code>' (for short scripts, escape quotes properly)\n"
            "     c) Use heredoc: python << 'EOF'\n<code>\nEOF\n"
            "  3) Pass required arguments (API tokens, dates, etc.) as before.\n"
            "  4) The code in .md files is meant to be executed, not just read or explained.\n"
            "  5) If there are multiple code blocks, identify the main executable one (usually the "
            "complete script, not just snippets or examples)."
        )
    else:
        parts.append("- Assigned skills: (none explicitly assigned)")
        parts.append(
            "- Because you have no assigned skills, you SHOULD start by calling "
            "find_helpful_skills with an appropriate task_description and tenant_id "
            "to discover relevant skills before attempting any complex domain work. "
            "Make sure the task_description includes both the user's request and any "
            "relevant skill names or keywords in both Korean and English when possible."
        )
        parts.append(
            "- In all cases when you use find_helpful_skills, you MUST call it twice per "
            "task: first with a Korean task_description and then with an English "
            "task_description for the same task. Use Korean if the user's request is in "
            "Korean (or translate the request into Korean), and create or refine an English "
            "version for the second call. In both calls, explicitly mention any relevant "
            "skill names (for example \"xlsx 스킬\", \"xlsx skill\") inside the "
            "task_description so that the search can match skills whose names appear only "
            "in English or only in Korean. Combine the results from both calls so that skills "
            "whose descriptions are written only in one language can still be discovered."
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
        "- When you need to run Python code, use the computer-use MCP tool run_shell. IMPORTANT:\n"
        "  * Do NOT show or suggest the command—ACTUALLY call run_shell immediately.\n"
        "  * If the script requires packages not yet installed, install them first using "
        "'pip install --user <package>' then run the script.\n"
        "  * For .py files: Use the FULL absolute path (e.g., python /app/skills/.../main.py --token=XXX)\n"
        "  * For code in .md files: Extract the code and run via:\n"
        "    - Temp file: echo '...' > /tmp/script.py && python /tmp/script.py [args]\n"
        "    - Or: python -c '...' (short scripts)\n"
        "    - Or: python << 'EOF'\\n<code>\\nEOF (multiline)\n"
        "  * Pass all required parameters (API keys, tokens, dates) as arguments or env vars.\n"
        "  * If a command fails due to permission issues, try alternative approaches "
        "(e.g., pip install --user instead of apt install).\n"
        "  * If script output contains the answer, extract and report it to the orchestrator."
    )
    parts.append(
        "- When the orchestrator delegates a specific step to you (for example with an "
        "instruction derived from a plan step like '## 1. 단계 제목' or mentioning specific "
        "tools/skills to use), you MUST treat that step description as your primary task "
        "and actively use the attached MCP/skill tools that match the requested tools/skills "
        "instead of answering with text only."
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
        "- FIRST, check the work-item InputData (Section 2) for any API keys, tokens, or "
        "credentials. Common field names include: api_token, access_token, api_key, token, "
        "apiKey, accessToken, etc. If found, USE THEM DIRECTLY when executing skill scripts."
    )
    parts.append(
        "- When executing a skill's .py script that requires credentials, pass the token "
        "from InputData as a command-line argument or environment variable. Example:\n"
        "  * If InputData contains {'api_token': 'ABC123'}, run:\n"
        "    python /path/to/script.py --api-token=ABC123\n"
        "  * Or: API_TOKEN=ABC123 python /path/to/script.py"
    )
    parts.append(
        "- If you need an API key, token, or other credential that is NOT in the "
        "work-item InputData or context, ONLY THEN request user intervention "
        "(e.g. via request_human_input or the tool that signals requires_human)."
    )
    parts.append(
        "- Whenever any information required to proceed is truly missing (not found in "
        "InputData), you MUST request user intervention instead of guessing or inventing "
        "values. Do not assume defaults for credentials or sensitive data."
    )
    parts.append(
        "- CRITICAL: Never return only code snippets or example responses when the task "
        "requires real execution (e.g. fetching KOSPI, calling an external API). You MUST:\n"
        "  1) Find the skill's .py script using find_helpful_skills + list_skill_files_http\n"
        "  2) Read the script to understand its usage and required arguments\n"
        "  3) Extract credentials from InputData\n"
        "  4) Execute the script with run_shell, passing all required arguments\n"
        "  5) Return the ACTUAL execution result, not instructions on how to execute"
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

