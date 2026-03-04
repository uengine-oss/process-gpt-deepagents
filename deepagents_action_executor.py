"""DeepAgents executor: create_deep_agent only; no process-gpt / a2a."""

from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from typing import Any

from agent_execution import (
    AgentExecutor,
    EventQueue,
    ExecutionContext,
    build_event_payload,
)
from agent_loader import build_subagent_configs
from database import insert_event
from deep_agent_factory import (
    build_orchestrator,
    build_orchestrator_system_prompt,
    default_orchestrator_prompt,
)
from mcp_loader import load_default_mcp_tools
from output_utils import convert_agent_output
from processgpt_agent_utils.utils.context_manager import set_context

logger = logging.getLogger(__name__)

try:
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain.agents.middleware.types import ToolCallRequest
except ImportError:
    AgentMiddleware = None  # type: ignore[misc, assignment]
    ToolCallRequest = None  # type: ignore[misc, assignment]


def _enqueue_status(
    eq: EventQueue,
    event_type: str,
    data: dict[str, Any],
    proc_inst_id: str,
    task_id: str,
    crew_type: str,
    job_id: str | None = None,
    *,
    exec_ctx: ExecutionContext | None = None,
    logical_event_type: str | None = None,
    phase: str | None = None,
    agent_name: str | None = None,
) -> None:
    """Enqueue a status event with optional execution context metadata."""
    effective_job_id = job_id or str(uuid.uuid4())
    logical = logical_event_type
    if logical is None:
        logical = "agent_started" if event_type == "task_started" else "agent_finished"
    effective_phase = phase or "reasoning"
    payload = build_event_payload(
        exec_ctx,
        logical_event_type=logical,
        phase=effective_phase,
        agent_name=agent_name,
        extra_data=data,
    )
    eq.enqueue_event(
        {
            "event_type": event_type,
            "job_id": effective_job_id,
            "crew_type": crew_type,
            "data": payload,
        }
    )


def _enqueue_artifact(
    eq: EventQueue,
    name: str,
    description: str,
    text: Any,
    proc_inst_id: str,
    task_id: str,
    *,
    exec_ctx: ExecutionContext | None = None,
) -> None:
    data = build_event_payload(
        exec_ctx,
        logical_event_type="artifact",
        phase="final",
        extra_data={"name": name, "description": description, "text": text},
    )
    eq.enqueue_event(
        {
            "event_type": "task_completed",
            "job_id": "artifact",
            "crew_type": "result",
            "data": data,
            "text": text,
            "last_chunk": True,
        }
    )


def _create_subagent_event_middleware(
    event_queue: EventQueue,
    proc_inst_id: str,
    task_id: str,
    name_to_context: dict[str, dict[str, Any]],
    subagent_tool_log: dict[str, list[str]],
) -> Any:
    """Create middleware that emits task_started/task_completed around each subagent (task tool) run.

    Only used when AgentMiddleware is available; otherwise returns None and executor falls back
    to emitting subagent events after the run.
    """
    if AgentMiddleware is None or ToolCallRequest is None:
        return None

    class SubagentEventMiddleware(AgentMiddleware):
        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            tool_name = getattr(getattr(request, "tool", None), "name", None)
            if tool_name != "task":
                return await handler(request)

            call = getattr(request, "call", None) or {}
            args = call.get("args", {}) if isinstance(call, dict) else {}
            subagent_type = args.get("subagent_type") or args.get("subagent_name") or ""
            ctx = name_to_context.get(subagent_type) if subagent_type else None
            if not ctx:
                return await handler(request)

            job_id = ctx["job_id"]
            _enqueue_status(
                event_queue,
                "task_started",
                {
                    "role": ctx["role"],
                    "name": ctx["name"],
                    "goal": ctx["goal"],
                    "agent_profile": ctx["agent_profile"],
                    "agent_id": ctx.get("agent_id"),
                },
                proc_inst_id,
                task_id,
                "action",
                job_id=job_id,
            )
            try:
                result = await handler(request)
                used_tools = subagent_tool_log.get(job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"수행한_작업": 작업_텍스트},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                )
                return result
            except Exception:
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {"수행한_작업": "작업 실행 중 오류가 발생했습니다."},
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=job_id,
                )
                raise

    return SubagentEventMiddleware()


