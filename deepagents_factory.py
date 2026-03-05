"""Factory for creating the multi-agent orchestrator via deepagents."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from deepagents_hitl import get_hitl_tools

logger = logging.getLogger(__name__)


def build_orchestrator(
    *,
    model_name: str,
    system_prompt: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
    tools: Sequence[Any] | None = None,
    middleware: Sequence[Any] | None = None,
) -> CompiledStateGraph:
    """Create the top-level orchestrator agent with DB-driven subagents.

    The main agent coordinates work by delegating to subagents via the
    built-in ``task()`` tool. The orchestrator should only receive HITL
    tools (request_human_input) and optionally shared utility tools.
    Do NOT pass skill/MCP/API tools here — those belong to subagents;
    giving them to the orchestrator leads to it doing work itself instead
    of delegating. Optional *middleware* is passed to create_deep_agent
    for subagent event hooks etc.
    """
    effective_prompt = system_prompt or default_orchestrator_prompt()
    # DeepAgents 하네스와 동일하게 그래프 레벨 HITL 을 위해
    # request_human_input 도구를 항상 주입한다. 오케스트레이터에는
    # 스킬/MCP 도구를 넣지 않아 위임(task) 동작이 유지되도록 한다.
    hitl_tools = list(get_hitl_tools())
    tools = list(tools) if tools else []
    all_tools = hitl_tools + tools
    middleware_list = list(middleware) if middleware else []

    agent = create_deep_agent(
        model=model_name,
        tools=all_tools,
        system_prompt=effective_prompt,
        subagents=subagents or [],
        middleware=middleware_list,
    )
    logger.info(
        "Orchestrator created: model=%s subagents=%d tools=%d",
        model_name,
        len(subagents or []),
        len(tools),
    )
    return agent


def default_orchestrator_prompt() -> str:
    """Base rules for the orchestrator: work-item structure, delegation, HITL, and scope."""
    return (
        "You are the ProcessGPT Multi-Agent Orchestrator acting as a Task Execution Planner.\n\n"
        "You always receive a single user message called the *work-item query*.\n"
        "It has three sections:\n"
        "[Description]  – what this activity is trying to achieve.\n"
        "[Instruction]  – how it should be done, including required tools/MCP/skills.\n"
        "[InputData]    – JSON string with the actual business data for this activity.\n\n"
        "First, carefully parse these three sections and understand:\n"
        "- the business goal,\n"
        "- any explicit constraints and approvals required,\n"
        "- which InputData keys are important.\n\n"
        "Your ONLY role is planning, delegation, and synthesis — you must NOT do domain work yourself:\n"
        "- You have exactly two tools: task() (to delegate to subagents) and request_human_input() (for HITL). "
        "You do NOT have access to skill tools, MCP tools, or API-calling tools; those belong to subagents.\n"
        "- For any domain-specific work (data fetch, API calls, skill execution, analysis), you MUST call "
        "task() with the appropriate subagent from Section 2 and wait for their result. Never perform "
        "such work by yourself or reply with only text/code examples.\n"
        "- Do NOT use a general-purpose subagent for domain work. Use the named subagents listed in "
        "Section 2 (e.g. financial/data/research agents with assigned skills).\n"
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
        "- Do not call find_helpful_skills, read_skill_document, or any API/skill tools — only "
        "task() and request_human_input() are available to you.\n\n"
        "Human-in-the-loop (HITL):\n"
        "- If Instructions or Description indicate approvals, sensitive data, or explicit "
        "human review (e.g. terms like \"승인\", \"검토\", \"결재\", \"민감\", \"개인정보\", "
        "\"approval\"), you MUST include at least one step where a human is asked to review "
        "or approve either the plan or the final result.\n"
        "- If essential InputData is missing for a requested action, you MUST include a "
        "step to ask the human for the missing information before executing that action.\n"
        "- Clearly describe what must be confirmed or provided by the human in those steps.\n\n"
        "Task scope and robustness:\n"
        "- Perform only what the Description/Instruction explicitly request for this "
        "single work-item. Do not invent extra follow-up work.\n"
        "- If a tool or subagent fails, continue as far as possible with the remaining "
        "information and clearly state any limitations in the final synthesis.\n"
        "\n"
        "Planning and logging requirements:\n"
        "- First, create an explicit, step-by-step execution plan that can be logged as text.\n"
        "- If the work-item requires any domain work (data, APIs, skills), your plan MUST include "
        "at least one step where you call task() with a subagent; do not finish with only a text "
        "answer when a subagent could perform the work.\n"
        "- For each step, describe which subagent will act, which tools/skills it will likely "
        "use, what inputs it will consume, and what outputs it will produce.\n"
        "\n"
        "Required final response format (you MUST structure your reply as follows):\n"
        "- [PLAN] ... [/PLAN]: Put ONLY the step-by-step execution plan in this block. "
        "Use numbered steps; for each step include agent/tools/input/output. Do NOT put "
        "error messages, general commentary, or final results inside [PLAN]. This block is "
        "stored as the official 'plans' field.\n"
        "- [SUMMARY] ... [/SUMMARY] or [RESULT] ... [/RESULT]: Put execution summary, "
        "constraints, or final outcome narrative here (not inside [PLAN]).\n"
        "\n"
        "Credentials and human-in-the-loop (HITL):\n"
        "- When a subagent needs API keys, tokens, or other credentials for external services "
        "that are not in InputData, do NOT have the subagent return only placeholder code or "
        "'insert your API key here'. The system will ask the human for the missing credential "
        "and then continue; plan for a step where the human provides it and the subagent "
        "performs the real call.\n"
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
) -> str:
    """Compose a sectioned system_prompt for the DeepAgents orchestrator.

    This mirrors the spirit of CrewAI's DynamicPromptGenerator by appending
    structured context (work-item summary, agent metadata, HITL hints, and
    tool-usage policy) on top of the static base rules.
    """
    parts: list[str] = []
    parts.append(base_rules.rstrip())

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
        "- For each step, specify: which subagent (by name) handles it, which tools/skills that "
        "subagent will use, what inputs it consumes, and what output it must return.\n"
        "- Do not use the general-purpose subagent for domain work; use the named subagents "
        "in Section 2 (e.g. those with roles/skills that match the request).\n"
        "- Your final reply MUST include a [PLAN]...[/PLAN] block containing only the "
        "step-by-step plan (no error text or results inside that block), and optionally "
        "[SUMMARY]...[/SUMMARY] or [RESULT]...[/RESULT] for summary/outcome.\n"
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
    )

    # (선택) 섹션 7: 사전 계산된 실행 플랜
    if plan_text:
        cleaned_plan = (str(plan_text).strip() or "")
        if cleaned_plan:
            parts.append(
                "\n=== Section 7: Pre-computed execution plan (read-only) ===\n"
                "The following plan was generated in a previous planning step. Use it as a strong "
                "prior when delegating to subagents, but you may refine or adjust it if needed. "
                "If you change the plan substantially, summarise the changes in your final result.\n\n"
                f"{cleaned_plan}\n"
            )

    return "\n".join(parts)


def build_planner_agent(
    *,
    model_name: str,
    system_prompt: str,
) -> CompiledStateGraph:
    """Create a lightweight planner agent that only generates a PLAN block.

    - No subagents or domain tools are attached.
    - This agent should only be used to produce a [PLAN]...[/PLAN] block and
      MUST NOT execute tools or skills.
    """
    agent = create_deep_agent(
        model=model_name,
        tools=[],
        system_prompt=system_prompt,
        subagents=[],
        middleware=[],
    )
    logger.info("Planner agent created: model=%s", model_name)
    return agent

