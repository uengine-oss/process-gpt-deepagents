"""Factory for creating middleware-first deepagents runtime."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from deepagents_backend import make_deepagent_backend_factory
from deepagents_hitl import get_hitl_tools
from execution_context import format_execution_clock_section

logger = logging.getLogger(__name__)

def build_runtime_agent(
    *,
    model_name: str,
    system_prompt: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
    tools: Sequence[Any] | None = None,
    hitl_tools: Sequence[Any] | None = None,
    middleware: Sequence[Any] | None = None,
    skills: list[str] | None = None,
) -> CompiledStateGraph:
    """Create single deep agent runtime with middleware-first architecture."""
    effective_prompt = system_prompt or default_orchestrator_prompt()
    effective_hitl = list(hitl_tools) if hitl_tools is not None else list(get_hitl_tools())
    root_tools = list(tools) if tools else []
    all_tools = effective_hitl + root_tools
    middleware_list = list(middleware) if middleware else []
    # NOTE:
    # create_deep_agent() already injects TodoListMiddleware, FilesystemMiddleware,
    # and SubAgentMiddleware internally. Adding them again causes duplicate middleware errors.

    agent = create_deep_agent(
        model=model_name,
        tools=all_tools,
        system_prompt=effective_prompt,
        subagents=subagents or [],
        middleware=middleware_list,
        backend=make_deepagent_backend_factory(),
        skills=skills if skills else None,
    )
    logger.info(
        "Runtime agent created: model=%s subagents=%d tools=%d extra_middleware=%d",
        model_name,
        len(subagents or []),
        len(root_tools),
        len(middleware_list),
    )
    return agent


def default_orchestrator_prompt() -> str:
    """Base rules for middleware-first orchestrator agent."""
    return (
        "You are the ProcessGPT Multi-Agent Orchestrator.\n\n"
        "You always receive a single user message called the *work-item query*.\n"
        "It has three sections:\n"
        "[Description]  – what this activity is trying to achieve.\n"
        "[Instruction]  – how it should be done, including required tools/MCP/skills.\n"
        "[InputData]    – JSON string with the actual business data for this activity.\n\n"
        "First, carefully parse these three sections and understand:\n"
        "- the business goal,\n"
        "- any explicit constraints and approvals required,\n"
        "- which InputData keys are important.\n\n"
        "Your role is to plan, delegate, execute, and synthesize results with middleware support:\n"
        "- Use write_todos to keep your plan updated while solving multi-step work.\n"
        "- Use task() to delegate domain work to subagents whenever specialized tools are needed.\n"
        "- Use filesystem tools for large intermediate outputs to reduce context bloat.\n"
        "- request_human_input() is available for HITL when approvals or missing critical data appear.\n"
        "- For any domain-specific work (data fetch, API calls, skill execution, analysis), you MUST call "
        "task() with the appropriate subagent from Section 2 and wait for their result. Never perform "
        "such work by yourself or reply with only text/code examples.\n"
        "- Do NOT use a general-purpose subagent for domain work. Use the named domain subagents listed in "
        "Section 2 (e.g. financial/data/research agents with assigned skills).\n"
        "- A built-in subagent named exactly `스킬 탐색 도우미` is always in Section 2. "
        "For EVERY work-item you MUST call task() with subagent_type `스킬 탐색 도우미` once before any domain "
        "subagent — no exceptions. Capture its ranked skill paths and MCP-backed recommendations first; only "
        "then delegate execution/analysis to domain subagents using those results. Put this as step 1 in write_todos.\n"
        "- Break the work into small atomic steps. For each step that involves tools/skills/APIs, "
        "choose the most appropriate subagent by name/role/description and call task() with a clear "
        "instruction. Only after you receive subagent results may you synthesize the final answer.\n\n"
        "Domain routing (mandatory delegation):\n"
        "- If the query involves a specialised area (e.g. financial data, external APIs, research, "
        "analysis), you MUST call task() with the subagent whose name or role matches that area. "
        "That subagent will use its configured tools/skills; you only assign and aggregate.\n"
        "- You must not answer from your own knowledge when a suitable subagent exists. If in doubt, "
        "delegate to the subagent whose skills best match the request.\n\n"
        "Tools and skills policy:\n"
        "- Subagents own all execution tools and skills (MCP, APIs, memory, rule engines). You do not "
        "have these tools. Specify in your plan which subagent will do what; do not attempt to run "
        "skills or tools yourself.\n"
        "- When a delegated subagent has memory/rule/RAG tools (mem0, memento, dmn_rule), instruct it "
        "in task() to use them when Section 2 / the work-item implies personalization, rules, or "
        "document grounding — not on every trivial step.\n"
        "- Do not call find_helpful_skills, read_skill_document, or any API/skill tools — only "
        "task() and request_human_input() are available to you.\n\n"
        "Missing skills / sandbox (orchestrator — do not bypass delegation):\n"
        "- You MUST NOT finish the work-item alone with prose such as \"skill files are missing\", "
        "\"cannot access Google Sheets\", or only generic Cloud Console setup steps when Section 2 "
        "lists domain subagents. Those answers belong after real execution attempts by a subagent.\n"
        "- Skill discovery is never optional: the mandatory `스킬 탐색 도우미` run comes first; domain subagents "
        "then follow (1) mounted volume/FS skills, (2) claude-skills MCP, (3) sandbox execution per their "
        "instructions. They MUST use sandbox_write_and_run (or write + shell) only after MCP "
        "has been tried when MCP is configured.\n"
        "- For execution tasks, domain subagents should prefer synthesized task-specific scripts "
        "(built from request + InputData + skill references) over blindly executing original skill scripts. "
        "Direct skill-script execution is allowed only when it exactly matches required inputs and behavior.\n"
        "- When delegating with task(), include resolved dates and symbols in plain text (from the execution "
        "clock block the runtime injects into your prompt) so subagents do not re-guess \"yesterday\" or "
        "invent CLI flags.\n"
        "- Your final synthesis MUST quote concrete subagent outputs (stdout/stderr, exit codes), "
        "not replace them with manual runbooks.\n\n"
        "Human-in-the-loop (HITL) — last resort only:\n"
        "- request_human_input() stops the whole run. Use it only after subagents have exhausted "
        "reasonable tool attempts (mem0/memento, MCP, sandbox, FS, API retries), or when law/policy "
        "requires explicit human approval.\n"
        "- If Instructions or Description indicate approvals, sensitive data, or explicit "
        "human review (e.g. \"승인\", \"결재\", \"민감\", \"개인정보\", \"approval\"), plan a human "
        "review step — but still delegate tool-based work first where possible.\n"
        "- If InputData looks incomplete, do NOT plan \"ask the human\" as step 1. Plan: (1) delegate "
        "to subagents to retrieve from mem0/memento, infer from docs, list/discover APIs, use defaults; "
        "(2) retry with corrected parameters; (3) only then consider HITL for what is still truly missing.\n"
        "- Before any HITL for missing parameters, subagents must attempt autonomous discovery (skill docs, "
        "API spec, endpoint list/search, error-guided retries). HITL is the last resort, not the first.\n"
        "- Clearly describe what the human must confirm only when a human step is truly necessary.\n\n"
        "Task scope and robustness:\n"
        "- Perform only what the Description/Instruction explicitly request for this "
        "single work-item. Do not invent extra follow-up work.\n"
        "- If a tool or subagent fails, continue as far as possible with the remaining "
        "information and clearly state any limitations in the final synthesis.\n"
        "\n"
        "Planning and logging requirements:\n"
        "- Keep todo items updated with write_todos for all non-trivial tasks.\n"
        "- Prefer explicit delegation to named subagents for tools/API/skills operations.\n"
        "Credentials and HITL:\n"
        "- When credentials are not in InputData, instruct subagents to use mem0/memento first, then MCP, "
        "then sandbox — not immediate human upload requests. Reserve HITL for when tools genuinely "
        "cannot supply the secret after those attempts.\n"
        "- Do NOT have subagents stop at placeholder code; keep delegating until real calls or valid HITL.\n"
        "- Never return only placeholder code or example snippets when real execution is "
        "expected; ensure the plan leads to actual tool/API use once credentials are supplied.\n"
    )


def build_orchestrator_system_prompt(
    *,
    base_rules: str,
    description: str,
    instruction: str,
    input_data: Mapping[str, Any] | None,
    agents: list[dict[str, Any]] | None,
    feedback: str | None,
    form_info: Mapping[str, Any] | None,
    tenant_id: str | None = None,
    plan_text: str | None = None,
    output_language: str | None = None,
    hitl_resume: Mapping[str, str] | None = None,
) -> str:
    """Compose a sectioned system_prompt for the DeepAgents orchestrator.

    This mirrors the spirit of CrewAI's DynamicPromptGenerator by appending
    structured context (work-item summary, agent metadata, HITL hints, and
    tool-usage policy) on top of the static base rules.
    """
    parts: list[str] = []
    parts.append(base_rules.rstrip())
    parts.append("\n\n" + format_execution_clock_section())

    # 사용자 입력이 한글인 경우 [PLAN] 및 최종 result를 한글로 작성하도록 지시
    if output_language == "korean":
        parts.append(
            "\n\n=== Output language ===\n"
            "The user's request is in Korean. You MUST write your [PLAN] block and all "
            "final result/summary text in Korean. Do not use English for plans or result.\n"
        )

    # 섹션 1: 워크아이템 요약 + 테넌트 컨텍스트
    desc = (description or "").strip()
    instr = (instruction or "").strip()
    idata = input_data or {}
    key_list = ", ".join(sorted(idata.keys())) if isinstance(idata, dict) and idata else "none"
    tenant = (tenant_id or "").strip() or "(unknown)"

    parts.append(
        "\n\n=== Section 1: Work-item summary ===\n"
        f"- Description: {desc or '(empty)'}\n"
        f"- Instruction: {instr or '(empty)'}\n"
        f"- InputData keys: {key_list}\n"
        f"- Tenant ID: {tenant}\n"
    )

    if hitl_resume:
        fkey = (hitl_resume.get("field") or "human_response").strip()
        ans = (hitl_resume.get("answer") or "").strip()
        if ans:
            parts.append(
                "\n=== Section 1b: Human follow-up after interrupt (HITL resume) ===\n"
                f"The workflow was paused for human input. The following was provided under "
                f"InputData key `{fkey}` — you MUST treat this as authoritative and continue "
                f"immediately without asking for the same information again.\n\n"
                f"{ans}\n"
            )

    # 섹션 2: 에이전트 / 스킬 / 도구 맥락
    agent_lines: list[str] = []
    for ag in agents or []:
        name = ag.get("alias") or ag.get("username") or ag.get("name") or str(ag.get("id"))
        role = ag.get("role") or "Unspecified role"
        skills = ag.get("skills") or ""
        tools = ag.get("tools") or ""
        agent_lines.append(
            f"- Agent '{name}': role={role}, skills={skills or '(none)'}, tools={tools or '(none)'}"
        )
    if agent_lines:
        parts.append(
            "=== Section 2: Available agents and skills (you MUST delegate to these) ===\n"
            "The following subagents are available. You MUST delegate all domain-specific work "
            "(API calls, data fetch, skill execution, analysis) to the most appropriate agent "
            "below using the task() tool. Do not perform such work yourself.\n"
            "Each agent has its own tools and skills; use task() with the agent name and a clear "
            "instruction. Describe in your plan which subagent handles which step.\n"
            + "\n".join(agent_lines)
            + "\n"
        )

    # 섹션 3: HITL 판단 관련 정보 (텍스트 레벨 힌트)
    fb = (feedback or "").strip()
    has_feedback = bool(fb)
    parts.append("=== Section 3: Human-in-the-loop (HITL) hints ===\n")
    if has_feedback:
        parts.append(f"- Feedback summary is present and should override older conventions: {fb}\n")
    parts.append(
        "- When planning, pay special attention to approval- or sensitivity-related words in "
        "Description/Instruction such as '승인', '검토', '결재', '민감', '개인정보', "
        "'approval', 'sensitive', 'personal data'.\n"
        "- If such words appear, include at least one explicit step where a human must review "
        "or approve either the plan or the final result.\n"
        "- If the requested action clearly needs data that is not present in InputData, "
        "include a step where you ask the human to provide the missing information before "
        "executing the action.\n"
        "- For non-sensitive API/query parameters, do not ask human first. Plan autonomous "
        "parameter discovery + at least one corrective retry from tool error feedback before HITL.\n"
    )

    # 섹션 4: 폼/출력 구조 관련 정보 (있을 때만)
    if form_info:
        fields = form_info.get("fields")
        html = (form_info.get("html") or "").strip()
        has_fields = bool(fields)
        parts.append("=== Section 4: Form / output structure hints ===\n")
        if has_fields:
            parts.append(
                "- Form fields are defined for this work-item. Ensure that your final plan "
                "produces outputs that can be mapped cleanly into these fields.\n"
            )
        if html:
            parts.append(
                "- HTML form definition is available. Use it to understand any special types "
                "(e.g. report, slide, multidata sections) when describing expected outputs.\n"
            )

    # 섹션 5: 멀티 에이전트 플랜 요구사항
    parts.append(
        "=== Section 5: Multi-agent planning requirements ===\n"
        "- You do not have domain tools (skills, APIs, MCP). All such work MUST be done by "
        "subagents via task(). Break the task into atomic steps and for each step that needs "
        "tools/skills/data, assign a subagent from Section 2.\n"
        "- Mandatory order: step 1 MUST always be task() → `스킬 탐색 도우미` before any "
        "other subagent. After you receive its skill recommendations, plan domain subagent steps.\n"
        "- For each step, specify: which subagent (by name) handles it, which tools/skills that "
        "subagent will use, what inputs it consumes, and what output it must return.\n"
        "- If a subagent has mem0/memento/dmn_rule and the task benefits from them, mention in the "
        "delegated instruction which to try and why (e.g. stored credentials vs DMN policy vs RAG docs).\n"
        "- Do not use the general-purpose subagent for domain work; use the named domain subagents "
        "in Section 2 (roles/skills that match the request). Use `스킬 탐색 도우미` only for "
        "skill search/ranking (always first), not for full domain execution.\n"
        "- Your final reply MUST include a [PLAN]...[/PLAN] block containing only the "
        "step-by-step plan (no error text or results inside that block), and optionally "
        "[SUMMARY]...[/SUMMARY] or [RESULT]...[/RESULT] for summary/outcome.\n"
        "- For data lookup/investigation requests (e.g. API-based queries), ensure delegated outputs include "
        "verifiable evidence fields: source, endpoint_or_series, as_of, value, unit.\n"
    )

    parts.append(
        "\n=== Section 5b: Sandbox fallback when skill files are missing ===\n"
        "- API/Sheets/script work must not end as prose-only refusals (e.g. \"no skill files\").\n"
        "- You MUST call task() with the appropriate subagent from Section 2 and instruct them to use "
        "sandbox_write_and_run (or write + shell) after copy/read failures.\n"
    )

    # 섹션 6: 최종 출력 형식 요구사항
    parts.append(
        "\n=== Section 6: Final output format requirements ===\n"
        "- When there is no form schema or the form_id is 'defaultform', the final business "
        "result will be wrapped as:{\"output\": {\"status\": \"SUCCESS\" | \"Fail\", "
        "\"result\": \"plain text only\"}}. The 'result' value MUST be plain text only "
        "(no JSON strings, no list/dict literals).\n"
        "- In this case, produce a clear, human-readable narrative of the outcome for "
        "'result'.\n"
        "- When a concrete form schema is present, ensure that your outputs can be mapped "
        "into the form fields described above while still being safely serialisable as JSON.\n"
        "- When a concrete form schema is present, your FINAL answer MUST be a single valid JSON object "
        "(no markdown, no code fences, no extra prose). It MUST contain ONLY the form fields (keys) "
        "described above, with correctly-typed values. This output form is the deliverable.\n"
        "- Do NOT confuse input activities (e.g. any @activity:... mentions) with the output form. "
        "Input activity names may be absent in the work-item; do not rely on them. Always populate the "
        "output form fields regardless.\n"
    )

    # 선택: 외부에서 전달된 플랜 힌트가 있으면 참조 컨텍스트로 제공
    if plan_text:
        cleaned_plan = (str(plan_text).strip() or "")
        if cleaned_plan:
            parts.append(
                "\n=== Section 7: External planning hint ===\n"
                "Use this as optional context only and keep write_todos as source of truth.\n\n"
                f"{cleaned_plan}\n"
            )

    return "\n".join(parts)


def build_orchestrator(
    *,
    model_name: str,
    system_prompt: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
    tools: Sequence[Any] | None = None,
    hitl_tools: Sequence[Any] | None = None,
    middleware: Sequence[Any] | None = None,
    skills: list[str] | None = None,
) -> CompiledStateGraph:
    """Backward compatible alias for runtime agent builder."""
    return build_runtime_agent(
        model_name=model_name,
        system_prompt=system_prompt,
        subagents=subagents,
        tools=tools,
        hitl_tools=hitl_tools,
        middleware=middleware,
        skills=skills,
    )

