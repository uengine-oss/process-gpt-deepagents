"""Local replacements for SDK AgentExecutor, RequestContext, and EventQueue."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Literal

TaskState = Literal["working", "completed"]


class RequestContext:
    """Context for a single task (replaces SDK RequestContext)."""

    def __init__(self, task_row: dict[str, Any]) -> None:
        self._row = task_row
        extras = task_row.get("extras")
        if isinstance(extras, str):
            import json
            try:
                extras = json.loads(extras)
            except Exception:
                extras = {}
        self._extras = extras or {}

    def get_user_input(self) -> str:
        """Return the user task content (query)."""
        return (
            self._row.get("description")
            or self._row.get("name")
            or self._row.get("title")
            or self._row.get("user_input")
            or ""
        )

    def get_context_data(self) -> dict[str, Any]:
        """Return row and extras for the executor."""
        return {"row": self._row, "extras": self._extras}


class EventQueue:
    """In-memory event queue (replaces SDK EventQueue)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def enqueue_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class ExecutionContext:
    """Execution-scoped context for multi-agent runs and tracing.

    This is kept intentionally small and domain-agnostic so it can be reused
    across different orchestrators and agent implementations.
    """

    def __init__(
        self,
        *,
        correlation_id: str,
        agent_path: list[str] | None = None,
        current_agent: str | None = None,
        task_id: str | None = None,
        proc_inst_id: str | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self.agent_path: list[str] = list(agent_path) if agent_path else []
        # If current_agent is not provided, derive it from the path when possible.
        self.current_agent: str | None = current_agent or (
            self.agent_path[-1] if self.agent_path else None
        )
        self.task_id = task_id
        self.proc_inst_id = proc_inst_id

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

    @contextmanager
    def with_agent(self, agent_name: str):
        """Context manager that pushes/pops agent_name on the path."""
        self.push_agent(agent_name)
        try:
            yield self
        finally:
            self.pop_agent()

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
    """Build a standardised events.data payload for execution tracing and UI.

    - `meta`: execution-scoped metadata (correlation_id, agent_path, phase, etc.)
    - `payload`: user-facing body for this event (message, plan, tool IO, etc.)
    """
    meta: dict[str, Any] = {}
    if exec_ctx is not None:
        meta.update(exec_ctx.as_dict())

    # Allow caller to override / specify agent_name explicitly.
    if agent_name is not None:
        meta["agent_name"] = agent_name

    meta["logical_event_type"] = logical_event_type
    meta["phase"] = phase

    if tool_name is not None:
        meta["tool_name"] = tool_name
    if requires_human is not None:
        meta["requires_human"] = requires_human
    if input_schema is not None:
        meta["input_schema"] = input_schema
    if output_schema is not None:
        meta["output_schema"] = output_schema

    payload_body: dict[str, Any] = {}
    if extra_data:
        payload_body.update(extra_data)

    return {
        "meta": meta,
        "payload": payload_body,
    }


def new_agent_text_message(
    text: str,
    proc_inst_id: str,
    task_id: str,
) -> str:
    """Build message string for status events (A2A-style)."""
    return text


def new_text_artifact(
    name: str,
    description: str,
    text: str,
) -> dict[str, Any]:
    """Build artifact dict for TaskArtifactUpdateEvent."""
    return {"name": name, "description": description, "text": text}


class AgentExecutor(ABC):
    """Abstract executor interface (replaces SDK AgentExecutor)."""

    @abstractmethod
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> str:
        """Run the task. Returns the draft result string (JSON) to write to DB."""
        ...

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        """Called when the task is cancelled."""
        pass
