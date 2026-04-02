"""Built-in 스킬 탐색 도우미 subagent: discovery-only prompt + tool wiring."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from execution_context import format_execution_clock_section
from sandbox_tools import get_sandbox_tools
from skill_resolution_tools import get_skill_resolution_tools
from subagents import (
    ALWAYS_ATTACH_LOCAL_TOOL_NAMES,
    SKILLS_ROOT_DIR,
    _baseline_mcp_tools,
    _sandbox_tool_names,
    _skill_resolution_tool_names,
)

logger = logging.getLogger(__name__)

SKILL_DISCOVERY_SUBAGENT_NAME = "스킬 탐색 도우미"
SKILL_DISCOVERY_BUILTIN_AGENT_ID = "__builtin_skill_discovery__"
SKILL_DISCOVERY_EVENT_ROLE = SKILL_DISCOVERY_SUBAGENT_NAME


def synthetic_skill_discovery_agent_row() -> dict[str, Any]:
    """Synthetic DB-shaped row so orchestrator Section 2 and event metadata stay aligned."""
    return {
        "id": SKILL_DISCOVERY_BUILTIN_AGENT_ID,
        "alias": SKILL_DISCOVERY_SUBAGENT_NAME,
        "username": SKILL_DISCOVERY_SUBAGENT_NAME,
        "name": SKILL_DISCOVERY_SUBAGENT_NAME,
        "role": SKILL_DISCOVERY_EVENT_ROLE,
        "description": (
            "Finds and ranks relevant skills for the work-item using mounted filesystem paths "
            "and claude-skills MCP tools. Returns paths and summaries for domain subagents."
        ),
        "persona": "",
        "goal": "Maximize correct skill selection before domain execution.",
        "skills": "",
        "tools": "",
        "agent_profile": "/images/chat-icon.png",
    }


def _build_skill_discovery_system_prompt(
    query: str = "",
    tenant_id: str | None = None,
    *,
    allowed_skill_names: list[str] | None = None,
    allowed_skill_names_by_agent: dict[str, list[str]] | None = None,
    hinted_skill_slugs: list[str] | None = None,
) -> str:
    """Narrow prompt: discovery only (FS + claude-skills MCP), not full domain execution."""
    t_id = (tenant_id or "").strip() or "(unknown)"
    q = (query or "").strip()
    allowed = list(allowed_skill_names or [])
    by_agent = allowed_skill_names_by_agent or {}
    hints = list(hinted_skill_slugs or [])
    parts: list[str] = [
        "=== Built-in 스킬 탐색 도우미 subagent ===",
        format_execution_clock_section().rstrip(),
        "",
        "You find the best-matching skills for the current work-item before heavy domain execution.",
        "You work for ANY domain (finance, marketing, coding, etc.); use the same retrieval pipeline.",
        "",
        "=== Runtime-enforced checklist (DO NOT SKIP) ===",
        "The orchestrator will FAIL this run unless you call ALL of the following tools at least once:",
        "- skill_volume_probe (filesystem / mounted volume check)",
        "- find_helpful_skills (claude-skills MCP search)",
        "- read_skill_document (claude-skills MCP read/list docs)",
        "Therefore you must execute both FS-based and MCP-based discovery before finishing.",
        "",
        "=== Critical requirement: always produce structured handoff JSON ===",
        "You MUST end your final answer with a machine-readable handoff block so the orchestrator can inject it:",
        "[SKILL_HANDOFF_V1]",
        "{ ... valid JSON object ... }",
        "This JSON must be valid and parseable (no trailing commas, no comments).",
        "",
        "=== Mandatory discovery workflow (FS + MCP, both required) ===",
        "0) Call skill_volume_probe(tenant_id, skill_names_csv) early for assigned skills (Tier 1 / volume).",
        "1) Call list_skills first (no arguments). Keep the full inventory in mind for this turn.",
        "2) Deterministic shortlist (before semantic search):",
        "   - Build intersection: assigned_skill_names ∩ list_skills inventory names.",
        "   - If intersection is non-empty, those skills are mandatory top-priority candidates and MUST be ranked first.",
        "   - If multiple intersection hits exist, order them by explicit mention in user text, then by lexical similarity.",
        "   - Also match kebab-case folder names and description text against the user request (e.g. "
        '"marketing", "coding", "investment").',
        "3) For each shortlisted skill name, call read_skill_document(skill_name=<name>, document_path=null) "
        "to list available files. Do NOT assume the root SKILL.md is readable via read_skill_document — "
        "if SKILL.md is missing from that listing, read the best reference doc (e.g. references/*.md) or "
        "another listed file that explains the skill.",
        "3b) For execution-oriented requests, provide guidance that favors synthesized task scripts "
        "using skill references/templates over blindly running original scripts.",
        "4) Call find_helpful_skills(task_description, tenant_id, allowed_skill_names) to catch additional relevant skills. "
        "Treat its ranking as supplementary: if a name from step 2 exists in list_skills, you MUST still "
        "report it even if find_helpful_skills omits it.",
        "5) Prefer read-only tools. Avoid delete_skill / delete_skill_file / update_skill_file unless the user "
        "explicitly asks to modify catalog skills.",
        "",
        "=== Tier 1 — Volume (MANDATORY fallback for assigned skills) ===",
        "If `allowed_skill_names` (assigned skills) is non-empty and MCP discovery does NOT clearly surface them,",
        "you MUST attempt filesystem-based resolution via the mounted skills volume:",
        f"- Use `skill_volume_probe(tenant_id, skill_names_csv)` for all allowed_skill_names (comma-separated).",
        "- Then materialize each candidate skill into the sandbox workspace:",
        "  1) Call `sandbox_create` (if you don't already have workspace_id).",
        "  2) For each allowed skill name, call `sandbox_copy_mounted_skill(tenant_id, skill_name, dest_subdir)`.",
        "  3) Call `sandbox_read_workspace_files` to confirm which files exist (SKILL.md, references, scripts).",
        "If a skill exists on disk, treat it as found even if MCP tools couldn't rank it.",
        "",
        "=== Tier transitions & observability ===",
        "Use `report_skill_resolution_tier(1|2|3, started|success|failed, detail)` when you switch tiers.",
        "",
        f"=== Tenant ===\ntenant_id: {t_id}",
    ]
    if allowed:
        parts.extend(
            [
                "",
                "=== Assigned skill names (from crew agents; use in allowlist + matching) ===",
                ", ".join(allowed),
            ]
        )
    if by_agent:
        try:
            pretty = json.dumps(by_agent, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(by_agent)
        parts.extend(
            [
                "",
                "=== Assigned skill names by domain subagent (optional; if present, produce by_agent handoffs) ===",
                pretty,
            ]
        )
    if hints:
        parts.extend(
            [
                "",
                "=== Hinted skill names / slugs (from user text; verify against list_skills) ===",
                ", ".join(hints),
            ]
        )
    if q:
        parts.extend(["", "=== Work-item query ===", q])
    parts.extend(
        [
            "",
            "=== Final message to orchestrator (required) ===",
            "- Provide a short human summary first (ranked skills, 1–2 line justification each).",
            "- Then output the handoff JSON block exactly as described below.",
            "",
            "=== Required handoff JSON schema (SKILL_HANDOFF_V1) ===",
            "Your final output MUST end with:",
            "[SKILL_HANDOFF_V1]",
            "{",
            '  "version": 1,',
            '  "tenant_id": "<tenant id>",',
            '  "allowed_skill_names": ["..."],',
            '  "selected_primary": "<skill_name or empty>",',
            '  "by_agent": { "<agent_name>": { ...SKILL_HANDOFF_V1 object... }, "...": { ... } },',
            '  "ranked": [',
            "    {",
            '      "skill_name": "...",',
            '      "evidence": {"source": "mcp"|"volume", "notes": "..."},',
            '      "documents": [{"path": "...", "purpose": "..."}],',
            '      "endpoints": ["..."],',
            '      "sandbox_materialization": {',
            '        "required": true,',
            '        "suggested_calls": [',
            '          {"tool": "sandbox_create", "args": {}},',
            '          {"tool": "sandbox_copy_mounted_skill", "args": {"tenant_id": "...", "skill_name": "...", "dest_subdir": "skill"}},',
            '          {"tool": "sandbox_read_workspace_files", "args": {}}',
            "        ]",
            "      }",
            "    }",
            "  ]",
            "}",
        ]
    )
    return "\n".join(parts)


def build_skill_discovery_subagent(
    *,
    all_tools: list[Any] | None = None,
    query: str = "",
    tenant_id: str | None = None,
    allowed_skill_names: list[str] | None = None,
    allowed_skill_names_by_agent: dict[str, list[str]] | None = None,
    hinted_skill_slugs: list[str] | None = None,
) -> dict[str, Any]:
    """Single subagent config: claude-skills MCP tools only, plus sandbox + tier observability tools."""
    if all_tools is None:
        all_tools = []
    merged = list(all_tools) + list(get_sandbox_tools()) + list(get_skill_resolution_tools())
    tools_map: dict[str, Any] = {getattr(t, "name", str(t)): t for t in merged}
    sandbox_names = _sandbox_tool_names()
    resolution_names = _skill_resolution_tool_names()
    server_to_tools: dict[str, list[Any]] = {}
    for t in merged:
        server = getattr(t, "_mcp_server", None)
        if server:
            server_to_tools.setdefault(str(server), []).append(t)

    filtered_tools: list[Any] = []
    seen_ids: set[int] = set()

    mcp_added = 0
    for t in _baseline_mcp_tools(server_to_tools):
        if id(t) not in seen_ids:
            filtered_tools.append(copy.copy(t))
            seen_ids.add(id(t))
            mcp_added += 1
    if mcp_added == 0:
        logger.warning(
            "Skill discovery subagent: no claude-skills MCP tools in loader output; Tier 2 unavailable"
        )

    for name_group in (sandbox_names, resolution_names):
        for tn in sorted(name_group):
            t = tools_map.get(tn)
            if t is None:
                continue
            if id(t) not in seen_ids:
                filtered_tools.append(copy.copy(t))
                seen_ids.add(id(t))

    # Tenant context tools (mem0/memento/dmn_rule) are always attached to *all* subagents,
    # including the built-in skill discovery agent, so orchestration can rely on them.
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
            "Skill discovery subagent: attached tenant context tools %s (added=%d, total_tools=%d)",
            ", ".join(ALWAYS_ATTACH_LOCAL_TOOL_NAMES),
            local_added,
            len(filtered_tools),
        )

    description = (
        "Discovers skills via mounted volume (Tier 1) and claude-skills MCP (Tier 2). "
        "Call this subagent when skill choice is unclear or before domain work that depends on the right skill pack."
    )
    return {
        "name": SKILL_DISCOVERY_SUBAGENT_NAME,
        "description": description,
        "system_prompt": _build_skill_discovery_system_prompt(
            query=query,
            tenant_id=tenant_id,
            allowed_skill_names=allowed_skill_names,
            allowed_skill_names_by_agent=allowed_skill_names_by_agent,
            hinted_skill_slugs=hinted_skill_slugs,
        ),
        "tools": filtered_tools,
    }
