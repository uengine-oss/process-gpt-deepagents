"""align_tool_event_job_id: 서브 도구 이벤트 job_id 를 agent_path·바인딩으로 보정."""

from __future__ import annotations

from app.executor.context import ExecutionContext
from app.skills.volume_tool_events import (
    SkillVolumeToolEventBinding,
    align_tool_event_job_id,
    skill_volume_tool_event_binding,
)


def test_align_root_depth1_unchanged() -> None:
    ctx = ExecutionContext(correlation_id="c1", agent_path=["작업 실행 기획자"])
    assert align_tool_event_job_id(resolved_job_id="root-uuid", exec_ctx=ctx) == "root-uuid"


def test_align_subagent_maps_via_binding() -> None:
    binding = SkillVolumeToolEventBinding(
        event_queue=None,
        todo_id="t1",
        proc_inst_id="p1",
        crew_type="deepagents",
        default_job_id="root-uuid",
        root_exec_ctx=None,
        subagent_name_to_job_id={"도메인에이전트": "sub-job-uuid"},
    )
    tok = skill_volume_tool_event_binding.set(binding)
    try:
        ctx = ExecutionContext(
            correlation_id="c1",
            agent_path=["작업 실행 기획자", "도메인에이전트"],
        )
        assert (
            align_tool_event_job_id(resolved_job_id="root-uuid", exec_ctx=ctx) == "sub-job-uuid"
        )
    finally:
        skill_volume_tool_event_binding.reset(tok)