def _parse_block(source: str, header: str, next_headers: list[str]) -> str:
    """Extract a block that starts after header and ends before any next_headers."""
    if not source:
        return ""
    start_idx = source.find(header)
    if start_idx == -1:
        return ""
    start_idx += len(header)
    tail = source[start_idx:]
    # Find the earliest occurrence of any other header.
    end_idx = len(tail)
    for h in next_headers:
        idx = tail.find(h)
        if idx != -1 and idx < end_idx:
            end_idx = idx
    return tail[:end_idx].strip()


def parse_workitem_query(query: str) -> dict[str, Any]:
    """Parse [Description]/[Instruction]/[InputData] blocks from the work-item query."""
    text = query or ""
    # Normalise newlines for simpler parsing.
    text = text.replace("\r\n", "\n")

    desc = _parse_block(
        text,
        "[Description]",
        ["[Instruction]", "[InputData]"],
    )
    instr = _parse_block(
        text,
        "[Instruction]",
        ["[Description]", "[InputData]"],
    )
    input_raw = _parse_block(
        text,
        "[InputData]",
        ["[Description]", "[Instruction]"],
    )

    input_data: dict[str, Any] = {}
    if input_raw:
        raw = input_raw.strip()
        # Allow optional surrounding code fences or text around JSON.
        json_match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        candidate = json_match.group(0) if json_match else raw
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                input_data = parsed
        except Exception:
            logger.warning("Failed to parse InputData JSON from query; using empty dict.")

    return {
        "description": desc,
        "instruction": instr,
        "input_data_raw": input_raw,
        "input_data": input_data,
    }


def _should_trigger_human_asked(
    description: str,
    instruction: str,
    input_data: dict[str, Any],
) -> tuple[bool, str]:
    """Safety-net heuristics to decide whether to request human-in-the-loop before running.

    This runs before the DeepAgents graph. When DeepAgents graph-level HITL (e.g. interrupt_on
    or credential nodes) is in use, that path takes precedence; this heuristic remains as a
    fallback for approval/sensitivity/missing-InputData signals. Any human_asked event must
    use the unified schema: data = { role, text, type, options }.
    """
    text = f"{description}\n{instruction}".lower()

    approval_keywords = [
        "승인",
        "검토",
        "결재",
        "민감",
        "개인정보",
        "personal data",
        "sensitive",
        "approve",
        "approval",
    ]
    if any(kw in text for kw in approval_keywords):
        return True, "Instruction indicates approval / sensitive data handling."

    # Very simple heuristic: no InputData at all.
    if not input_data:
        missing_keywords = ["추가 정보", "입력", "정보 제공", "데이터"]
        if any(kw in text for kw in missing_keywords):
            return True, "InputData is empty while instructions mention additional information."

    return False, ""


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


def _extract_hitl_decision_from_output(output: Any) -> dict[str, Any] | None:
    """Inspect a tool output for a generic, domain-agnostic HITL signal.

    Contract (domain-independent):
    - If output is a dict and contains `requires_human` set to true, treat it as a
      HITL request. Optional keys:
        - question: str      → what to ask the human
        - choices: list[str] → optional predefined options
        - reason: str        → internal explanation (not user-facing, optional)
        - input_schema/output_schema: any → for replay/diagnostics
    """
    if not isinstance(output, dict):
        return None
    requires = output.get("requires_human")
    if requires is not True:
        return None

    question = output.get("question")
    choices = output.get("choices")
    reason = output.get("reason") or ""
    decision: dict[str, Any] = {
        "requires_human": True,
        "question": question,
        "choices": choices,
        "reason": reason,
    }
    # Pass through optional schema fields if present.
    if "input_schema" in output:
        decision["input_schema"] = output["input_schema"]
    if "output_schema" in output:
        decision["output_schema"] = output["output_schema"]
    # Include the raw output for debugging / context replay.
    decision["raw_payload"] = output
    return decision


