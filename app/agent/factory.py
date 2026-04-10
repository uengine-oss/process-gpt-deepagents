"""Factory for creating middleware-first deepagents runtime.

- ``create_deep_agent`` composes TodoListMiddleware, FilesystemMiddleware, SubAgentMiddleware, etc.
- The orchestrator runs **inside** the DeepAgents graph and delegates to subagents via ``task()``.
- ``write_pipeline_spec`` / ``write_todos`` are optional notes or trace helpers only; they do not
  replace native ``task()`` execution unless legacy external executor mode is enabled.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from app.agent.backend import make_deepagent_backend_factory
from app.agent.hitl import get_hitl_tools
from app.execution_context import format_execution_clock_section
from app.executor.asset_role_resolver import format_pipeline_asset_roles_for_system_prompt

logger = logging.getLogger(__name__)

# Fallback if JSON formatting fails (aligned with asset_role_resolver budget).
_MAX_ASSET_ROLES_FALLBACK_CHARS = 480_000


def build_runtime_agent(
    *,
    model_name: str,
    system_prompt: str | None = None,
    subagents: list[dict[str, Any]] | None = None,
    tools: Sequence[Any] | None = None,
    hitl_tools: Sequence[Any] | None = None,
    middleware: Sequence[Any] | None = None,
    skills: list[str] | None = None,
    memory: list[str] | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
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
        memory=memory if memory else None,
        checkpointer=checkpointer,
        store=store,
    )
    lim_raw = (os.environ.get("DEEP_AGENT_RECURSION_LIMIT") or "").strip()
    if lim_raw:
        try:
            agent = agent.with_config({"recursion_limit": int(lim_raw)})
        except ValueError:
            logger.warning(
                "Ignoring invalid DEEP_AGENT_RECURSION_LIMIT=%r (expected int)",
                lim_raw,
            )
    logger.info(
        "Runtime agent created: model=%s subagents=%d tools=%d extra_middleware=%d memory_sources=%d",
        model_name,
        len(subagents or []),
        len(root_tools),
        len(middleware_list),
        len(memory or []),
    )
    return agent


def default_orchestrator_prompt() -> str:
    """Base rules: DeepAgents 그래프 내부 오케스트레이터 — task()로 서브에이전트 실행."""
    return (
        "당신은 ProcessGPT 멀티 에이전트 오케스트레이터입니다. 실행은 **DeepAgents 그래프 안에서** 이루어지며, "
        "당신은 **`task()` 도구로 서브에이전트를 호출**하고 그 반환 내용을 바탕으로 다음 결정을 내립니다.\n\n"
        "워크아이템 쿼리 구조:\n"
        "- [Description], [Instruction], [InputData](JSON 문자열)\n"
        "위를 파싱해 목표·제약·중요 InputData 키를 파악하세요.\n\n"
        "메시지에 포함된 `[ASSET_INVENTORY_V1]`·`[PIPELINE_STATE_CONTRACT_V1]`·섹션 1a `asset_roles`는 "
        "**수집된 전체 자산**을 반영합니다. 자산을 생략하거나 BPMN만 남기지 마세요.\n\n"
        "실행 순서(파일 자산이 여러 개 포함된 업무의 일반 규칙):\n"
        "1) 섹션 2에 서브에이전트가 있으면 `task()`로 실행을 위임합니다.\n"
        "2) 입력 자산(예: PDF/BPMN/XLSX 등)은 `[ASSET_INVENTORY_V1]`/`asset_roles`에 있는 **URL/경로**를 그대로 사용합니다. "
        "사용자에게 다시 경로를 요구하지 말고, 샌드박스 도구로 직접 다운로드/파싱/생성을 수행하세요.\n"
        "3) 서브에이전트는 코드에 하드코딩된 도메인 역할명이 아니라, **DB에서 동적으로 로드된 에이전트**를 사용합니다.\n\n"
        "구조화 산출물(가능하면 JSON)과 의미 있는 키 이름을 유지하세요: "
        "template_schema, process_facts, reference_facts, filled_template, artifact_file, public_url, "
        "deliverable_files, execution_trace. 마크다운만으로 끝내지 말고 **기계가 처리하기 쉬운 JSON**을 우선합니다.\n\n"
        "`write_todos`·`write_pipeline_spec`은 선택적 메모/관찰용일 뿐이며, **실행의 중심이 아닙니다**. "
        "실제 실행은 반드시 `task()` 디스패치로 진행합니다.\n\n"
        "매핑 시 근거가 없는 셀 값을 지어내지 말고, 빈칸·missing_reason·evidence_status를 남깁니다.\n\n"
        "HITL: `request_human_input()`은 최후 수단입니다.\n\n"
        "산출물이 파일(xlsx 등)이면 샌드박스/업로드 도구로 **public URL**까지 확보하고, "
        "폼 출력이면 필수 키만 담은 **단일 JSON 객체**로 답합니다. "
        "**draft_file 등 파일 필드에는 .bpmn 입력 경로를 넣지 마세요.** 업로드된 산출물 URL만 사용합니다.\n"
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
    output_language: str | None = None,
    hitl_resume: Mapping[str, str] | None = None,
    pipeline_asset_roles: Mapping[str, Any] | None = None,
) -> str:
    """Compose a sectioned system_prompt for the DeepAgents orchestrator.

    This mirrors the spirit of CrewAI's DynamicPromptGenerator by appending
    structured context (work-item summary, agent metadata, HITL hints, and
    tool-usage policy) on top of the static base rules.
    """
    parts: list[str] = []
    parts.append(base_rules.rstrip())
    parts.append("\n\n" + format_execution_clock_section())

    # 출력 언어: 시스템 프롬프트/플랜/결과를 한글로 통일
    if output_language == "korean":
        parts.append(
            "\n\n=== 출력 언어 ===\n"
            "- 사용자의 요청이 한글입니다.\n"
            "- 최종 요약/결과 텍스트는 **반드시 한글**로 작성하세요.\n"
            "- 계획/결과에 영어 문장(설명/주의/가이드 포함)을 섞지 마세요.\n"
        )

    # 섹션 1: 워크아이템 요약 + 테넌트 컨텍스트
    desc = (description or "").strip()
    instr = (instruction or "").strip()
    idata = input_data or {}
    key_list = ", ".join(sorted(idata.keys())) if isinstance(idata, dict) and idata else "none"
    tenant = (tenant_id or "").strip() or "(unknown)"

    parts.append(
        "\n\n=== 섹션 1: 워크아이템 요약 ===\n"
        f"- Description: {desc or '(empty)'}\n"
        f"- Instruction: {instr or '(empty)'}\n"
        f"- InputData keys: {key_list}\n"
        f"- Tenant ID: {tenant}\n"
    )

    if pipeline_asset_roles:
        try:
            roles_blob = format_pipeline_asset_roles_for_system_prompt(pipeline_asset_roles)
        except Exception:
            logger.exception("format_pipeline_asset_roles_for_system_prompt failed; using fallback")
            roles_blob = str(pipeline_asset_roles)[: _MAX_ASSET_ROLES_FALLBACK_CHARS]
        n_assets = 0
        if isinstance(pipeline_asset_roles, dict):
            _ar = pipeline_asset_roles.get("assets")
            if isinstance(_ar, list):
                n_assets = len(_ar)
        parts.append(
            "\n=== 섹션 1a: 입력 자산 역할(시스템 추정) ===\n"
            f"[ASSET_INVENTORY_V1]와 동일하게 **{n_assets}**개 자산 전체를 유지하세요. "
            f"`task()` 위임·중간 산출물에 경로/역할을 빠짐없이 전달하세요(일부만 복사 금지).\n"
            f"{roles_blob}\n"
        )

    if hitl_resume:
        fkey = (hitl_resume.get("field") or "human_response").strip()
        ans = (hitl_resume.get("answer") or "").strip()
        if ans:
            parts.append(
                "\n=== 섹션 1b: HITL 재개 입력(사람 답변) ===\n"
                "워크플로우가 사람 입력을 위해 일시중지되었고, 아래 답변이 제공되었습니다.\n"
                f"- InputData key: `{fkey}`\n"
                "- 이 답변은 **권위 있는 값**으로 간주하고 즉시 작업을 재개하세요.\n"
                "- 동일 정보를 다시 요청하지 마세요.\n\n"
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
            "=== 섹션 2: 사용 가능한 서브에이전트/스킬(`task()`로 호출) ===\n"
            "아래 이름의 서브에이전트를 **`task(subagent_type=...)`** 로 호출해 도메인 실행을 수행합니다.\n"
            "- 도메인 작업은 직접 흉내 내지 말고 반드시 적합한 에이전트에 위임합니다.\n"
            + "\n".join(agent_lines)
            + "\n"
        )

    # 섹션 3: HITL 판단 관련 정보 (텍스트 레벨 힌트)
    fb = (feedback or "").strip()
    has_feedback = bool(fb)
    parts.append("=== 섹션 3: HITL(사람 개입) 힌트 ===\n")
    if has_feedback:
        parts.append(f"- 피드백 요약이 존재하며, 이전 관행보다 우선합니다: {fb}\n")
    parts.append(
        "- Description/Instruction에 승인/민감/개인정보/결재/검토 등의 표현이 있으면, 계획에 사람 검토/승인 단계를 최소 1회 포함하세요.\n"
        "- 필요한 데이터가 InputData에 명백히 없고 도구로도 확보 불가한 경우에만, 누락 정보 제공을 HITL로 요청하세요.\n"
        "- 민감하지 않은 파라미터는 즉시 HITL로 묻지 말고, 자율 탐색 + 오류 기반 1회 이상 보정 재시도 후에만 HITL을 고려하세요.\n"
    )

    # 섹션 4: 폼/출력 구조 관련 정보 (있을 때만)
    if form_info:
        fields = form_info.get("fields")
        html = (form_info.get("html") or "").strip()
        has_fields = bool(fields)
        parts.append("=== 섹션 4: 폼/출력 구조 힌트 ===\n")
        if has_fields:
            parts.append(
                "- 이 워크아이템에는 폼 필드가 정의되어 있습니다. 최종 계획/결과가 해당 필드에 깔끔하게 매핑되도록 작성하세요.\n"
            )
        if html:
            parts.append(
                "- HTML 폼 정의가 제공되었습니다. 기대 출력(예: report/slide/multidata 섹션 등)을 설명할 때 타입/구조 힌트로 활용하세요.\n"
            )

    # 섹션 5: 멀티 에이전트 실행 규칙
    parts.append(
        "=== 섹션 5: 멀티 에이전트 실행 규칙 ===\n"
        "- 단계별로 **`task()`** 를 호출해 서브에이전트를 실행하고, 반환·도구 로그를 근거로 다음 호출을 결정합니다.\n"
        "- `write_pipeline_spec` / `write_todos`는 선택 메모일 뿐이며, 이들만으로 실행을 기대하지 마세요.\n"
        "- 다수 파일이 있어도 **검증 가능한 덩어리**(인벤토리, 스키마 JSON, 매핑 표) 단위로 나누어 위임합니다.\n"
        "- 양식/템플릿 의도: 템플릿 PDF 구조 추출 → BPMN/참고 문서 사실 추출 → 매핑 → xlsx 렌더 → 업로드 순을 `task()` 체인으로 수행합니다.\n"
        "- 조사/조회는 source, endpoint_or_series, as_of, value, unit 등 증거 필드를 요구합니다.\n"
    )

    parts.append(
        "\n=== 섹션 5b: 스킬 파일 누락 시 샌드박스 대체 실행 ===\n"
        "- API/Sheets/스크립트 작업은 “스킬 파일이 없다”로 끝내지 말고, 담당 서브에이전트에게 샌드박스/대체 실행을 지시합니다.\n"
    )

    # 섹션 6: 최종 출력 형식 요구사항
    parts.append(
        "\n=== 섹션 6: 최종 출력 형식 요구사항 ===\n"
        "- 폼 스키마가 없거나 form_id가 'defaultform'이면, 최종 결과는 다음으로 래핑됩니다:\n"
        "  {\"output\": {\"status\": \"SUCCESS\" | \"Fail\", \"result\": \"plain text only\"}}\n"
        "  이때 result는 **순수 텍스트만** 허용됩니다(문자열 안에 JSON/리스트/딕셔너리 리터럴 금지).\n"
        "- 폼 스키마가 있으면, 최종 답변은 **단 하나의 유효한 JSON 객체**여야 하며(마크다운/코드펜스/추가 문장 금지),\n"
        "  위에서 지정한 폼 필드 키만 포함해야 합니다(타입도 맞춰야 함).\n"
        "- 파일형 필드(draft_file 등)는 **public URL 또는 업로드된 xlsx 산출물**만 넣고, 입력 `.bpmn` 경로는 넣지 마세요.\n"
        "- 입력(activity 등)과 출력 폼을 혼동하지 말고, 폼 필드는 항상 채우세요.\n"
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
    memory: list[str] | None = None,
    checkpointer: Any | None = None,
    store: Any | None = None,
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
        memory=memory,
        checkpointer=checkpointer,
        store=store,
    )

