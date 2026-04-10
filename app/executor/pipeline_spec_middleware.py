"""Middleware: ``write_pipeline_spec`` tool + state field ``pipeline_spec``."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Sequence, cast

try:
    from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest, OmitFromInput
except ImportError:  # pragma: no cover
    AgentMiddleware = None  # type: ignore[misc, assignment]
    AgentState = dict  # type: ignore[misc, assignment]
    ModelRequest = Any  # type: ignore[misc, assignment]
    OmitFromInput = Any  # type: ignore[misc, assignment]
from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.types import Command
from typing_extensions import NotRequired, TypedDict, override

from app.executor.a2a_events import _enqueue_status
from app.executor.pipeline_validator import validate_pipeline_spec

logger = logging.getLogger(__name__)

try:
    from langgraph.runtime import Runtime
except Exception:  # pragma: no cover
    Runtime = Any  # type: ignore[misc, assignment]

ContextT = Any
ResponseT = Any


def _merge_pipeline_outputs(left: Any, right: Any) -> dict[str, Any]:
    """Shallow-merge pipeline output blobs; non-None values from ``right`` win."""
    a = left if isinstance(left, dict) else {}
    b = right if isinstance(right, dict) else {}
    out = dict(a)
    for k, v in b.items():
        if v is not None:
            out[k] = v
    return out


if AgentMiddleware is not None:

    class PipelineSpecAgentState(AgentState[ResponseT]):
        """Merged graph state: ``pipeline_spec`` + accumulated ``pipeline_outputs``."""

        pipeline_spec: Annotated[NotRequired[dict[str, Any]], OmitFromInput]
        pipeline_outputs: Annotated[NotRequired[dict[str, Any]], _merge_pipeline_outputs]

else:  # pragma: no cover
    PipelineSpecAgentState = dict  # type: ignore[misc, assignment]


WRITE_PIPELINE_SPEC_DESCRIPTION = """Submit a machine-executable pipeline plan.

Accepted shapes (all equivalent after normalization):
- One object with keys asset_roles and steps (flat)
- One object { "pipeline_spec": { asset_roles, steps, ... } } (nested)
- pipeline_spec plus optional top-level asset_roles / steps (merged; top-level wins)

Must include asset_roles and steps (see validation errors if missing).

Allowed kinds: extract_structure, extract_facts, map_to_template, render_artifact, publish_artifact.

If validation fails, read the error message and submit a corrected spec."""

_MAX_PIPELINE_SPEC_UNWRAP = 12


def unwrap_nested_pipeline_spec_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Lift repeated ``pipeline_spec`` wrappers until the inner dict has no such key or cap hit."""
    cur: Any = d
    for _ in range(_MAX_PIPELINE_SPEC_UNWRAP):
        if not isinstance(cur, dict):
            return {}
        inner = cur.get("pipeline_spec")
        if isinstance(inner, dict):
            cur = inner
            continue
        break
    return cur if isinstance(cur, dict) else {}


