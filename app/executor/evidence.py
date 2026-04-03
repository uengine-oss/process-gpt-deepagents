"""Execution evidence payload for final result wrapping."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _build_execution_evidence(
    *,
    raw_text: str,
    wrapped_result: dict[str, Any],
    subagent_contexts: list[dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
    subagent_skill_log: dict[str, list[str]],
    structured_domain_outputs_by_job: dict[str, Any] | None = None,
    structured_skill_handoff_by_job: dict[str, Any] | None = None,
    deliverable_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build internal evidence layer separated from user-facing final output."""
    subagents: list[dict[str, Any]] = []
    for ctx in subagent_contexts:
        job_id = str(ctx.get("job_id") or "")
        used_tools = sorted(set(subagent_tool_log.get(job_id, [])))
        used_skills = sorted(set(subagent_skill_log.get(job_id, [])))
        subagents.append(
            {
                "name": ctx.get("name"),
                "role": ctx.get("role"),
                "job_id": job_id,
                "tools_used": used_tools,
                "skills_used": used_skills,
            }
        )

    parsed = wrapped_result.get("json")
    extracted_fields: dict[str, Any] = {}
    if isinstance(parsed, dict):
        for key in ("source", "endpoint_or_series", "as_of", "value", "unit"):
            if key in parsed:
                extracted_fields[key] = parsed.get(key)

    out_ev: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "subagent_runs": subagents,
        "model_output_cleaned": wrapped_result.get("cleaned") or raw_text or "",
        "extracted_fields": extracted_fields,
        "structured_domain_outputs_by_job": structured_domain_outputs_by_job or {},
        "structured_skill_handoff_by_job": structured_skill_handoff_by_job or {},
    }
    if deliverable_files:
        out_ev["deliverable_files"] = deliverable_files
    return out_ev
