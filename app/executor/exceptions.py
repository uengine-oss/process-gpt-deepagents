"""Executor-specific exceptions."""

from __future__ import annotations

from typing import Any


class MissingSubagentsConfigurationError(RuntimeError):
    """Raised when no subagents are configured; the orchestrator must not run alone."""

    friendly: str

    def __init__(self, friendly: str | None = None) -> None:
        msg = friendly or (
            "이 할 일에 서브 에이전트가 연결되어 있지 않습니다. "
            "활동에 최소 한 명의 에이전트를 지정한 뒤 다시 실행해 주세요."
        )
        self.friendly = msg
        super().__init__(msg)


class HitlInterrupt(Exception):
    """Exception used to signal a HITL interrupt from inside a tool call.

    Tool implementations can return a structured payload such as:
        {\"requires_human\": true, \"question\": \"...\", \"choices\": [...], ...}
    and the instrumentation layer will raise this exception so that the
    executor can stop the run and hand control back to the caller.
    """

    def __init__(self, *, decision: dict[str, Any], tool_name: str, job_id: str) -> None:
        super().__init__(decision.get("reason") or decision.get("question") or "HITL interrupt requested.")
        self.decision = decision
        self.tool_name = tool_name
        self.job_id = job_id
