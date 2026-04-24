"""Subagent prompt sections builder.

Goal: keep delegated task() model calls small and stable.

Unlike the root orchestrator, subagents already receive their main task context via the
`task(description=...)` handoff. Therefore we avoid injecting large work-item q blobs
([Description]/[Instruction]/[InputData]) into system prompts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


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
) -> dict[str, str]:
    """Return sections for step-aware subagent prompt composition.

    Keep these sections short. The delegated task context must come from `task()` description.
    """
    tid = (tenant_id or "").strip() or "(unknown)"
    skill_root_dir = (skill_root_dir or "").strip().rstrip("/") or "/app/skills"

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

    base_identity = "당신은 ProcessGPT의 도메인 실행 서브에이전트입니다."

    priorities = (
        "=== 우선순위(짧게) ===\n"
        "- 1) `task()` 위임(description)의 지시/출력 형식을 최우선으로 따른다.\n"
        "- 2) 추정 금지: 문서/실행 결과로 검증한다.\n"
        "- 3) 가능하면 실행(샌드박스)하고, 불가하면 사유+다음 시도안을 짧게 보고한다.\n"
    )

    tenant = (
        "=== 테넌트/스킬 경로(필수) ===\n"
        f"- tenant_id: {tid}\n"
        f"- skill_root_dir: \"{skill_root_dir}/{{tenant_id}}/\"\n"
        "- 다른 tenant 경로 접근 금지.\n"
    )

    exec_policy = (
        "=== 실행 원칙(핵심) ===\n"
        "- 큰 워크아이템 q([Description]/[Instruction]/[InputData])를 system prompt에 재주입하지 않는다.\n"
        "- 필요한 컨텍스트는 우선 `mem0`/`memento`/`dmn_rule` 또는 도구 실행 결과로 확보한다.\n"
        "- 실행이 가능하면 샌드박스에서 수행하고 stdout/stderr/exit_code로 근거를 남긴다.\n"
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
        "=== 보고 형식(짧게) ===\n"
        "- 결과: 검증 가능한 값/근거 중심.\n"
        "- 실패: 오류 메시지 요약 + 1회 이상 보정 재시도 후 상태.\n"
    )

    return {
        "clock": clock.strip(),
        "lang": lang.strip(),
        "sub_base_identity": base_identity.strip(),
        "sub_priorities": priorities.strip(),
        "sub_tenant": tenant.strip(),
        "sub_exec_policy": exec_policy.strip(),
        "sub_skill_tiers": skill_tiers.strip(),
        "sub_doc_required": doc_required.strip(),
        "sub_sandbox_rules": sandbox_rules.strip(),
        "sub_reporting": reporting.strip(),
    }

