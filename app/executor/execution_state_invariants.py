"""Structured state invariants (spec §6.2) — optional strict enforcement via env flags."""

from __future__ import annotations

from typing import Any


def _assets_from_roles(ar: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(ar, dict):
        return []
    raw = ar.get("assets")
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict)]


def strict_pipeline_state_violations(
    *,
    pipeline_asset_roles: dict[str, Any] | None,
    pipeline_outputs: dict[str, Any] | None,
) -> list[str]:
    """Return violation messages when required outputs are missing for given asset roles."""
    errors: list[str] = []
    po = pipeline_outputs if isinstance(pipeline_outputs, dict) else {}
    assets = _assets_from_roles(pipeline_asset_roles if isinstance(pipeline_asset_roles, dict) else None)
    has_template = any(str(a.get("role") or "").strip() == "template_candidate" for a in assets)
    has_bpmn = any(str(a.get("type") or "").strip().lower() == "bpmn" for a in assets)
    has_ref_pdf = any(
        str(a.get("type") or "").strip().lower() == "pdf"
        and str(a.get("role") or "").strip() == "reference_material"
        for a in assets
    )

    def _nonempty(key: str) -> bool:
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

    if has_template and not _nonempty("template_schema"):
        errors.append("invariant: template_candidate 자산이 있으나 pipeline_outputs.template_schema 가 비어 있음")
    if has_bpmn and not _nonempty("process_facts"):
        errors.append("invariant: BPMN 자산이 있으나 pipeline_outputs.process_facts 가 비어 있음")
    if has_ref_pdf and not _nonempty("reference_facts"):
        errors.append(
            "invariant: reference_material PDF가 있으나 pipeline_outputs.reference_facts 가 비어 있음"
        )
    return errors
