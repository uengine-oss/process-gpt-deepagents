"""Deterministic helpers + observability for Tier1→2→3 skill resolution."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    from langchain.tools import tool
except Exception:  # pragma: no cover
    def tool(func=None, **_kwargs):  # type: ignore
        if func is None:
            return lambda f: f
        return func

logger = logging.getLogger(__name__)

SKILLS_ROOT_DIR = (os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")


@tool
def skill_volume_probe(tenant_id: str, skill_names_csv: str) -> str:
    """Tier 1 (filesystem): check mounted volume for SKILL.md under each skill folder.

    skill_names_csv: comma-separated folder names under ``<SKILLS_ROOT>/<tenant_id>/``.
    Returns JSON with found paths, missing names, and whether Tier 1 satisfied any skill.
    """
    tenant = (tenant_id or "").strip().strip("/")
    names = [s.strip() for s in (skill_names_csv or "").split(",") if s.strip()]
    base = Path(SKILLS_ROOT_DIR).resolve() / tenant if tenant else Path(SKILLS_ROOT_DIR).resolve()
    found: list[str] = []
    missing: list[str] = []
    for name in names:
        p = (base / name / "SKILL.md").resolve()
        try:
            p.relative_to(base.resolve())
        except ValueError:
            missing.append(name)
            continue
        if p.is_file():
            found.append(str(p))
        else:
            missing.append(name)
    payload = {
        "tier": 1,
        "skills_root": SKILLS_ROOT_DIR,
        "tenant_id": tenant or None,
        "requested": names,
        "found_skill_md": found,
        "missing": missing,
        "tier1_any_hit": bool(found),
        "catalog_note": (
            "Skills from GitHub sources (list_skills / MCP) may not exist under "
            "<skills_root>/<tenant_id>/<name>/ on the volume. "
            "sandbox_copy_mounted_skill can still materialize them via claude-skills HTTP API "
            "when PROCESSGPT_SKILLS_API_BASE_URL is set (e.g. http://claude-skills:8765 in Compose)."
        ),
    }
    log_rec = {
        "event": "skill_volume_probe",
        "tier": 1,
        "skills_root": SKILLS_ROOT_DIR,
        "tenant_id": tenant or None,
        "requested": names,
        "found_count": len(found),
        "missing_count": len(missing),
        "tier1_any_hit": bool(found),
    }
    logger.info("skill_volume_probe %s", json.dumps(log_rec, ensure_ascii=False))
    return json.dumps(payload, ensure_ascii=False)


@tool
def report_skill_resolution_tier(
    tier: int,
    status: str,
    detail: str = "",
) -> str:
    """Log Tier 1/2/3 progress for observability (structured JSON log line).

    tier: 1=file volume, 2=MCP claude-skills, 3=sandbox code execution.
    status: e.g. started, success, failed, skipped.
    """
    rec = {
        "event": "skill_resolution_tier",
        "tier": int(tier),
        "status": (status or "").strip(),
        "detail": (detail or "").strip()[:4000],
    }
    logger.info("skill_resolution_tier %s", json.dumps(rec, ensure_ascii=False))
    return json.dumps({"logged": True, **rec}, ensure_ascii=False)


def get_skill_resolution_tools() -> list[Any]:
    return [skill_volume_probe, report_skill_resolution_tier]