def _json_safe(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable structures.

    - dict/list/tuple: recurse into elements
    - other non-serialisable objects: fall back to str()
    """
    try:
        json.dumps(value)
        return value
    except TypeError:
        pass

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [ _json_safe(v) for v in value ]
    # Fallback: string representation
    return str(value)


def _extract_plain_text(value: Any) -> str:
    """Extract plain text from common message/content structures without stringifying objects."""
    if isinstance(value, str):
        # 문자열이 리스트/딕셔너리 표현일 수 있으므로 한 번 파싱을 시도한다.
        text_str = value.strip()
        parsed: Any | None = None
        # JSON 파싱 시도
        try:
            parsed = json.loads(text_str)
        except Exception:
            parsed = None
        # JSON이 아니면 literal_eval 시도 (list/dict 등의 리터럴만 허용)
        if parsed is None:
            try:
                parsed = ast.literal_eval(text_str)
            except Exception:
                parsed = None

        if isinstance(parsed, (list, dict)):
            # 파싱에 성공했다면, 그 안에서 텍스트 필드만 추출한다.
            return _extract_plain_text(parsed)
        # 일반 문자열이면 그대로 반환
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return ""


def _extract_plan_block(source: str) -> str:
    """Extract the [PLAN]...[/PLAN] block from orchestrator output. Returns plain text only."""
    if not source or not isinstance(source, str):
        return ""
    text = source.strip()
    start_marker = "[PLAN]"
    end_marker = "[/PLAN]"
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    start_idx += len(start_marker)
    end_idx = text.find(end_marker, start_idx)
    if end_idx == -1:
        # No closing tag: take from start to end of string
        block = text[start_idx:].strip()
    else:
        block = text[start_idx:end_idx].strip()
    # Ensure we return plain text (no list/dict string representation)
    return _extract_plain_text(block) if block else ""


def _instrument_tools(
    tools: list[Any],
    *,
    todo_id: str | None,
    proc_inst_id: str | None,
    crew_type: str = "action",
    job_id_getter: callable | None = None,
    job_tool_log: dict[str, list[str]] | None = None,
    exec_ctx: ExecutionContext | None = None,
) -> list[Any]:
    """Wrap tools in-place so that all calls are logged into the events table.

    IMPORTANT: we do NOT change the tool type, only monkey-patch invoke/ainvoke
    on existing tool instances so that DeepAgents / LangChain can still treat
    them as normal tools.
    """
    if not tools:
        return []

    def _resolve_job_id() -> str:
        """Return the job_id to use for this tool call.

        If a job_id_getter is provided and returns a truthy value, that value
        is used so that tool_usage_started/finished share the same job_id as
        the surrounding task_started event. Otherwise, fall back to a fresh
        UUID per tool call.
        """
        if job_id_getter is not None:
            try:
                value = job_id_getter()
                if value:
                    return str(value)
            except Exception:
                logger.exception("job_id_getter failed; falling back to new UUID", exc_info=True)
        return str(uuid.uuid4())

    def _log_started(base_tool: Any, job_id: str, tool_input: Any) -> None:
        try:
            safe_input = _json_safe(tool_input)
            tool_name = getattr(base_tool, "name", base_tool.__class__.__name__)
            payload = build_event_payload(
                exec_ctx,
                logical_event_type="tool_call",
                phase="tool_call",
                tool_name=tool_name,
                extra_data={
                    "tool_name": tool_name,
                    "mcp_server": getattr(base_tool, "_mcp_server", None),
                    "input": safe_input,
                },
            )
            insert_event(
                id=str(uuid.uuid4()),
                job_id=job_id,
                todo_id=str(todo_id) if todo_id else None,
                proc_inst_id=str(proc_inst_id) if proc_inst_id else None,
                event_type="tool_usage_started",
                status=None,
                crew_type=crew_type,
                data=payload,
            )
        except Exception:
            logger.exception("Failed to insert tool_usage_started event", exc_info=True)

    def _log_finished(
        base_tool: Any,
        job_id: str,
        tool_input: Any,
        tool_output: Any,
        error: Exception | None = None,
    ) -> dict[str, Any] | None:
        """Log tool result and return a potential HITL decision (if any)."""
        safe_input = _json_safe(tool_input)
        safe_output = _json_safe(tool_output)
        tool_name = getattr(base_tool, "name", base_tool.__class__.__name__)
        data: dict[str, Any] = {
            "tool_name": tool_name,
            "mcp_server": getattr(base_tool, "_mcp_server", None),
            "input": safe_input,
            "output": safe_output,
        }
        if error is not None:
            data["error"] = str(error)
        decision: dict[str, Any] | None = None
        # Only attempt to extract a HITL decision when there is no error and
        # the original tool_output is available.
        if error is None and isinstance(tool_output, dict):
            decision = _extract_hitl_decision_from_output(tool_output)
        try:
            payload = build_event_payload(
                exec_ctx,
                logical_event_type="tool_result",
                phase="tool_result",
                tool_name=tool_name,
                extra_data=data,
            )
            insert_event(
                id=str(uuid.uuid4()),
                job_id=job_id,
                todo_id=str(todo_id) if todo_id else None,
                proc_inst_id=str(proc_inst_id) if proc_inst_id else None,
                event_type="tool_usage_finished",
                status=None,
                crew_type=crew_type,
                data=payload,
            )
            # 서브 에이전트 작업 요약 작성을 위해 job_id별 사용된 도구 이름을 수집한다.
            if job_tool_log is not None:
                try:
                    payload_body = payload.get("payload") or {}
                    name = payload_body.get("tool_name")
                    if isinstance(name, str) and name:
                        job_tool_log.setdefault(job_id, []).append(name)
                except Exception:
                    logger.exception("Failed to collect tool usage for job summary", exc_info=True)
        except Exception:
            logger.exception("Failed to insert tool_usage_finished event", exc_info=True)
        return decision

    for t in tools:
        # Avoid double-wrapping the same tool instance.
        if getattr(t, "_processgpt_instrumented", False):
            continue

        orig_invoke = getattr(t, "invoke", None)
        orig_ainvoke = getattr(t, "ainvoke", None)

        if orig_invoke is not None and callable(orig_invoke):

            def make_sync_wrapper(base_tool: Any, original):
                def wrapper(tool_input: Any, *args: Any, **kwargs: Any) -> Any:
                    job_id = _resolve_job_id()
                    _log_started(base_tool, job_id, tool_input)
                    try:
                        result = original(tool_input, *args, **kwargs)
                        decision = _log_finished(base_tool, job_id, tool_input, result, None)
                        if decision is not None:
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return result
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, tool_input, None, e)
                        raise

                return wrapper

            # Pydantic-based tools (e.g. StructuredTool) forbid normal setattr;
            # use object.__setattr__ to bypass the field check.
            try:
                object.__setattr__(t, "invoke", make_sync_wrapper(t, orig_invoke))
            except Exception:
                logger.exception("Failed to patch tool.invoke for instrumentation", exc_info=True)

        if orig_ainvoke is not None and callable(orig_ainvoke):

            def make_async_wrapper(base_tool: Any, original):
                async def wrapper(tool_input: Any, *args: Any, **kwargs: Any) -> Any:
                    job_id = _resolve_job_id()
                    _log_started(base_tool, job_id, tool_input)
                    try:
                        result = await original(tool_input, *args, **kwargs)
                        decision = _log_finished(base_tool, job_id, tool_input, result, None)
                        if decision is not None:
                            raise HitlInterrupt(
                                decision=decision,
                                tool_name=decision.get("tool_name") or getattr(
                                    base_tool, "name", base_tool.__class__.__name__
                                ),
                                job_id=job_id,
                            )
                        return result
                    except Exception as e:  # pragma: no cover - defensive logging
                        _log_finished(base_tool, job_id, tool_input, None, e)
                        raise

                return wrapper

            try:
                object.__setattr__(t, "ainvoke", make_async_wrapper(t, orig_ainvoke))
            except Exception:
                logger.exception("Failed to patch tool.ainvoke for instrumentation", exc_info=True)

        setattr(t, "_processgpt_instrumented", True)

    return tools


class DeepAgentsActionExecutor(AgentExecutor):
    """Runs todolist query via create_deep_agent(subagents); emits dict events."""

    async def execute(
        self,
        context: Any,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> str:
        logger.info("DeepAgents 멀티 에이전트 실행 시작")

        query = context.get_user_input()
        context_data = context.get_context_data() or {}
        row = context_data.get("row", {})
        extras = context_data.get("extras", {})

        proc_inst_id = row.get("root_proc_inst_id") or row.get("proc_inst_id") or ""
        task_id = str(row.get("id") or "")
        tenant_id = row.get("tenant_id") or ""
        form_id = extras.get("form_id")

        logger.info("task_id=%s proc_inst_id=%s form_id=%s", task_id, proc_inst_id, form_id)

        # Prepare run_state / human_response before creating execution context.
        run_state = (extras.get("run_state") or {}) if isinstance(extras.get("run_state"), dict) else {}
        human_response_answer = extras.get("human_response_answer")

        # Create or reuse execution context for this run.
        initial_agent_path = ["root_orchestrator"]
        if isinstance(run_state, dict) and isinstance(run_state.get("agent_path"), list):
            # Reuse stored agent_path when resuming.
            initial_agent_path = list(run_state.get("agent_path")) or initial_agent_path
        if exec_ctx is None:
            base_correlation = None
            if isinstance(run_state, dict) and isinstance(run_state.get("correlation_id"), str):
                base_correlation = run_state.get("correlation_id")
            job_uuid = base_correlation or str(uuid.uuid4())
            exec_ctx = ExecutionContext(
                correlation_id=job_uuid,
                agent_path=initial_agent_path,
                task_id=task_id or None,
                proc_inst_id=proc_inst_id or None,
            )
        job_uuid = exec_ctx.correlation_id

        # Parse work-item query into structured parts for downstream logic / HITL.
        parsed_query = parse_workitem_query(query or "")
        description = parsed_query.get("description") or ""
        instruction = parsed_query.get("instruction") or ""
        input_data = parsed_query.get("input_data") or {}

        # Resume: if we have run_state and human_response, merge answer into input_data.
        if isinstance(human_response_answer, str) and human_response_answer.strip():
            pending_field = run_state.get("pending_field") or "human_response"
            input_data = dict(input_data)
            input_data[pending_field] = human_response_answer.strip()
            parsed_query["input_data"] = input_data

            # Emit a resume event for tracing.
            try:
                resume_payload = build_event_payload(
                    exec_ctx,
                    logical_event_type="resume",
                    phase="resume",
                    extra_data={
                        "resume_source": "human_response",
                        "human_answer": human_response_answer.strip(),
                        "previous_interrupt_agent_path": run_state.get("agent_path"),
                        "previous_interrupt_tool_name": run_state.get("tool_name"),
                    },
                )
                insert_event(
                    id=str(uuid.uuid4()),
                    job_id=job_uuid,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    event_type="task_working",
                    status=None,
                    crew_type="action",
                    data=resume_payload,
                )
            except Exception:
                logger.exception("Failed to insert resume event", exc_info=True)

        # HITL safety net: pre-run heuristic; when graph-level HITL is used it takes precedence.
        hitl_needed, hitl_reason = _should_trigger_human_asked(description, instruction, input_data)
        if hitl_needed:
            try:
                hitl_job_id = str(uuid.uuid4())
                hitl_data = build_event_payload(
                    exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    requires_human=True,
                    extra_data={
                        "role": "user",
                        "text": hitl_reason
                        or "Additional information or approval is required.",
                        "type": "text",
                        "options": [],
                    },
                )
                insert_event(
                    id=str(uuid.uuid4()),
                    job_id=hitl_job_id,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    event_type="human_asked",
                    status="ASKED",
                    crew_type="action",
                    data=hitl_data,
                )
                return {
                    "status": "waiting_for_human",
                    "run_state": {
                        "reason": hitl_reason,
                        "pending_field": "input_data",
                        "correlation_id": exec_ctx.correlation_id,
                        "agent_path": list(exec_ctx.agent_path),
                        "tool_name": None,
                        "tool_call_context": None,
                    },
                }
            except Exception:
                logger.exception("Failed to insert human_asked event", exc_info=True)

        # Initialize processgpt_agent_utils context (for tools like mem0, memento, dmn_rule).
        set_context(
            task_id=str(task_id) if task_id else "",
            proc_inst_id=str(proc_inst_id) if proc_inst_id else "",
            crew_type="action",
            users_email=extras.get("notify_user_emails", []),
        )

        all_tools: list[Any] = []
        try:
            all_tools = await load_default_mcp_tools(tenant_id=tenant_id or None)
        except Exception as e:
            logger.warning(
                "MCP tools loading failed (subagent config continues with no MCP tools): %s",
                e,
                exc_info=False,
            )

        # job_id 컨텍스트: 기본값은 오케스트레이터(플래너) 작업의 job_id이며,
        # 서브 에이전트별 도구 호출은 각 서브 에이전트 전용 job_id로 분리된다.
        current_job_id: dict[str, str | None] = {"value": None}
        subagent_tool_log: dict[str, list[str]] = {}

        agent_rows = extras.get("agents", []) or []
        subagents = build_subagent_configs(
            agent_rows,
            all_tools=all_tools,
            query=query or "",
            tenant_id=tenant_id or None,
        )
        if not subagents:
            logger.warning(
                "No subagents configured (agents=%d). Orchestrator will run without delegation.",
                len(agent_rows),
            )

        subagent_contexts: list[dict[str, Any]] = []
        # 서브 에이전트별 job_id와 메타데이터를 준비하고,
        # 각 서브 에이전트의 MCP 도구를 해당 job_id로 인스트루먼트한다.
        for idx, row in enumerate(agent_rows):
            agent_name = (
                row.get("alias")
                or row.get("username")
                or row.get("name")
                or str(row.get("id"))
            )
            agent_role = row.get("role") or ""
            agent_goal = row.get("goal") or ""
            agent_profile = row.get("agent_profile") or "/images/chat-icon.png"
            agent_id = row.get("id")

            sub_job_id = str(uuid.uuid4())
            # 이 서브 에이전트의 도구 호출은 모두 sub_job_id로 로깅된다.
            if idx < len(subagents):
                sub_tools = subagents[idx].get("tools", [])
                _instrument_tools(
                    sub_tools,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    crew_type="action",
                    job_id_getter=lambda sj=sub_job_id: sj,
                    job_tool_log=subagent_tool_log,
                    exec_ctx=exec_ctx,
                )

            subagent_contexts.append(
                {
                    "job_id": sub_job_id,
                    "role": agent_role,
                    "name": agent_name,
                    "goal": agent_goal,
                    "agent_profile": agent_profile,
                    "agent_id": agent_id,
                }
            )

        name_to_context = {ctx["name"]: ctx for ctx in subagent_contexts}
        subagent_event_middleware = _create_subagent_event_middleware(
            event_queue,
            proc_inst_id,
            task_id,
            name_to_context,
            subagent_tool_log,
        )
        extra_middleware = [subagent_event_middleware] if subagent_event_middleware else []

        model_name = extras.get("model") or "openai:gpt-4o"

        # Build a sectioned, dynamic system prompt for the orchestrator,
        # combining static base rules with the current work-item context.
        base_rules = default_orchestrator_prompt()
        feedback_summary = extras.get("summarized_feedback") or ""
        form_fields = extras.get("form_fields")
        form_html = extras.get("form_html")
        form_info = None
        if form_fields or form_html:
            form_info = {"fields": form_fields, "html": form_html}

        system_prompt = build_orchestrator_system_prompt(
            base_rules=base_rules,
            description=description,
            instruction=instruction,
            input_data=input_data,
            agents=agent_rows,
            feedback=feedback_summary,
            form_info=form_info,
            tenant_id=tenant_id or None,
        )

        orchestrator = build_orchestrator(
            model_name=model_name,
            system_prompt=system_prompt,
            subagents=subagents,
            middleware=extra_middleware,
        )

        # 이 시점 이후에 발생하는 모든 도구 호출은 job_uuid와 동일한 job_id로 로깅된다.
        current_job_id["value"] = job_uuid
        # Task Execution Planner용 오케스트레이터 구성 이벤트
        planner_task_description = "\n\n".join(
            [part for part in [description, instruction] if part]
        ).strip()
        _enqueue_status(
            event_queue,
            "task_started",
        {
            # UI용 보기 좋은 메시지 (도메인 독립)
            "message": "루트 오케스트레이터가 작업 계획 수립을 시작했습니다.",
            "task_description": planner_task_description,
        },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="planning",
            agent_name="root_orchestrator",
        )

        try:
            result = await orchestrator.ainvoke(
                {"messages": [{"role": "user", "content": query or ""}]}
            )
        except HitlInterrupt as hi:
            # A tool signalled that human input is required. Record a human_asked
            # interrupt event and return waiting_for_human so the caller can pause.
            decision = hi.decision or {}
            question = decision.get("question") or "이 작업을 계속 진행하려면 사람이 추가 정보를 입력해야 합니다."
            choices = decision.get("choices") or []
            try:
                hitl_job_id = str(uuid.uuid4())
                hitl_data = build_event_payload(
                    exec_ctx,
                    logical_event_type="interrupt",
                    phase="interrupt",
                    requires_human=True,
                    tool_name=hi.tool_name,
                    extra_data={
                        "role": "user",
                        "text": question,
                        "type": "text",
                        "options": choices,
                        "reason": decision.get("reason"),
                        "tool_call_context": {
                            "job_id": hi.job_id,
                            "tool_name": hi.tool_name,
                        },
                    },
                )
                insert_event(
                    id=str(uuid.uuid4()),
                    job_id=hitl_job_id,
                    todo_id=task_id or None,
                    proc_inst_id=proc_inst_id or None,
                    event_type="human_asked",
                    status="ASKED",
                    crew_type="action",
                    data=hitl_data,
                )
            except Exception:
                logger.exception("Failed to insert human_asked event from tool HITL", exc_info=True)

            return {
                "status": "waiting_for_human",
                "run_state": {
                    "reason": decision.get("reason") or "Tool requested human input.",
                    "pending_field": "human_input",
                    "correlation_id": exec_ctx.correlation_id,
                    "agent_path": list(exec_ctx.agent_path),
                    "tool_name": hi.tool_name,
                    "tool_call_context": {
                        "job_id": hi.job_id,
                        "tool_name": hi.tool_name,
                    },
                },
            }
        final_messages = result.get("messages", [])
        raw_text = ""
        if final_messages:
            last = final_messages[-1]
            content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
            raw_text = _extract_plain_text(content)

        # [PLAN]...[/PLAN] 블록만 plans로 기록 (정형화된 계획 텍스트만; 에러/요약 제외)
        plans_text = _extract_plan_block(raw_text)
        if not plans_text and raw_text:
            plans_text = _extract_plain_text(raw_text).strip()
        _enqueue_status(
            event_queue,
            "task_completed",
            {
            "message": "작업 계획 수립이 완료되었습니다.",
            "plan": plans_text,
            },
            proc_inst_id,
            task_id,
            "action",
            job_id=job_uuid,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="planning",
            agent_name="root_orchestrator",
        )

        # 서브 에이전트 task_started/task_completed는 미들웨어에서 task() 호출 시 발행됨.
        # 미들웨어가 없거나, 미들웨어가 있어도 오케스트레이터가 task()를 한 번도 호출하지 않은 경우
        # 사후에 이벤트를 발행해 서브 에이전트 구성이 로그에 남도록 한다.
        any_subagent_invoked = any(
            subagent_tool_log.get(ctx["job_id"]) for ctx in subagent_contexts
        )
        if not subagent_event_middleware or not any_subagent_invoked:
            if subagent_event_middleware and not any_subagent_invoked:
                logger.warning(
                    "Orchestrator completed without delegating to any subagent (task() not called); "
                    "emitting subagent events as fallback."
                )
            for ctx in subagent_contexts:
                sub_job_id = ctx["job_id"]
                used_tools = subagent_tool_log.get(sub_job_id, [])
                작업_텍스트 = (
                    f"다음 도구 호출을 통해 작업을 수행했습니다: {', '.join(sorted(set(used_tools)))}"
                    if used_tools
                    else "도구 호출 이벤트 없이 작업을 수행했습니다."
                )
                _enqueue_status(
                    event_queue,
                    "task_started",
                    {
                        "message": f"서브 에이전트 '{ctx['name']}' 실행을 시작했습니다.",
                        "role": ctx["role"],
                        "goal": ctx["goal"],
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=sub_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_started",
                    phase="reasoning",
                    agent_name=ctx["name"],
                )
                _enqueue_status(
                    event_queue,
                    "task_completed",
                    {
                        "message": f"서브 에이전트 '{ctx['name']}' 실행이 완료되었습니다.",
                        "summary": 작업_텍스트,
                    },
                    proc_inst_id,
                    task_id,
                    "action",
                    job_id=sub_job_id,
                    exec_ctx=exec_ctx,
                    logical_event_type="agent_finished",
                    phase="reasoning",
                    agent_name=ctx["name"],
                )

        form_fields = extras.get("form_fields")
        pure_form_data, wrapped_result, original_wo_form, _, _ = convert_agent_output(
            raw_text, form_id, form_fields
        )
        if pure_form_data is None:
            pure_form_data = {}

        # 폼이 없거나 defaultform인 경우, 항상 JSON 파싱 가능한 고정 포맷으로 래핑
        has_form = bool(form_fields) and form_id not in (None, "", "defaultform")
        if not has_form:
            base_text = original_wo_form or wrapped_result.get("cleaned") or ""
            # 모델이 리스트/딕셔너리 구조를 문자열로 반환한 경우에도
            # 내부 text/content 필드만 추출하여 순수 텍스트로 만든다.
            final_text = _extract_plain_text(base_text).strip()
            result_status = "SUCCESS" if final_text else "Fail"
            final_result_payload = {
                "output": {
                    "status": result_status,
                    "result": final_text,
                },
            }
            final_event_data = final_result_payload
        else:
            final_event_data = pure_form_data

        end_job = str(uuid.uuid4())
        _enqueue_status(
            event_queue,
            "task_started",
            {
                "message": "최종 결과 생성을 시작했습니다.",
            },
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_started",
            phase="final",
            agent_name="final_result",
        )
        _enqueue_status(
            event_queue,
            "task_completed",
            {
                "message": "최종 결과 생성이 완료되었습니다.",
                "result": final_event_data,
            },
            proc_inst_id,
            task_id,
            "result",
            job_id=end_job,
            exec_ctx=exec_ctx,
            logical_event_type="agent_finished",
            phase="final",
            agent_name="final_result",
        )
        _enqueue_artifact(
            event_queue,
            name="deepagents_action_result",
            description="DeepAgents 멀티 에이전트 실행 결과",
            # wrapped_result는 dict/list 형태의 JSON-직렬화 가능한 객체여야 하며,
            # 그대로 events.data 및 todolist.draft(jsonb)에 저장된다.
            text=wrapped_result,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            exec_ctx=exec_ctx,
        )

        logger.info("DeepAgents 멀티 에이전트 실행 완료")
        return {"status": "completed"}

    async def cancel(
        self,
        context: Any,
        event_queue: EventQueue,
        exec_ctx: ExecutionContext | None = None,
    ) -> None:
        logger.info("작업 취소 요청됨")
