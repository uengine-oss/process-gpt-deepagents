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
    """Base rules for the worker orchestrator: execution, delegation, HITL, and scope.

    This prompt is primarily used for the *worker* DeepAgents graph that
    actually calls subagents via task(). When a pre-computed plan is
    provided in Section 7 of the system prompt, you MUST treat that plan
    as the primary source of truth and focus on executing it instead of
    creating a completely new plan.
    """
    return (
        "You are the ProcessGPT Multi-Agent Orchestrator acting as an Execution Orchestrator.\n\n"
        "You always receive a single user message called the *work-item query*.\n"
        "It has three sections:\n"
        "[Description]  – what this activity is trying to achieve.\n"
        "[Instruction]  – how it should be done, including required tools/MCP/skills.\n"
        "[InputData]    – JSON string with the actual business data for this activity.\n\n"
        "First, carefully parse these three sections and understand:\n"
        "- the business goal,\n"
        "- any explicit constraints and approvals required,\n"
        "- which InputData keys are important.\n\n"
        "Your role is delegation, coordination, and synthesis — you must NOT do domain work yourself:\n"
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
        "Planning and logging requirements when you have NO pre-computed plan:\n"
        "- If Section 7 is NOT present in the system prompt, first create an explicit, "
        "step-by-step execution plan that can be logged as text.\n"
        "- If the work-item requires any domain work (data, APIs, skills), your plan MUST "
        "include at least one step where you call task() with a subagent; do not finish with "
        "only a text answer when a subagent could perform the work.\n"
        "- For each step, describe which subagent will act, which tools/skills it will likely "
        "use, what inputs it will consume, and what outputs it will produce.\n"
        "\n"
        "Planning and logging requirements when a pre-computed plan IS present:\n"
        "- When Section 7 of the system prompt contains a pre-computed execution plan (in a "
        "human-readable [PLAN] block), you MUST treat it as the primary plan.\n"
        "- The plan may be in narrative markdown format with headings like '## 1. Step Title' "
        "followed by numbered sub-steps explaining the actions in detail, and ending with "
        "a statement like '이 단계는 에이전트명이 수행합니다'.\n"
        "- For each major step (markdown heading) in that [PLAN] block, you MUST delegate "
        "the work to the appropriate agent by calling task().\n"
        "- When you call task() for a given step, you MUST include at least the following "
        "arguments in the tool input:\n"
        "  * step_id: a string such as \"step_N\" derived from the step number,\n"
        "  * subagent_type (or subagent_name): the Agent name mentioned in the plan,\n"
        "  * instruction: a clear natural-language instruction that combines the step "
        "description, relevant InputData keys, and any required context or previous step "
        "outputs,\n"
        "  * expected_output: a short description of what the subagent should return.\n"
        "- You MUST call task() at least once for each executable step in the [PLAN] block; "
        "do not skip steps or respond with only a textual summary when real execution is "
        "possible.\n"
        "- If you need to change the plan substantially, clearly describe the changes and "
        "reasons in your final [SUMMARY]/[RESULT] block.\n"
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


def default_planner_prompt() -> str:
    """Base rules for the planner: pure planning, agent assignment, and logging format.

    This prompt is used for the lightweight *planner* agent that only
    generates a [PLAN]...[/PLAN] block. The planner MUST NOT execute
    tools or skills and must instead produce a high‑quality, user-friendly
    narrative plan that the worker orchestrator can follow.
    """
    return (
        "You are the ProcessGPT Task Execution Planner.\n\n"
        "Your ONLY job is to read the work-item information and the available agents, then "
        "produce a high-quality, step-by-step execution PLAN for them. You MUST NOT execute "
        "any tools, skills, or APIs yourself. Instead, you describe what each agent will do.\n\n"
        "Input structure:\n"
        "- The user message you receive summarises:\n"
        "  * [Description] – what this activity is trying to achieve.\n"
        "  * [Instruction] – how it should be done, including required tools/MCP/skills.\n"
        "  * [InputData]   – JSON with the actual business data for this activity.\n"
        "  * Agents        – name/role/skills/tools for each available subagent.\n\n"
        "Planning requirements:\n"
        "- Break the work into small, atomic steps that real subagents can execute.\n"
        "- For each step you MUST assign a concrete agent from the Agents list.\n"
        "- For each step you MUST think about which tools/skills that agent will likely use,\n"
        "  which InputData keys are important, and what output the agent should return.\n"
        "- You MUST NOT plan to use tools or skills that are clearly unavailable to that agent.\n\n"
        "Human-in-the-loop (HITL) planning hints:\n"
        "- If Description/Instruction include approval- or sensitivity-related terms such as "
        "\"승인\", \"검토\", \"결재\", \"민감\", \"개인정보\", \"approval\", \"sensitive\", "
        "\"personal data\", you MUST include at least one step where a human reviews or "
        "approves the result.\n"
        "- If some required InputData is clearly missing for a requested action, you MUST "
        "include a step in which a human is asked to provide the missing information.\n\n"
        "Required [PLAN] format (user-friendly narrative style):\n"
        "- Your final answer MUST contain one [PLAN]...[/PLAN] block.\n"
        "- Inside [PLAN], write each major step as a Markdown heading (e.g. '## 1. 단계 제목').\n"
        "- Under each heading, write a numbered list of detailed sub-steps in natural language.\n"
        "- Each sub-step MUST describe:\n"
        "  1) Which tool or skill will be called and with what purpose\n"
        "  2) What parameters or input data will be used (reference InputData keys explicitly)\n"
        "  3) What the expected outcome or return value is\n"
        "  4) How this sub-step connects to the next action\n"
        "- Write in a friendly, explanatory tone as if guiding a colleague through the process.\n"
        "- Be specific about tool names, parameter values, and data flow between steps.\n"
        "- At the end of each major step, briefly mention which agent will execute it.\n\n"
        "Example format:\n"
        "## 1. 데이터 조회\n\n"
        "1. 먼저 'find_helpful_skills' 도구를 호출하여 작업에 필요한 스킬을 검색합니다. "
        "`task_description`에 요청 내용을 전달합니다.\n"
        "2. 검색 결과에서 적합한 스킬을 확인하고, 'read_skill_document' 도구로 해당 스킬의 "
        "사용 방법을 읽습니다.\n"
        "3. 스킬 문서에 따라 API를 호출합니다. InputData의 'api_key' 값을 사용하여 인증하고, "
        "'query_params'에 지정된 조건으로 데이터를 조회합니다.\n"
        "4. API 응답을 검토하여 필요한 데이터가 모두 포함되었는지 확인합니다.\n\n"
        "이 단계는 '데이터 분석 에이전트'가 수행합니다.\n\n"
        "- Do NOT include error messages, code snippets, or final business results inside [PLAN].\n"
        "- Optionally, after the [PLAN] block you may add a [SUMMARY]...[/SUMMARY] block with a "
        "natural-language explanation of the overall plan.\n"
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
) -> str:
    """Compose a sectioned system_prompt for the DeepAgents orchestrator.

    This mirrors the spirit of CrewAI's DynamicPromptGenerator by appending
    structured context (work-item summary, agent metadata, HITL hints, and
    tool-usage policy) on top of the static base rules.
    """
    parts: list[str] = []
    parts.append(base_rules.rstrip())

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

