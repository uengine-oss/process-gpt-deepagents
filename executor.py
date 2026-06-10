from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_message,
    new_text_status_update_event,
)
from a2a.types import Role, TaskState
from google.protobuf.json_format import ParseDict
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.types import Overwrite
from typing_extensions import override

from core.agent import build_agent
from core.checkpointer import get_async_postgres_checkpointer, resolve_checkpoint_thread_id
from core.hitl import extract_interrupt_payload, is_duplicate_hitl_request
from core.input_builder import (
    build_graph_inputs,
    build_user_message,
    collect_recent_messages,
    resolve_chat_room_id,
)
from core.activity import build_fallback_subagent, load_activity_capabilities
from core.chat_room import (
    a_update_chat_room_context,
    a_upsert_chat_room_start,
    build_initial_chat_room_context,
)
from core.mcp import ensure_work_assistant_mcp, load_chat_root_tools
from core.github_skill_fetcher import prefetch_github_skills
from core.output_utils import extract_final_text, extract_json_text
from core.token_hygiene import ToolResultTruncationMiddleware

logger = logging.getLogger(__name__)


class DeepAgentExecutor(AgentExecutor):

    @override
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> dict | None:
        try:
            await prefetch_github_skills()
        except Exception:
            logger.exception("github-skills: prefetch 실패, GitHub 스킬 없이 계속 진행")

        task_id = str(context.task_id)
        context_id = str(context.context_id)
        ctx_data = context.get_context_data() or {}
        extras = ctx_data.get("extras", {})
        row = ctx_data.get("row", {})

        tenant_id = str(row.get("tenant_id") or extras.get("tenant_id") or "").strip()
        agents = extras.get("agents", [])
        streamer = extras.get("streamer")
        form_id = extras.get("form_id")
        has_file = bool(extras.get("sources"))

        draft = row.get("draft") if isinstance(row.get("draft"), dict) else {}
        run_state: dict[str, Any] = {}
        if isinstance(extras.get("run_state"), dict):
            run_state = dict(extras["run_state"])
        elif isinstance(draft.get("run_state"), dict):
            run_state = dict(draft["run_state"])

        human_answer_stripped = str(extras.get("human_response_answer") or "").strip()

        tenant_mcp: dict[str, Any] = dict(extras.get("tenant_mcp") or {})
        if not tenant_mcp and tenant_id:
            try:
                from processgpt_agent_sdk.database import fetch_tenant_mcp
                fetched = await fetch_tenant_mcp(tenant_id)
                if isinstance(fetched, dict):
                    tenant_mcp = fetched
            except Exception:
                logger.warning("tenant_mcp DB 조회 실패 | tenant_id=%s", tenant_id, exc_info=True)

        is_chat = str(extras.get("request_kind") or "").strip().lower() == "chat"
        chat_room_id = resolve_chat_room_id(extras, row if isinstance(row, dict) else {})

        if is_chat:
            tenant_mcp = ensure_work_assistant_mcp(tenant_mcp, tenant_id)
        chat_root_tools = await load_chat_root_tools(tenant_mcp) if is_chat else []

        if is_chat and chat_room_id and tenant_id:
            try:
                await a_upsert_chat_room_start(
                    tenant_id=tenant_id,
                    room_id=chat_room_id,
                    participants=[],
                    room_name=None,
                    primary_agent_id=None,
                    context=build_initial_chat_room_context(has_file=has_file),
                )
            except Exception:
                logger.warning(
                    "chat_room start upsert 실패 | room_id=%s", chat_room_id, exc_info=True
                )

        user_message = build_user_message(
            context.get_user_input(),
            sources=extras.get("sources"),
            form_id=form_id,
            form_fields=extras.get("form_fields"),
        )
        recent_msgs = collect_recent_messages(extras) if is_chat else []

        await event_queue.enqueue_event(
            new_task(task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED)
        )

        activity_name = row.get("activity_name", "Unknown")
        job_id = str(uuid.uuid4())

        task_started_evt = new_text_status_update_event(
            task_id=task_id,
            context_id=context_id,
            state=TaskState.TASK_STATE_WORKING,
            text=json.dumps({"goal": f"{activity_name} 작업을 시작합니다."}, ensure_ascii=False),
        )
        ParseDict(
            {"event_type": "task_started", "job_id": job_id, "crew_type": "deepagents"},
            task_started_evt.metadata,
        )
        await event_queue.enqueue_event(task_started_evt)

        checkpointer = await get_async_postgres_checkpointer()
        checkpoint_thread_id = resolve_checkpoint_thread_id(is_chat=is_chat, row=row) or context_id

        inputs, use_graph_resume = build_graph_inputs(
            user_message,
            human_answer_stripped=human_answer_stripped,
            run_state=run_state,
            checkpointer=checkpointer,
            checkpoint_thread_id=checkpoint_thread_id,
            recent_messages=recent_msgs,
        )

        if use_graph_resume:
            logger.info("HITL 그래프 재개 | thread_id=%s task_id=%s", checkpoint_thread_id, task_id)
        elif human_answer_stripped:
            logger.info(
                "HITL 비-그래프 재개 | task_id=%s pending_field=%s",
                task_id,
                run_state.get("pending_field") or "human_response",
            )

        if human_answer_stripped:
            resume_run_state = {
                "pending_field": run_state.get("pending_field") or "human_response",
                "tool_name": run_state.get("tool_name") or "request_human_input",
                "last_question": run_state.get("last_question") or "",
                "checkpoint_thread_id": checkpoint_thread_id or "",
                "resume_source": "checkpoint_graph_resume" if use_graph_resume else "human_response",
            }
            human_answered_evt = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=json.dumps(
                    {"answer": human_answer_stripped, "type": "text", "run_state": resume_run_state},
                    ensure_ascii=False,
                ),
            )
            ParseDict(
                {"event_type": "human_answered", "job_id": job_id, "crew_type": "deepagents"},
                human_answered_evt.metadata,
            )
            await event_queue.enqueue_event(human_answered_evt)

        if not agents:
            proc_def_id = str(row.get("proc_def_id") or "").strip()
            activity_id = str(row.get("activity_id") or "").strip()
            act_tools, act_skills = load_activity_capabilities(
                tenant_id=tenant_id,
                proc_def_id=proc_def_id,
                activity_id=activity_id,
            )
            if act_tools or act_skills:
                fallback = build_fallback_subagent(
                    tenant_id=tenant_id,
                    activity_tools=act_tools,
                    activity_skills=act_skills,
                )
                if fallback:
                    agents = [fallback]
                    logger.info(
                        "서브에이전트 폴백 활성화 | proc_def_id=%s activity_id=%s tools=%s skills=%s",
                        proc_def_id, activity_id, act_tools, act_skills,
                    )
            elif not is_chat:
                logger.warning(
                    "서브에이전트 미설정 (activity fallback 없음) | proc_def_id=%s activity_id=%s",
                    proc_def_id, activity_id,
                )

        async def _update_chat_ctx(payload: dict) -> None:
            if not (is_chat and chat_room_id and tenant_id):
                return
            et = str(payload.get("type") or "")
            if et not in ("plan_tools", "plan_skills", "plan_todos", "tool_start", "tool_end"):
                return
            try:
                await a_update_chat_room_context(
                    tenant_id=tenant_id,
                    room_id=chat_room_id,
                    evt=payload,
                    has_file=has_file,
                )
            except Exception:
                logger.warning("chat_room context update 실패 | type=%s", et, exc_info=True)

        config = {"configurable": {"thread_id": checkpoint_thread_id}}
        graph = await build_agent(
            tenant_id=tenant_id,
            checkpointer=checkpointer,
            subagents_raw=agents,
            tenant_mcp=tenant_mcp,
            extra_tools=chat_root_tools,
            middleware=[ToolResultTruncationMiddleware()],
        )
        active_tool_calls: dict[str, str] = {}
        full_response = ""
        final_state: dict[str, Any] = {}

        try:
            async for stream_evt, data in graph.astream(
                inputs, config=config, stream_mode=["messages", "updates", "values"]
            ):
                if stream_evt == "messages":
                    msg, _meta = data
                    if isinstance(msg, AIMessageChunk):
                        text = msg.content if isinstance(msg.content, str) else "".join(
                            b.get("text", "") for b in msg.content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if text:
                            full_response += text
                            await event_queue.enqueue_event(
                                new_text_message(text=text, role=Role.ROLE_AGENT)
                            )
                            if streamer:
                                await streamer.send_json({"type": "token", "content": text})

                        for tc in getattr(msg, "tool_call_chunks", []):
                            tc_id = tc.get("id")
                            tc_name = tc.get("name")
                            if tc_id and tc_name and tc_id not in active_tool_calls:
                                active_tool_calls[tc_id] = tc_name
                                if streamer:
                                    _ts_payload = {"type": "tool_start", "tool": tc_name, "id": tc_id}
                                    await streamer.send_json(_ts_payload)
                                    asyncio.create_task(_update_chat_ctx(_ts_payload))

                elif stream_evt == "updates":
                    for _node, update in data.items():
                        if not isinstance(update, dict):
                            continue
                        messages_update = update.get("messages", [])
                        if isinstance(messages_update, Overwrite):
                            messages_update = messages_update.value or []
                        for msg in messages_update:
                            if getattr(msg, "tool_calls", None):
                                for tc in msg.tool_calls:
                                    tool_name = tc["name"]
                                    tool_args = tc.get("args", {})
                                    evt = new_text_status_update_event(
                                        task_id=task_id,
                                        context_id=context_id,
                                        state=TaskState.TASK_STATE_WORKING,
                                        text=json.dumps(
                                            {"tool": tool_name, "args": tool_args, "tool_name": tool_name},
                                            ensure_ascii=False,
                                        ),
                                    )
                                    ParseDict(
                                        {"event_type": "tool_usage_started", "job_id": job_id, "crew_type": "deepagents"},
                                        evt.metadata,
                                    )
                                    await event_queue.enqueue_event(evt)
                                    if streamer:
                                        _pt_payload = {
                                            "type": "plan_tools",
                                            "tools": [{"tool": tool_name, "name": tool_name, "args": tool_args}],
                                        }
                                        await streamer.send_json(_pt_payload)
                                        asyncio.create_task(_update_chat_ctx(_pt_payload))

                            elif isinstance(msg, ToolMessage):
                                tool_name = active_tool_calls.pop(
                                    msg.tool_call_id, getattr(msg, "name", "unknown")
                                )
                                result_content = msg.content
                                evt = new_text_status_update_event(
                                    task_id=task_id,
                                    context_id=context_id,
                                    state=TaskState.TASK_STATE_WORKING,
                                    text=json.dumps(
                                        {"tool": tool_name, "tool_name": tool_name, "result": result_content},
                                        ensure_ascii=False,
                                    ),
                                )
                                ParseDict(
                                    {"event_type": "tool_usage_finished", "job_id": job_id, "crew_type": "deepagents"},
                                    evt.metadata,
                                )
                                await event_queue.enqueue_event(evt)
                                if streamer:
                                    _te_payload = {"type": "tool_end", "tool": tool_name, "output": result_content}
                                    await streamer.send_json(_te_payload)
                                    asyncio.create_task(_update_chat_ctx(_te_payload))

                elif stream_evt == "values" and isinstance(data, dict):
                    final_state = data

            raw_text = extract_final_text(final_state, streamed_fallback=full_response)

            interrupt_payload = extract_interrupt_payload(final_state)
            if interrupt_payload:
                question = (
                    interrupt_payload.get("question") if isinstance(interrupt_payload, dict) else None
                ) or "이 작업을 계속 진행하려면 추가 입력이 필요합니다."
                context_text = (
                    interrupt_payload.get("context", "") if isinstance(interrupt_payload, dict) else ""
                )
                pending_field = "human_response"

                if is_duplicate_hitl_request(run_state, question=question, pending_field=pending_field):
                    logger.info("중복 HITL 요청 억제 | task_id=%s", task_id)
                    return {"status": "waiting_for_human", "run_state": dict(run_state)}

                new_run_state = {
                    "pending_field": pending_field,
                    "tool_name": "request_human_input",
                    "last_question": question,
                    "checkpoint_thread_id": checkpoint_thread_id or "",
                }
                hitl_evt = new_text_status_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    text=json.dumps(
                        {
                            "question": question,
                            "context": context_text,
                            "type": "text",
                            "run_state": new_run_state,
                        },
                        ensure_ascii=False,
                    ),
                )
                ParseDict(
                    {"event_type": "human_asked", "job_id": job_id, "crew_type": "deepagents"},
                    hitl_evt.metadata,
                )
                await event_queue.enqueue_event(hitl_evt)
                return {"status": "waiting_for_human", "run_state": new_run_state}

            result_completed_evt = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=json.dumps({"result": raw_text}, ensure_ascii=False),
            )
            ParseDict(
                {"event_type": "task_completed", "job_id": job_id, "crew_type": "deepagents"},
                result_completed_evt.metadata,
            )
            await event_queue.enqueue_event(result_completed_evt)

            result_job_id = str(uuid.uuid4())
            result_started_evt = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=json.dumps(
                    {
                        "goal": "최종 결과를 반환합니다.",
                        "name": "최종 결과 반환",
                        "role": "최종 결과 반환",
                        "agent_profile": "/images/chat-icon.png",
                    },
                    ensure_ascii=False,
                ),
            )
            ParseDict(
                {"event_type": "task_started", "job_id": result_job_id, "crew_type": "deepagents"},
                result_started_evt.metadata,
            )
            await event_queue.enqueue_event(result_started_evt)

            final_text = extract_json_text(raw_text) if form_id else raw_text
            result_done_evt = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=final_text,
            )
            ParseDict(
                {"event_type": "task_completed", "job_id": result_job_id, "crew_type": "deepagents"},
                result_done_evt.metadata,
            )
            await event_queue.enqueue_event(result_done_evt)

            artifact_evt = new_text_artifact_update_event(
                task_id=task_id,
                context_id=context_id,
                name="assistant_response",
                text=final_text,
                last_chunk=True,
            )
            ParseDict({"role": "assistant"}, artifact_evt.metadata)
            await event_queue.enqueue_event(artifact_evt)

            if is_chat and chat_room_id:
                logger.info(
                    "채팅 완료 | room_id=%s task_id=%s response_len=%d",
                    chat_room_id, task_id, len(final_text),
                )

        except Exception as e:
            logger.exception("execute 실패: %s", e)
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text=json.dumps({"error": str(e)}, ensure_ascii=False),
                )
            )

        return None

    @override
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = str(context.task_id)
        context_id = str(context.context_id)
        logger.info("cancel task_id=%s", task_id)
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="작업이 취소되었습니다.",
            )
        )
