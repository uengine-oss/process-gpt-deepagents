"""Best-effort checks that native multi-agent runs mention expected structured outputs (spec §6.2).

This does not read LangGraph custom state keys (deepagents may not expose them); it scans
serialized conversation text for contract markers after a run.
"""

from __future__ import annotations

import json
from typing import Any


def _assets(pipeline_asset_roles: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(pipeline_asset_roles, dict):
        return []
    raw = pipeline_asset_roles.get("assets")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict)]


def _pipeline_output_nonempty(po: dict[str, Any], key: str) -> bool:
    v = po.get(key)
    if v is None:
        return False
    if isinstance(v, dict) and not v:
        return False
    if isinstance(v, list) and not v:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def validate_native_execution_contract(
    *,
    pipeline_asset_roles: dict[str, Any] | None,
    messages_blob: str,
    pipeline_outputs: dict[str, Any] | None = None,
) -> list[str]:
    """Return human-readable warnings (empty if nothing to flag)."""
    warnings: list[str] = []
    po = pipeline_outputs if isinstance(pipeline_outputs, dict) else {}
    blob = (messages_blob or "").lower()

    assets = _assets(pipeline_asset_roles)
    has_template = any(str(a.get("role") or "").strip() == "template_candidate" for a in assets)
    has_bpmn = any(str(a.get("type") or "").strip().lower() == "bpmn" for a in assets)
    has_pdf_ref = any(
        str(a.get("type") or "").strip().lower() == "pdf"
        and str(a.get("role") or "").strip() == "reference_material"
        for a in assets
    )

    def _missing(marker: str) -> bool:
        return marker.lower() not in blob

    if has_template and not _pipeline_output_nonempty(po, "template_schema") and _missing("template_schema"):
        warnings.append(
            "template_candidate 자산이 있는데 대화/도구 출력에 'template_schema' 흔적이 없습니다. "
            "템플릿 PDF 구조 추출 단계가 수행됐는지 확인하세요."
        )
    if has_bpmn and not _pipeline_output_nonempty(po, "process_facts") and _missing("process_facts"):
        warnings.append(
            "BPMN 자산이 있는데 'process_facts' 흔적이 없습니다. BPMN 사실 추출 단계 위임을 확인하세요."
        )
    if has_pdf_ref and not _pipeline_output_nonempty(po, "reference_facts") and _missing("reference_facts"):
        warnings.append(
            "reference_material PDF가 있는데 'reference_facts' 흔적이 없습니다. 참고 문서 사실 추출 단계 위임을 확인하세요."
        )

    if blob.strip() and (
        "xlsx" in blob or "spreadsheet" in blob or "openpyxl" in blob
    ):
        pu_ok = _pipeline_output_nonempty(po, "public_url")
        if not pu_ok and _missing("public_url") and _missing("http"):
            warnings.append(
                "엑셀/스프레드시트 관련 출력이 있으나 public URL 흔적이 약합니다. 업로드 단계를 확인하세요."
            )

    return warnings


def messages_to_audit_blob(messages: Any, *, max_chars: int = 1_200_000) -> str:
    """Flatten LangGraph messages list/dict to a single searchable string."""
    parts: list[str] = []
    try:
        raw = json.dumps(messages, ensure_ascii=False)
        parts.append(raw)
    except Exception:
        parts.append(str(messages))
    s = "\n".join(parts)
    if len(s) > max_chars:
        return s[: max_chars // 2] + "\n…\n" + s[-max_chars // 2 :]
    return s
