"""Subagent prompt sections builder.

Goal: keep delegated task() model calls small and stable.

Unlike the root orchestrator, subagents already receive their main task context via the
`task(description=...)` handoff. Therefore we avoid injecting large work-item q blobs
([Description]/[Instruction]/[InputData]) into system prompts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _iso_now_local() -> str:
    try:
        return datetime.now(timezone.utc).astimezone().isoformat()
    except Exception:
        return ""


def build_subagent_prompt_sections(
    *,
    tenant_id: str | None,
    skill_root_dir: str,
    output_language: str | None = None,
    agent_info: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return sections for step-aware subagent prompt composition.

    agent_info: optional per-agent metadata (name, role, description, persona, goal,
    skills, declared_tools, form_fields). When provided, agent-specific sections are
    included so the middleware delivers the full context to the model.
    """
    tid = (tenant_id or "").strip() or "(unknown)"
    skill_root = (skill_root_dir or "").strip().rstrip("/") or "/app/skills"

    lang = ""
    if output_language == "korean":
        lang = (
            "=== 출력 언어 ===\n"
            "- 최종 보고/요약 텍스트는 **반드시 한글**로 작성하세요.\n"
            "- 영어 문장(설명/주의/가이드 포함)을 섞지 마세요.\n"
        )

    clock = (
        "=== 실행 시각 ===\n"
        f"- now: {_iso_now_local()}\n"
        "- time_zone: local (server)\n"
    )

    base_identity = "당신은 실행 서브에이전트입니다."

    priorities = (
        "=== 우선순위 ===\n"
        "- 1) `task()` 위임(description)의 지시/출력 형식을 최우선으로 따른다.\n"
        "- 2) 추정 금지: 문서/실행 결과로 검증한다.\n"
        "- 3) 가능하면 실행(샌드박스)하고, 불가하면 사유+다음 시도안을 짧게 보고한다.\n"
    )

    tenant = (
        "=== 테넌트/스킬 경로(필수) ===\n"
        f"- tenant_id: {tid}\n"
        f"- skill_root_dir: \"{skill_root}/{{tenant_id}}/\"\n"
        "- 다른 tenant 경로 접근 금지.\n"
    )

    exec_policy = (
        "=== 실행 원칙(핵심) ===\n"
        "- 큰 워크아이템 q([Description]/[Instruction]/[InputData])를 system prompt에 재주입하지 않는다.\n"
        "- 필요한 컨텍스트는 우선 `mem0`/`memento`/`dmn_rule` 또는 도구 실행 결과로 확보한다.\n"
        "- 실행이 가능하면 샌드박스에서 수행하고 stdout/stderr/exit_code로 근거를 남긴다.\n"
    )

    knowledge_tools = (
        "=== 지식/규칙/문서 도구(mem0/memento/dmn_rule) ===\n"
        "- `mem0`: 개인화/누적 지식(선호, 과거 실행 정보, 사용자가 저장한 자격 증명 등). "
        "사람에게 묻기 전에 먼저 조회하는 것을 권장합니다.\n"
        "- `dmn_rule`: 정책/결재/자격/라우팅 등 규칙 기반 의사결정이 필요한 경우 사용합니다.\n"
        "- `memento`: **RAG**. 워크아이템·InputData·지시문에 **문서/파일 URL·스토리지 경로**가 있으면 추측하지 말고 "
        "`memento`로 **적극 조회**해 근거(발췌·출처)를 확보하세요. 오케스트레이터가 `write_todos`로 배정한 \"문서 확인\" 단계는 여기서 수행합니다.\n"
        "- pdf/xlsx/docx/pptx/csv/zip 등 **바이너리·복합 문서**는 `memento`만으로 부족할 수 있으므로, "
        "할당 스킬(SKILL.md)·`read_file`/샌드박스(다운로드·파싱·추출 스크립트)를 조합해 **실제 내용을 읽은 뒤** 결론을 내세요.\n"
        "- 위 도구들이 유의미하면 MCP/무거운 실행 전에 먼저 호출해 파라미터/리스크 결정을 보정하세요."
    )

    skill_tiers = (
        "=== 스킬 해석 3티어(요약) ===\n"
        "- Tier1(FS): `skill_volume_probe` → `ls/read_file/glob` → `sandbox_copy_mounted_skill`\n"
        "- Tier2(MCP): Tier1 실패 시에만 `list_skills/read_skill_document`\n"
        "- Tier3(Sandbox): 문서 확인 후 태스크 전용 스크립트 작성/실행\n"
    )

    doc_required = (
        "=== 문서→물리화→실행(가드) ===\n"
        "- 임의 API/스크립트 추측 금지.\n"
        "- 실행/물리화 전 `read_skill_document`로 사용법을 확인한다.\n"
    )

    sandbox_rules = (
        "=== 샌드박스 실행(가드) ===\n"
        "- 실행 결과는 stdout/stderr/exit_code를 포함해 보고한다.\n"
        "- 생성/수정 파일이 있으면 경로를 명시한다.\n"
    )

    reporting = (
        "=== 보고 형식 ===\n"
        "- 결과: 검증 가능한 값/근거 중심.\n"
        "- 실패: 오류 메시지 요약 + 1회 이상 보정 재시도 후 상태.\n"
    )

    # Agent-specific sections (only built when agent_info is provided).
    agent_identity = ""
    agent_skills_section = ""
    agent_output_format = ""
    agent_tools_section = ""

    if agent_info:
        name = str(agent_info.get("name") or "").strip()
        role = str(agent_info.get("role") or "역할 미지정").strip()
        description = str(agent_info.get("description") or "").strip()
        persona = str(agent_info.get("persona") or "").strip()
        goal = str(agent_info.get("goal") or "").strip()
        skills: list[str] = agent_info.get("skills") or []
        declared_tools: list[str] = agent_info.get("declared_tools") or []
        form_fields: list[dict[str, Any]] | None = agent_info.get("form_fields")

        identity_lines = ["=== 역할/도메인 ===", f"- 이름: {name}", f"- 역할: {role}"]
        if description:
            identity_lines.append(f"- 설명: {description}")
        if persona:
            identity_lines.append(f"- 페르소나: {persona}")
        if goal:
            identity_lines.append(f"- 목표: {goal}")
        agent_identity = "\n".join(identity_lines)

        if skills:
            agent_skills_section = (
                f"=== 할당 스킬 ===\n"
                f"- 할당 스킬: {', '.join(skills)}\n"
                f"- 기본 문서 경로 템플릿: \"{skill_root}/{{tenant_id}}/{{skill_name}}/SKILL.md\""
            )
        else:
            agent_skills_section = (
                f"=== 할당 스킬 ===\n"
                f"- 할당 스킬: (명시 없음)\n"
                f"- \"{skill_root}/{{tenant_id}}/\" 아래를 탐색해 가장 관련 높은 스킬 폴더를 선택하세요."
            )

        if isinstance(form_fields, list) and form_fields:
            field_lines = [
                "=== 출력 형식(필수 — 폼 JSON) ===",
                "- 이 태스크의 **최종 출력은 반드시** 아래 폼 필드를 키로 하는 순수 JSON 객체여야 합니다.",
                "- 추가 설명 텍스트 없이 JSON 블록만 반환하세요(마크다운 코드펜스 없이).",
                "- 폼 필드 목록:",
            ]
            example_keys: list[str] = []
            for field in form_fields:
                if not isinstance(field, dict):
                    continue
                fid = str(field.get("id") or field.get("key") or field.get("name") or "").strip()
                flabel = str(field.get("label") or field.get("title") or fid).strip()
                ftype = str(field.get("type") or field.get("fieldType") or "string").strip()
                req_mark = " [필수]" if field.get("required", False) else ""
                field_lines.append(f"  - {fid} ({ftype}){req_mark}: {flabel}")
                if fid:
                    example_keys.append(fid)
            if example_keys:
                example = {k: "..." for k in example_keys}
                field_lines.append(f"\n출력 예시:\n{json.dumps(example, ensure_ascii=False, indent=2)}")
            agent_output_format = "\n".join(field_lines)

        if declared_tools:
            agent_tools_section = "=== 선언된 도구(참고) ===\n- " + ", ".join(declared_tools)

    return {
        "clock": clock.strip(),
        "lang": lang.strip(),
        "sub_base_identity": base_identity.strip(),
        "sub_priorities": priorities.strip(),
        "sub_tenant": tenant.strip(),
        "sub_exec_policy": exec_policy.strip(),
        "sub_knowledge_tools": knowledge_tools.strip(),
        "sub_skill_tiers": skill_tiers.strip(),
        "sub_doc_required": doc_required.strip(),
        "sub_sandbox_rules": sandbox_rules.strip(),
        "sub_reporting": reporting.strip(),
        "agent_identity": agent_identity.strip(),
        "agent_skills": agent_skills_section.strip(),
        "agent_output_format": agent_output_format.strip(),
        "agent_tools": agent_tools_section.strip(),
    }