def normalize_from_raw_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Merge tool-call argument dict into one spec dict (handles schema mismatch from LLMs).

    - Pops ``pipeline_spec`` if present and merges its contents first.
    - Merges remaining keys (e.g. ``asset_roles``, ``steps`` at top level).
    - Unwraps any nested ``pipeline_spec`` chains inside the merged object.
    """
    if not isinstance(args, dict):
        return {}
    skip = frozenset({"tool_call_id"})
    filtered = {k: v for k, v in args.items() if k not in skip and v is not None}
    nested = filtered.pop("pipeline_spec", None)
    merged: dict[str, Any] = {}
    if isinstance(nested, dict):
        merged.update(nested)
    merged.update(filtered)
    return unwrap_nested_pipeline_spec_keys(merged)


def _format_pipeline_plan_message(spec: dict[str, Any]) -> str:
    try:
        steps = spec.get("steps") if isinstance(spec, dict) else None
        if not isinstance(steps, list):
            return json.dumps(spec, ensure_ascii=False)[:8000]
        lines: list[str] = []
        for s in steps:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip() or "?"
            kind = str(s.get("kind") or "").strip() or "?"
            lines.append(f"- {sid}: {kind}")
        head = "파이프라인 스펙:\n" + "\n".join(lines)
        if len(head) > 8000:
            return head[:8000] + "…"
        return head
    except Exception:
        return str(spec)[:8000]


def _command_has_pipeline_update(result: Any) -> bool:
    if result is None:
        return False
    try:
        upd = getattr(result, "update", None)
        if isinstance(upd, dict) and "pipeline_spec" in upd:
            return True
    except Exception:
        return False
    return False


def create_pipeline_spec_middleware(
    *,
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    job_uuid: str,
    exec_ctx: Any,
    planning_gate: dict[str, Any],
) -> Any | None:
    """Returns middleware registering ``write_pipeline_spec`` and optional planning events."""
    if AgentMiddleware is None:
        return None

    class PipelineSpecMiddleware(AgentMiddleware[PipelineSpecAgentState, ContextT, ResponseT]):
        state_schema = PipelineSpecAgentState  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()

            @tool(description=WRITE_PIPELINE_SPEC_DESCRIPTION)
            def write_pipeline_spec(
                pipeline_spec: dict[str, Any] | None = None,
                asset_roles: Any | None = None,
                steps: Any | None = None,
                tool_call_id: Annotated[str, InjectedToolCallId] = "",
            ) -> Command[Any]:
                synthetic: dict[str, Any] = {}
                if isinstance(pipeline_spec, dict):
                    synthetic["pipeline_spec"] = pipeline_spec
                if asset_roles is not None:
                    synthetic["asset_roles"] = asset_roles
                if steps is not None:
                    synthetic["steps"] = steps
                effective = normalize_from_raw_tool_args(synthetic)
                ok, errors = validate_pipeline_spec(effective)
                if not ok:
                    payload = {"ok": False, "validation_errors": errors}
                    return Command(
                        update={
                            "messages": [
                                ToolMessage(
                                    content=json.dumps(payload, ensure_ascii=False),
                                    tool_call_id=tool_call_id,
                                    status="error",
                                )
                            ],
                        }
                    )
                return Command(
                    update={
                        "pipeline_spec": effective,
                        "messages": [
                            ToolMessage(
                                content=json.dumps({"ok": True, "accepted": True}, ensure_ascii=False),
                                tool_call_id=tool_call_id,
                            )
                        ],
                    }
                )

            self.tools: Sequence[Any] = [write_pipeline_spec]

        async def awrap_model_call(
            self,
            request: ModelRequest[ContextT],
            handler: Any,
        ) -> Any:
            extra = (
                "\n\n## `write_pipeline_spec`\n"
                "Use `write_pipeline_spec` to submit the execution plan. "
                "Do not use natural-language todos for execution planning.\n"
            )
            if request.system_message is not None:
                new_system_content = [
                    *request.system_message.content_blocks,
                    {"type": "text", "text": extra},
                ]
            else:
                new_system_content = [{"type": "text", "text": extra.lstrip()}]
            new_system_message = SystemMessage(
                content=cast("list[str | dict[str, str]]", new_system_content)
            )
            return await handler(request.override(system_message=new_system_message))

        @override
        async def aafter_model(
            self, state: PipelineSpecAgentState, runtime: Runtime[ContextT]
        ) -> dict[str, Any] | None:
            messages = state["messages"]
            if not messages:
                return None
            last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
            if not last_ai_msg or not last_ai_msg.tool_calls:
                return None
            calls = [tc for tc in last_ai_msg.tool_calls if tc["name"] == "write_pipeline_spec"]
            if len(calls) <= 1:
                return None
            error_messages = [
                ToolMessage(
                    content=(
                        "Error: call `write_pipeline_spec` at most once per model turn "
                        "with a single consolidated pipeline_spec object."
                    ),
                    tool_call_id=tc["id"],
                    status="error",
                )
                for tc in calls
            ]
            return {"messages": error_messages}

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            base_tool = getattr(request, "tool", None)
            tool_name = getattr(base_tool, "name", None) or ""
            if tool_name != "write_pipeline_spec":
                return await handler(request)
            result = await handler(request)
            if _command_has_pipeline_update(result):
                planning_gate["saw_write_pipeline_spec"] = True
                if not planning_gate.get("planning_completed_emitted"):
                    try:
                        args = {}
                        try:
                            from app.executor.tool_calls import _tool_call_args_from_request

                            args = _tool_call_args_from_request(request) or {}
                        except Exception:
                            logger.exception("Failed to read write_pipeline_spec args", exc_info=True)
                        spec = normalize_from_raw_tool_args(args) if isinstance(args, dict) else {}
                        plan_message = ""
                        if spec:
                            plan_message = _format_pipeline_plan_message(spec)
                        _enqueue_status(
                            event_queue,
                            "task_completed",
                            {"message": plan_message or "파이프라인 스펙이 확정되었습니다."},
                            proc_inst_id,
                            task_id,
                            "action",
                            job_id=job_uuid,
                            exec_ctx=exec_ctx,
                            logical_event_type="agent_finished",
                            phase="planning",
                            agent_name="작업 실행 기획자",
                        )
                        planning_gate["planning_completed_emitted"] = True
                    except Exception:
                        logger.exception("Failed to enqueue planning task_completed for pipeline_spec", exc_info=True)
            return result

    return PipelineSpecMiddleware()
