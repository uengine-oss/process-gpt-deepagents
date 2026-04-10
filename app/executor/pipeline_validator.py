"""Validate planner-produced pipeline specs (generic dependency rules, not domain cases)."""

from __future__ import annotations

from typing import Any

STEP_KINDS = frozenset(
    {
        "extract_structure",
        "extract_facts",
        "map_to_template",
        "render_artifact",
        "publish_artifact",
    }
)


def _step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or "").strip()


def _normalize_asset_roles(spec: dict[str, Any]) -> dict[str, Any]:
    ar = spec.get("asset_roles")
    return ar if isinstance(ar, dict) else {}


def _assets_from_roles(asset_roles: dict[str, Any]) -> list[dict[str, Any]]:
    assets = asset_roles.get("assets")
    if not isinstance(assets, list):
        return []
    return [a for a in assets if isinstance(a, dict)]


def _has_template_candidate(asset_roles: dict[str, Any]) -> bool:
    for a in _assets_from_roles(asset_roles):
        if str(a.get("role") or "").strip() == "template_candidate":
            return True
    return False


def _steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw = spec.get("steps")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def _renders_file(step: dict[str, Any]) -> bool:
    if _step_kind(step) != "render_artifact":
        return False
    fmt = str(step.get("format") or "").strip().lower()
    file_like = {
        "xlsx",
        "xls",
        "csv",
        "pdf",
        "docx",
        "pptx",
        "zip",
        "file",
        "artifact",
    }
    return bool(fmt) and fmt in file_like


def _first_kind_indices(kinds: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, k in enumerate(kinds):
        if k not in out:
            out[k] = i
    return out


def _final_state_has_public_url(state: dict[str, Any]) -> bool:
    for v in state.values():
        if isinstance(v, dict):
            u = v.get("public_url")
            if isinstance(u, str) and u.strip():
                return True
    return False


def validate_pipeline_execution_state(spec: dict[str, Any], state: dict[str, Any]) -> tuple[bool, list[str]]:
    """After running a pipeline: if spec includes render_artifact, require a usable public_url in state."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return False, ["pipeline_spec must be an object"]
    kinds = [_step_kind(s) for s in _steps(spec)]
    if "render_artifact" not in kinds:
        return True, []
    if not _final_state_has_public_url(state):
        errors.append(
            "render_artifact was in the pipeline but final execution state has no non-empty public_url"
        )
    return len(errors) == 0, errors


def validate_pipeline_spec(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, errors). Rules are structural, not tied to specific filenames."""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return False, ["pipeline_spec must be an object"]

    asset_roles = _normalize_asset_roles(spec)
    steps = _steps(spec)
    if not steps:
        errors.append("steps must be a non-empty list")

    kinds = [_step_kind(s) for s in steps]
    for i, k in enumerate(kinds):
        if k not in STEP_KINDS:
            errors.append(f"step {i}: unknown kind {k!r}")

    has_extract_structure = any(k == "extract_structure" for k in kinds)
    has_extract_facts = any(k == "extract_facts" for k in kinds)
    has_extract = has_extract_structure or has_extract_facts
    has_map = any(k == "map_to_template" for k in kinds)
    has_render = any(k == "render_artifact" for k in kinds)
    has_publish = any(k == "publish_artifact" for k in kinds)

    has_tc = _has_template_candidate(asset_roles)
    if has_tc:
        required_chain = (
            "extract_structure",
            "map_to_template",
            "render_artifact",
            "publish_artifact",
        )
        idx = _first_kind_indices(kinds)
        for rk in required_chain:
            if rk not in idx:
                errors.append(f"template_candidate requires {rk} step")
        if all(rk in idx for rk in required_chain):
            if not (
                idx["extract_structure"]
                < idx["map_to_template"]
                < idx["render_artifact"]
                < idx["publish_artifact"]
            ):
                errors.append(
                    "template_candidate: step order must be "
                    "extract_structure → map_to_template → render_artifact → publish_artifact"
                )

    if has_map and not has_extract_structure:
        errors.append("map_to_template requires extract_structure (template_schema)")

    if has_render:
        if not has_publish:
            errors.append("render_artifact requires publish_artifact")
        if not has_extract:
            errors.append("forbidden: render_artifact without any extract step")
        if not has_tc and not (has_map or has_extract_structure):
            errors.append("render_artifact requires map_to_template or extract_structure beforehand")

    any_file_out = any(_renders_file(s) for s in steps)
    if any_file_out and not has_publish:
        errors.append("file render requires publish_artifact")

    if has_render and has_publish:
        ri = next((i for i, k in enumerate(kinds) if k == "render_artifact"), None)
        pi = next((i for i, k in enumerate(kinds) if k == "publish_artifact"), None)
        if ri is not None and pi is not None and pi < ri:
            errors.append("publish_artifact must come after render_artifact")

    ok = len(errors) == 0
    return ok, errors
