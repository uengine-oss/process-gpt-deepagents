"""ExecutionContext, event payload builder, and execution evidence for A2A."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class ExecutionContext:
    """Execution-scoped context for multi-agent runs and tracing.

    DeepAgents 오케스트레이션에 공통으로 필요한 상위 메타데이터(correlation_id,
    agent_path, task_id, proc_inst_id 등)를 담고, events.data.meta 에 직렬화하기
    위한 최소 정보를 제공한다.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        agent_path: list[str] | None = None,
        current_agent: str | None = None,
        task_id: str | None = None,
        proc_inst_id: str | None = None,
        agent_role: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.agent_path: list[str] = list(agent_path) if agent_path else []
        # If current_agent is not provided, derive it from the path when possible.
        self.current_agent: str | None = current_agent or (
            self.agent_path[-1] if self.agent_path else None
        )
        self.task_id = task_id
        self.proc_inst_id = proc_inst_id
        _ar = (agent_role or "").strip()
        self.agent_role: str | None = _ar or None

    @property
    def depth(self) -> int:
        return len(self.agent_path)

    def push_agent(self, agent_name: str) -> None:
        self.agent_path.append(agent_name)
        self.current_agent = agent_name

    def pop_agent(self) -> str | None:
        if not self.agent_path:
            return None
        popped = self.agent_path.pop()
        self.current_agent = self.agent_path[-1] if self.agent_path else None
        return popped

    def as_dict(self) -> dict[str, Any]:
        """Return a minimal, serialisable snapshot of execution metadata."""
        meta: dict[str, Any] = {
            "correlation_id": self.correlation_id,
            "agent_path": list(self.agent_path),
            "agent_name": self.current_agent,
            "depth": self.depth,
        }
        if self.task_id is not None:
            meta["task_id"] = self.task_id
        if self.proc_inst_id is not None:
            meta["proc_inst_id"] = self.proc_inst_id
        if self.agent_role:
            meta["role"] = self.agent_role
        return meta


def build_event_payload(
    exec_ctx: ExecutionContext | None,
    *,
    logical_event_type: str,
    phase: str,
    tool_name: str | None = None,
    requires_human: bool | None = None,
    input_schema: Any | None = None,
    output_schema: Any | None = None,
    agent_name: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """events.data 로 저장될 **평탄화(flat)** 된 JSON 페이로드를 생성한다.

    - correlation_id, agent_path, phase 등 실행 메타데이터와
      goal, plans, tool_name 같은 도메인 필드를 **동일한 깊이의 키**로 둔다.
    """
    merged: dict[str, Any] = {}

    # 실행 컨텍스트 메타데이터 먼저 병합
    if exec_ctx is not None:
        merged.update(exec_ctx.as_dict())

    # 호출 측에서 명시한 agent_name 이 있다면 덮어쓰기
    if agent_name is not None:
        merged["agent_name"] = agent_name

    # 논리적 이벤트 타입 / 페이즈 정보
    merged["logical_event_type"] = logical_event_type
    merged["phase"] = phase

    # 선택적 필드들
    if tool_name is not None:
        merged["tool_name"] = tool_name
    if requires_human is not None:
        merged["requires_human"] = requires_human
    if input_schema is not None:
        merged["input_schema"] = input_schema
    if output_schema is not None:
        merged["output_schema"] = output_schema

    # 도메인 데이터(목표, 플랜, 메시지 등)를 같은 레벨에 병합
    if extra_data:
        merged.update(extra_data)

    return merged


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
