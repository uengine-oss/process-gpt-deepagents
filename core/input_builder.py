from __future__ import annotations

import json
from typing import Any

from langgraph.types import Command


def build_user_message(
    base_message: str,
    *,
    sources: Any = None,
    form_id: str | None = None,
    form_fields: Any = None,
) -> str:
    """소스 첨부·폼 형식 안내를 포함한 최종 사용자 메시지를 조립한다."""
    message = base_message

    if sources:
        source_lines: list[str] = []
        for s in (sources if isinstance(sources, list) else []):
            file_name = s.get("file_name", "")
            file_id = s.get("file_id", "")
            file_path = s.get("file_path", "")
            line = file_name
            if file_id:
                line += f"\n[file_id]: {file_id}"
            if file_path:
                line += f"\n[file_path]: {file_path}"
            if line:
                source_lines.append(line)
        if source_lines:
            message = f"[참고 소스]\n{chr(10).join(source_lines)}\n\n{message}"

    if form_id and form_fields:
        field_schema = json.dumps(form_fields, ensure_ascii=False)
        example_output = json.dumps(
            {form_id: {f["key"]: f"<{f['text']}>" for f in form_fields if "key" in f}},
            ensure_ascii=False,
            indent=2,
        )
        message = (
            f"[응답 형식 안내]\n"
            f"아래 양식 필드에 맞춰 반드시 JSON 형태로만 답변하세요. 다른 설명 없이 JSON만 출력하세요.\n"
            f"필드 정의:\n{field_schema}\n"
            f"출력 예시:\n{example_output}\n\n"
            f"{message}"
        )

    return message


def resolve_chat_room_id(extras: dict[str, Any], row: dict[str, Any]) -> str:
    """요청 컨텍스트에서 채팅룸 식별자를 추출한다."""
    for key in ("conversation_id", "room_id", "chat_room_id"):
        v = extras.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    v2 = row.get("conversation_id")
    if isinstance(v2, str) and v2.strip():
        return v2.strip()
    return ""


def collect_recent_messages(extras: dict[str, Any]) -> list[dict[str, Any]]:
    """extras에서 채팅 히스토리를 정규화해 반환한다."""
    result: list[dict[str, Any]] = []
    for it in (extras.get("room_recent_messages") or []):
        if not isinstance(it, dict):
            continue
        role = str(it.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(it.get("content") or "").strip()
        if content:
            result.append({"role": role, "content": content})
    return result


def build_graph_inputs(
    user_message: str,
    *,
    human_answer_stripped: str,
    run_state: dict[str, Any],
    checkpointer: Any,
    checkpoint_thread_id: str,
    recent_messages: list[dict[str, Any]] | None = None,
) -> tuple[Any, bool]:
    """그래프 실행 입력과 use_graph_resume 플래그를 반환한다.

    Returns:
        (inputs, use_graph_resume)
        - inputs: graph.astream()에 전달할 값
        - use_graph_resume: True이면 LangGraph Command(resume=) 경로
    """
    use_graph_resume = bool(
        checkpointer is not None
        and checkpoint_thread_id
        and human_answer_stripped
        and str(run_state.get("tool_name") or "") == "request_human_input"
    )

    if use_graph_resume:
        return Command(resume={"answer": human_answer_stripped}), True

    if human_answer_stripped:
        pending_field = run_state.get("pending_field") or "human_response"
        augmented = (
            f"{user_message}\n\n"
            f"[HITL follow-up — field `{pending_field}`]:\n"
            f"{human_answer_stripped}\n\n"
            "Use this to continue the task immediately; do not ask for the same information again."
        )
        return {"messages": [{"role": "user", "content": augmented}]}, False

    history = list(recent_messages or [])
    if (
        history
        and history[-1].get("role") == "user"
        and history[-1].get("content", "").strip() == user_message.strip()
    ):
        state_msgs = history
    else:
        state_msgs = [*history, {"role": "user", "content": user_message}]

    return {"messages": state_msgs}, False
