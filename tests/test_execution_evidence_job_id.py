"""execution_evidence must key tools_used by subagent context job_id."""

from __future__ import annotations

from app.executor.evidence import _build_execution_evidence


def test_build_execution_evidence_uses_ctx_job_id_for_tool_log() -> None:
    subagent_contexts = [
        {
            "name": "Agent A",
            "role": "r1",
            "job_id": "jid-a",
            "exec_ctx": None,
        }
    ]
    subagent_tool_log = {"jid-a": ["read_file", "sandbox_run_shell"]}
    subagent_skill_log = {"jid-a": ["my-skill"]}
    ev = _build_execution_evidence(
        raw_text="",
        wrapped_result={"json": None},
        subagent_contexts=subagent_contexts,
        subagent_tool_log=subagent_tool_log,
        subagent_skill_log=subagent_skill_log,
    )
    runs = ev.get("subagent_runs") or []
    assert len(runs) == 1
    assert runs[0]["job_id"] == "jid-a"
    assert "read_file" in runs[0]["tools_used"]
    assert "sandbox_run_shell" in runs[0]["tools_used"]
    assert "my-skill" in runs[0]["skills_used"]
