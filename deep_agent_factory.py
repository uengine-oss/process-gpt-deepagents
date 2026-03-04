"""Factory for creating the multi-agent orchestrator via deepagents."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

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
    built-in ``task()`` tool.  Any *tools* passed here are available to
    the main agent directly (e.g. shared utility tools).
    Optional *middleware* is passed to create_deep_agent for subagent event hooks etc.
    """
    effective_prompt = system_prompt or default_orchestrator_prompt()
    tools = list(tools) if tools else []
    middleware_list = list(middleware) if middleware else []

    agent = create_deep_agent(
        model=model_name,
        tools=tools,
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
        "Your role is planning, delegation, and synthesis — not direct execution:\n"
        "- Break the work into small atomic steps.\n"
        "- For each step, choose the most appropriate subagent (by name/role/description).\n"
        "- You MUST delegate domain-specific work to subagents using the task() tool and wait "
        "for their reports before producing a final answer. Do NOT answer from your own knowledge "
        "when a suitable subagent exists.\n"
        "- CRITICAL: Whenever the work-item matches a subagent's domain (e.g. data analysis, "
        "research, external API calls), you MUST call the task() tool first with that subagent. "
        "Never respond with only text or code examples without delegating; the subagent will use "
        "its own tools to perform the actual work.\n\n"
        "Domain routing examples (not exhaustive):\n"
        "- If the query clearly belongs to a specialised area (for example, financial analysis, "
        "legal review, data engineering, customer support, etc.), call task() with the subagent "
        "whose name or role best matches that area. That subagent will use its configured tools "
        "to retrieve and interpret the necessary data.\n"
        "- For other domains, call task() with the subagent whose role best matches the topic.\n\n"
        "Tools and skills policy:\n"
        "- Subagents own their tools and skills (including any MCP tools, vector stores, memory, "
        "or rule engines). You should not call these directly; instead, specify in your plan "
        "which tools they are expected to use.\n"
        "- You may use organisation-wide context tools (for example, global knowledge or "
        "documentation search tools) only to gather shared background information before or "
        "between delegations.\n\n"
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
            "=== Section 2: Available agents and skills ===\n"
            "The following subagents are available. Delegate domain-specific work to the "
            "most appropriate agent based on role and skills. Each agent may have its own "
            "tools and skills configured; treat these as black boxes and describe them only "
            "at a high level in your plan (e.g. 'uses retrieval tools', 'uses analysis tools').\n"
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
        "- Break the task into atomic steps. For each step, specify:\n"
        "  * which subagent (by name/role) should handle it,\n"
        "  * which kinds of tools that subagent is expected to use "
        "(for example, retrieval/search tools, analysis tools, memory tools, rule engines),\n"
        "  * which parts of Description/Instruction/InputData (or previous steps) it consumes,\n"
        "  * what concrete intermediate output it must return.\n"
        "- Remember: some tools may be organisation-wide context tools (used at the orchestrator "
        "level), while others are owned by individual subagents. Reflect this separation when "
        "you describe how work is delegated.\n"
        "- Your final reply MUST include a [PLAN]...[/PLAN] block containing only the "
        "step-by-step plan (no error text or results inside that block), and optionally "
        "[SUMMARY]...[/SUMMARY] or [RESULT]...[/RESULT] for summary/outcome.\n"
    )

    # 섹션 6: 최종 출력 형식 요구사항
    parts.append(
        "\n=== Section 6: Final output format requirements ===\n"
        "- When there is no form schema or the form_id is 'defaultform', the final business "
        "result will be wrapped as: {\"output\": {\"status\": \"SUCCESS\" | \"Fail\", "
        "\"result\": \"plain text only\"}}. The 'result' value MUST be plain text only "
        "(no JSON strings, no list/dict literals).\n"
        "- In this case, produce a clear, human-readable narrative of the outcome for "
        "'result'.\n"
        "- When a concrete form schema is present, ensure that your outputs can be mapped "
        "into the form fields described above while still being safely serialisable as JSON.\n"
    )

    return "\n".join(parts)
