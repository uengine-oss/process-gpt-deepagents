from __future__ import annotations

import json
import re
from typing import Any

from langgraph.types import Command

# [user][이름]: 또는 [assistant][이름][agentId=...]: 형식 파싱
_MSG_RE = re.compile(r'\[(user|assistant)\](?:\[[^\]]*\])*:\s*')


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


def _parse_history_string(s: str) -> list[dict[str, Any]]:
    """'[user][이름]: 내용' 형식의 문자열(또는 줄바꿈으로 연결된 여러 메시지)을 파싱한다."""
    result: list[dict[str, Any]] = []
    # 각 메시지 시작 위치를 찾아 분리
    for m in re.finditer(
        r'\[(user|assistant)\](?:\[[^\]]*\])*:\s*(.*?)(?=\n\[(?:user|assistant)\]|\Z)',
        s,
        re.DOTALL,
    ):
        role = m.group(1)
        content = m.group(2).strip()
        if content:
            result.append({"role": role, "content": content})
    return result


def collect_recent_messages(extras: dict[str, Any]) -> list[dict[str, Any]]:
    """extras에서 채팅 히스토리를 정규화해 반환한다.

    metadata.room_recent_history 우선 조회.
    값은 문자열, 문자열 리스트, 딕셔너리 리스트 모두 처리한다.
    """
    metadata = extras.get("metadata") or {}
    raw = metadata.get("room_recent_history") or extras.get("room_recent_history") or []

    # 단일 문자열: 전체 히스토리가 하나의 문자열로 온 경우
    if isinstance(raw, str):
        return _parse_history_string(raw)

    result: list[dict[str, Any]] = []
    for it in raw:
        if isinstance(it, str):
            # 문자열 리스트: 각 항목이 '[user][...]: 내용' 형식
            m = _MSG_RE.match(it)
            if m:
                role = re.search(r'\[(user|assistant)\]', it)
                if role:
                    content = it[m.end():].strip()
                    if content:
                        result.append({"role": role.group(1), "content": content})
        elif isinstance(it, dict):
            # 딕셔너리 리스트 형식도 하위 호환 지원
            role_str = str(it.get("role") or "").strip().lower()
            if role_str not in ("user", "assistant"):
                continue
            content = str(it.get("content") or "").strip()
            if content:
                result.append({"role": role_str, "content": content})
    return result


def build_graph_inputs(
    user_message: str,
    *,
    human_answer_stripped: str,
    run_state: dict[str, Any],
    checkpointer: Any,
    checkpoint_thread_id: str,
    recent_messages: list[dict[str, Any]] | None = None,
    has_checkpoint: bool = False,
) -> tuple[Any, bool]:
    """그래프 실행 입력과 use_graph_resume 플래그를 반환한다.

    Returns:
        (inputs, use_graph_resume)
        - inputs: graph.astream()에 전달할 값
        - use_graph_resume: True이면 LangGraph Command(resume=) 경로

    Args:
        has_checkpoint: 이 thread_id 에 이미 누적된 체크포인트 상태가 있으면 True.
            지속 체크포인터(공유 메모리/Postgres) 사용 시, 이미 누적된 대화에 또 history 를
            주입하면 메시지가 중복되므로, 이 경우 새 사용자 메시지만 넘긴다.
    """
    use_graph_resume = bool(
        checkpointer is not None
        and checkpoint_thread_id
        and human_answer_stripped
        and str(run_state.get("tool_name") or "") == "ask_user"
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

    # 이미 체크포인트에 대화가 누적돼 있으면 history 재주입 없이 새 메시지만 추가한다(중복 방지).
    if has_checkpoint:
        return {"messages": [{"role": "user", "content": user_message}]}, False

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
