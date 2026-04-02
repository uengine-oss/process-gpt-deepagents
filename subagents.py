"""Load agent definitions from Supabase and build deepagents subagent configs."""

from __future__ import annotations

import copy
import logging
import os
import re
from typing import Any

from database import get_supabase
from execution_context import format_execution_clock_section
from sandbox_tools import get_sandbox_tools
from skill_resolution_tools import get_skill_resolution_tools

# Tier 2: claude-skills MCP tools are attached to every subagent via DEFAULT_MCP_SERVER_NAMES
# (when tenant_mcp includes that server). computer-use remains virtual (sandbox replaces shell).
DEFAULT_MCP_SERVER_NAMES: tuple[str, ...] = ("claude-skills",)
SKILLS_ROOT_DIR = (os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")
VIRTUAL_TOOL_NAMES: set[str] = {"computer-use"}
ALWAYS_ATTACH_LOCAL_TOOL_NAMES: tuple[str, ...] = ("mem0", "memento", "dmn_rule")

logger = logging.getLogger(__name__)


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

    Skill content is not inlined; subagents load it at runtime from mounted
    filesystem paths (tenant-aware), and may also use mem0/dmn_rule or other
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

    parts.append("\n" + format_execution_clock_section())

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
    parts.append("- Use this tenant_id to resolve skill paths under the mounted skills root.")

    # 섹션 4: 스킬 / 도구 사용 규칙
    agent_skills = _parse_csv(agent_row.get("skills"))
    tools = _parse_csv(agent_row.get("tools"))
    parts.append("\n=== Section 4: Skills and tools usage ===")
    parts.append(
        "\n=== Section 4a0: Built-in knowledge tools (use when they add value; not every task needs them) ===\n"
        "Your tool list may include local tools that are NOT generic MCP — treat them by purpose:\n"
        "- **mem0** — Long-term personalized memory: user/org preferences, standing facts, credentials or secrets "
        "the user chose to store, prior task notes, and anything that should persist across runs.\n"
        "  Use mem0 when: the task could benefit from personalization; [Instruction]/[Description]/user text "
        "mentions memory, prior runs, preferences, or stored keys; or you are about to ask the human for "
        "information that might already live in memory.\n"
        "  Skip mem0 when: the task is purely stateless/format-only and nothing suggests stored context.\n"
        "- **dmn_rule** — Business rules & decision logic: policies, thresholds, eligibility, DMN-style rules "
        "that must govern *what* to do or *whether* an action is allowed.\n"
        "  Use dmn_rule when: the request involves compliance, approvals, routing rules, or any "
        "structured decision that should not be invented from general knowledge.\n"
        "  Skip when: no rule-governed decision is involved.\n"
        "- **memento** — RAG / document retrieval over tenant-linked corpora: finding relevant internal "
        "documents, passages, or evidence to ground answers.\n"
        "  Use memento when: you need supporting documents, similar past cases, or authoritative text "
        "that may exist outside the current work-item text.\n"
        "  Skip when: everything needed is already in [Instruction]/[InputData] and no retrieval is implied.\n"
        "\n"
        "Ordering (soft, not mandatory): when you *do* use these tools, prefer calling them **before** "
        "heavy MCP or sandbox work so their outputs can steer parameters, credentials, and risk choices. "
        "If a tool returns nothing useful, proceed without looping endlessly.\n"
        "\n"
        "**Human-in-the-loop (HITL) and credentials / file uploads:**\n"
        "- Do **not** jump to `request_human_input` (or similar) for API keys, tokens, or credential files "
        "just because [Instruction] says \"upload a file\" or \"provide a key\", if mem0 (or memento for "
        "documented secrets) might already hold them — **query the relevant tools first**, then decide.\n"
        "- Only after you have **reasonably tried** attached mem0 / dmn_rule / memento (when applicable to "
        "the task) and still lack a required secret or file, escalate to human intervention with a clear, "
        "minimal question.\n"
        "- Summarize what you retrieved from memory/rule/RAG tools and how it changed your plan; if nothing "
        "was found, say so briefly and continue.\n"
    )
    parts.append(
        "\n=== Section 4a: Three-tier skill resolution (STRICT ORDER) ===\n"
        "- At the start of non-trivial tasks, use write_todos with items that include: "
        "`tier1_volume_fs`, `tier2_claude_skills_mcp` (only if Tier 1 fails), "
        "`tier3_sandbox` (only if Tier 2 fails and execution is required). "
        "- Tier 1 — Volume + filesystem: Call skill_volume_probe(tenant_id, skill_names_csv) "
        "with Section 3 tenant_id and your assigned skill folder names (comma-separated). "
        "That tool emits server-side logs automatically; still call "
        "report_skill_resolution_tier(1, \"started\"|\"success\"|\"failed\", short detail) "
        "so tier transitions appear in the same stream as Tier 2/3. "
        "Then use built-in ls/read_file/glob on paths under the skills root; prefer "
        "sandbox_copy_mounted_skill to mirror files into the sandbox before running scripts.\n"
        "- Tier 2 — claude-skills MCP: ONLY after Tier 1 shows missing SKILL.md / empty copy / "
        "skill_volume_probe.tier1_any_hit is false for what you need. "
        "Use the MCP tools from the claude-skills server (e.g. search/list/read similar skills). "
        "Call report_skill_resolution_tier(2, \"started\"|\"success\"|\"failed\", short detail) when entering/leaving this tier.\n"
        "- Tier 3 — Sandbox code: ONLY when Tier 2 did not produce a runnable skill AND the task "
        "requires code execution. Follow Section 4c. "
        "Call report_skill_resolution_tier(3, ... ) similarly.\n"
        "- Do not jump to Tier 3 while claude-skills MCP tools are available and you have not tried Tier 2.\n"
    )
    if agent_skills:
        names = ", ".join(agent_skills)
        parts.append(f"- Assigned skills: {names}")
        parts.append(
            f"- Skills root directory: {SKILLS_ROOT_DIR} "
            "(mounted docker volume)."
        )
        parts.append(
            "- Default document path for each skill follows this template (do NOT invent paths): "
            f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/{{skill_name}}/SKILL.md\"."
        )
        for s in agent_skills:
            parts.append(
                f"  * {s} → primary document_path: "
                f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/"
                f'{s}'
                "/SKILL.md\""
            )
        parts.append(
            "- When solving tasks, you MUST first read assigned skill documents using "
            "filesystem tools (ls/read_file)."
        )
        parts.append(
            "  1) Check directory existence with ls on "
            f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/<name>/\"."
        )
        parts.append(
            "  2) Read the primary document with read_file on "
            f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/<name>/SKILL.md\"."
        )
        parts.append(
            "  3) If missing, inspect the same directory for alternative docs/scripts and continue."
        )
        parts.append(
            "- You MUST NOT give up after one failed read. Explore neighboring files "
            "in the skill directory and proceed with best available instructions."
        )
        parts.append(
            "- When a skill's documents include Python scripts (*.py files), you MUST execute "
            "them in sandbox. Read SKILL.md, locate referenced .py files, then run them "
            "with sandbox_run_shell—do not only return code or instructions."
        )
        parts.append(
            "- IMPORTANT: Do NOT blindly run skill scripts as-is by default. Treat skill scripts as "
            "reference modules/templates first. Build a task-specific execution script from "
            "the user request + InputData + skill docs, then execute that synthesized script."
        )
        parts.append(
            "- Direct execution of an original skill script is allowed only when ALL are true: "
            "(a) the script's declared purpose exactly matches the request, "
            "(b) required inputs are already present in InputData/context, and "
            "(c) no task-specific parameter remapping is needed."
        )
        parts.append(
            "- Mandatory materialization + execution path (do not skip):\n"
            "  1) sandbox_create → note workspace_id.\n"
            "  2) sandbox_copy_mounted_skill with the same tenant_id as Section 3 and each "
            "assigned skill folder name (copies host-mounted files into the workspace).\n"
            "  3) Prefer sandbox_write_and_run (or write_files + run_shell) to execute a synthesized script "
            "that imports/reuses skill logic with task-specific parameters. "
            "Run original skill scripts directly only when they exactly match the requested task and inputs.\n"
            "  4) sandbox_read_workspace_files for key outputs if needed.\n"
            "  Your final message to the orchestrator MUST include actual command outputs "
            "(stdout/stderr snippets), not only generic API usage examples."
        )
        parts.append(
            "- If sandbox_copy_mounted_skill reports source_missing or source_not_dir, or "
            "copied_files is empty, you MUST use sandbox_write_and_run (preferred) or "
            "sandbox_write_files + sandbox_run_shell: synthesize a complete script from the "
            "work-item Instruction + InputData, run it, and return stdout/stderr. "
            "Never stop at placeholder code or setup prose only."
        )
    else:
        parts.append("- Assigned skills: (none explicitly assigned)")
        parts.append(
            "- Because you have no assigned skills, inspect tenant skill directories under "
            f"\"{SKILLS_ROOT_DIR}/{{tenant_id}}/\" and choose the most relevant one."
        )
        parts.append(
            "- If nothing usable is on disk, use sandbox_write_and_run to implement the task "
            "from Instruction + InputData without waiting for skill files."
        )

    parts.append(
        "\n=== Section 4c: Tier 3 — sandbox when FS + MCP did not yield a runnable skill ===\n"
        "Use Tier 3 only after Tier 1 (volume/FS) and Tier 2 (claude-skills MCP) are exhausted or "
        "MCP is unavailable, and the task still requires code execution.\n"
        "Typical triggers: ls/read_file failed for expected paths; "
        "sandbox_copy_mounted_skill returns source_missing, source_not_dir, or copied_files=[]; "
        "or MCP returned no executable skill.\n"
        "Order (do not skip step 3):\n"
        "  1) sandbox_create if you do not yet have workspace_id.\n"
        "  2) Write one complete Python (or shell) program that performs the requested action "
        "using data from the work-item; embed credentials from InputData when provided. "
        "This script should be task-specific (synthesized), not a blind copy of skill scripts.\n"
        "  3) sandbox_write_and_run(relative_path like \"fallback_main.py\", content=...) "
        "— or equivalent sandbox_write_files + sandbox_run_shell.\n"
        "  4) Return exit_code, stdout, stderr to the orchestrator (trim if very long). "
        "On failure, still report tool output — do not replace it with generic setup instructions only.\n"
        "Ending with \"skill files are missing\" without having run step 3 is forbidden.\n"
    )

    # NOTE: Avoid domain-specific hardcoding here.
    # Keep subagent prompts general; domain tactics should emerge from tools/skills + execution.
    parts.append(
        "\n=== Section 4d: Execution-first & discovery-first (general) ===\n"
        "- Prefer executing in sandbox over returning instructions.\n"
        "- Execution policy: synthesized task-specific scripts first; direct execution of original "
        "skill scripts is secondary and allowed only when the script precisely matches the request.\n"
        "- If an external resource identifier is missing (file id, document id, URL, etc.),\n"
        "  first attempt discovery using available tools/APIs in sandbox (search/list candidates, probe),\n"
        "  and only ask the user when you have exhausted reasonable discovery attempts.\n"
        "- Never end with placeholders (e.g. \"replace YOUR_* and run\"). Return concrete outputs\n"
        "  (stdout/stderr, exit codes, extracted data) from real execution attempts.\n"
        "- For lookup/investigation tasks using provided credentials (API key/token), you MUST execute\n"
        "  at least one real API request and include evidence fields in your final report:\n"
        "  source, endpoint_or_series, as_of, value, unit (or explain which field is unavailable).\n"
        "- If a skill script produces wrong/empty data, do not stop; apply minimal corrective rerun\n"
        "  (e.g. alternate stat code/params from the same API) and report both failure and final success.\n"
    )

    if tools:
        parts.append(f"- Declared tools (by name/server): {', '.join(tools)}")
    else:
        parts.append("- Declared tools: (none explicitly declared; rely on built-in and sandbox tools)")

    parts.append(
        "- When mem0, memento, or dmn_rule are attached, treat them as first-class tools: use them when "
        "Section 4a0 applies, so skill selection and execution reflect stored knowledge and rules."
    )
    parts.append(
        "- Keep all skill file reads inside the tenant root directory and never access "
        "other tenant paths."
    )
    parts.append(
        "- Prefer sandbox tools for generated code execution: create a sandbox workspace, "
        "write files, run shell commands in that sandbox, collect artifacts, then cleanup."
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
        "- **Human intervention is the last resort.** Before any `request_human_input`, "
        "`requires_human: true`, or upload request, work through this checklist in order "
        "(skip steps for tools you do not have): "
        "(1) mem0 / memento / dmn_rule per Section 4a0; "
        "(2) skill FS + Tier 1–2 discovery; "
        "(3) relevant MCP tools (list/search/read/call); "
        "(4) sandbox probes or scripted discovery; "
        "(5) at least one retry with parameters inferred from errors or docs. "
        "Only then escalate to the human with a single, minimal question.\n"
    )
    parts.append(
        "- If you need an API key, token, or other credential that is not in the "
        "work-item InputData or context, follow Section 4a0 first: query **mem0** (and **memento** "
        "when the secret may live in stored documents). Do NOT respond with only placeholder code or "
        "'insert your API key here'. Only if retrieval does not yield what you need, request user "
        "intervention (e.g. via request_human_input or the tool that signals requires_human); "
        "then perform the actual tool/API call."
    )
    parts.append(
        "- Whenever any information required to proceed is missing (API keys, tokens, "
        "file paths, parameters, or other inputs), separate credentials/sensitive data "
        "from non-sensitive query parameters. Prefer resolving credentials via mem0/memento "
        "(Section 4a0) before asking the human. Non-sensitive parameters should be resolved by "
        "autonomous discovery (skill docs, API docs/spec, list/search endpoints, prior tool outputs) "
        "before asking the user."
    )
    parts.append(
        "- Do NOT ask human input for optional query filters (date range, pagination, region, etc.) "
        "when a sensible default allows immediate execution. Ask only when a truly required value "
        "is missing and no safe default exists."
    )
    parts.append(
        "- If an API responds with missing-required-parameter errors (e.g. ERROR-100), do not "
        "immediately ask the human. Infer required fields from docs/error messages, retry with "
        "corrected parameters, and only escalate when at least one reasonable correction attempt fails."
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
    passed in the prompt; subagents load skill content at runtime from mounted
    filesystem paths (tenant-scoped).
    """
    if all_tools is None:
        all_tools = []
    all_tools = list(all_tools) + list(get_sandbox_tools()) + list(get_skill_resolution_tools())
    tools_map: dict[str, Any] = {getattr(t, "name", str(t)): t for t in all_tools}
    sandbox_names = _sandbox_tool_names()
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

        declared_tool_names = _parse_csv(row.get("tools"))
        # computer-use 는 샌드박스로 대체; claude-skills 는 MCP(Tier2) + FS(Tier1) 병행.
        agent_tool_names = [t for t in declared_tool_names if t not in VIRTUAL_TOOL_NAMES]
        has_skills = bool(_parse_csv(row.get("skills")))
        if not agent_tool_names and has_skills:
            # 스킬만 지정된 에이전트도 sandbox/built-in 도구 중심으로 동작한다.
            agent_tool_names = []

        # 각 서브 에이전트는 고유한 툴 인스턴스를 가져야 한다.
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
        #     기본 보조 서버 툴을 fallback 으로 붙인다(현재 기본 보조 서버 없음).
        if not filtered_tools:
            fallback_tools: list[Any] = []
            for t in _baseline_mcp_tools(server_to_tools):
                if id(t) not in seen_ids:
                    fallback_tools.append(copy.copy(t))
                    seen_ids.add(id(t))
            if fallback_tools:
                logger.warning(
                    "Agent '%s' had no resolved tools (requested=%s); "
                    "attaching default fallback tools from %s instead",
                    name,
                    agent_tool_names or "[]",
                    ", ".join(DEFAULT_MCP_SERVER_NAMES),
                )
                filtered_tools.extend(fallback_tools)
            elif agent_tool_names:
                # fallback 도 실패한 경우에만 경고를 남긴다.
                logger.warning(
                    "Agent '%s' requested tools %s but none matched loaded tools/default fallback servers",
                    name,
                    agent_tool_names,
                )

        # 3차: 기본 보조 서버의 툴은
        #    "모든" 서브 에이전트에 항상 포함되도록 보장한다.
        #    (이미 포함된 경우에는 seen_ids 체크로 중복을 방지한다.)
        baseline_added = 0
        for t in _baseline_mcp_tools(server_to_tools):
            if id(t) not in seen_ids:
                filtered_tools.append(copy.copy(t))
                seen_ids.add(id(t))
                baseline_added += 1
        if baseline_added > 0:
            logger.info(
                "Attached default fallback tools %s to agent '%s' (added=%d, total_tools=%d)",
                ", ".join(DEFAULT_MCP_SERVER_NAMES),
                name,
                baseline_added,
                len(filtered_tools),
            )

        # 3b차: 테넌트 컨텍스트 지식/기억/규칙 도구(mem0/memento/dmn_rule)는
        #      agent_rows[*].tools 에 명시되지 않아도 "항상" 서브에이전트에 부착한다.
        #      이유: 오케스트레이터는 도메인 작업을 subagent에 위임해야 하고,
        #      이 도구들은 루트(tool=0) 경로로는 호출되지 않기 때문이다.
        local_added = 0
        for tn in ALWAYS_ATTACH_LOCAL_TOOL_NAMES:
            t = tools_map.get(tn)
            if t is None:
                continue
            if id(t) not in seen_ids:
                filtered_tools.append(copy.copy(t))
                seen_ids.add(id(t))
                local_added += 1
        if local_added > 0:
            logger.info(
                "Attached tenant context tools %s to agent '%s' (added=%d, total_tools=%d)",
                ", ".join(ALWAYS_ATTACH_LOCAL_TOOL_NAMES),
                name,
                local_added,
                len(filtered_tools),
            )

        # 4차: 샌드박스 실행 도구는 "모든" 서브 에이전트에 기본 포함한다.
        # 결과 품질(실제 실행) 보장을 위해 tools 선언 여부와 무관하게 항상 붙인다.
        sandbox_added = 0
        if sandbox_names:
            for tn in sorted(sandbox_names):
                t = tools_map.get(tn)
                if t is None:
                    continue
                if id(t) not in seen_ids:
                    filtered_tools.append(copy.copy(t))
                    seen_ids.add(id(t))
                    sandbox_added += 1
        if sandbox_added > 0:
            logger.info(
                "Attached default sandbox tools to agent '%s' (added=%d, total_tools=%d)",
                name,
                sandbox_added,
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
