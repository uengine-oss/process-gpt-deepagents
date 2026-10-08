from __future__ import annotations

import json
import logging
import re
from typing import Any

from langgraph.types import Command

logger = logging.getLogger(__name__)

# [user][이름]: 또는 [assistant][이름][agentId=...]: 형식 파싱
_MSG_RE = re.compile(r'\[(user|assistant)\](?:\[[^\]]*\])*:\s*')

# 메시지 선두의 '/skill-name' 슬래시 커맨드
_SKILL_CMD_RE = re.compile(r'^/([A-Za-z0-9][\w\-]*)\s*')


def extract_skill_command(text: str) -> tuple[str, str]:
    """메시지 선두의 '/skill-name' 슬래시 커맨드를 파싱한다.

    Returns:
        (skill_name, remaining_text) — 슬래시 커맨드가 없으면 ("", 원본 text).
    """
    stripped = text.lstrip()
    m = _SKILL_CMD_RE.match(stripped)
    if not m:
        return "", text
    return m.group(1), stripped[m.end():].strip()


def _field_example_value(field: dict[str, Any]) -> Any:
    """폼 필드 정의로부터 출력 예시 값을 만든다. 다중 입력 모드 필드는 행(row) 객체의 배열로 표현한다."""
    if str(field.get("is_multidata_mode")).lower() == "true" and isinstance(field.get("fields"), list):
        row = {
            nf["key"]: f"<{nf.get('text', nf['key'])}>"
            for nf in field["fields"]
            if "key" in nf
        }
        return [row]
    return f"<{field.get('text', field.get('key'))}>"


def build_user_message(
    base_message: str,
    *,
    sources: Any = None,
    form_id: str | None = None,
    form_fields: Any = None,
    suggest_related_outputs: bool = False,
    agents: Any = None,
) -> str:
    """소스 첨부·폼 형식 안내를 포함한 최종 사용자 메시지를 조립한다."""
    message = base_message
    source_lines: list[str] = []
    prev_message = ""

    if isinstance(agents, list) and agents:
        agent_names = [
            str(a.get("username") or a.get("name")).strip().replace(" ", "-")
            for a in agents
            if isinstance(a, dict) and str(a.get("username") or a.get("name")).strip()
        ]
        if agent_names:
            prev_message = f"서브에이전트 {', '.join(agent_names)}에게 작업을 위임합니다.\n\n"

    if sources:
        for s in (sources if isinstance(sources, list) else []):
            file_name = s.get("file_name", "")
            file_id = s.get("file_id", "") or s.get("id", "")
            line = file_name
            if file_id:
                line += f"\n[file_id]: {file_id}"
            if line:
                source_lines.append(line)
        if source_lines:
            rag_sources = [s for s in (sources if isinstance(sources, list) else []) if s.get("file_id") or s.get("id")]
            rag_note = (
                "\n⚠️ 위 소스 파일은 RAG 인덱싱되어 있습니다. "
                "read_file 로 직접 읽지 말고 반드시 search_documents 도구를 사용해 내용을 조회하세요."
                if rag_sources else ""
            )
            message = f"[참고 소스]\n{chr(10).join(source_lines)}{rag_note}\n\n{message}"

    if suggest_related_outputs:
        message = (
            "ℹ️ 참고 소스 외에도, 이 작업에 같은 프로세스의 이전 워크아이템 산출물(output)이 입력으로 필요할 수 있습니다. "
            "필요하면 get_related_workitem_outputs 도구를 호출해 같은 프로세스의 이전 완료 워크아이템 output을 확인하세요.\n\n"
            f"{message}"
        )

    if form_id and form_fields:
        field_schema = json.dumps(form_fields, ensure_ascii=False)
        example_output = json.dumps(
            {form_id: {f["key"]: _field_example_value(f) for f in form_fields if "key" in f}},
            ensure_ascii=False,
            indent=2,
        )
        multidata_fields = [
            f for f in form_fields
            if str(f.get("is_multidata_mode")).lower() == "true" and isinstance(f.get("fields"), list)
        ]
        multidata_note = ""
        if multidata_fields:
            keys = ", ".join(f'"{f["key"]}"' for f in multidata_fields if "key" in f)
            multidata_note = (
                f"주의: {keys} 필드는 다중 입력(is_multidata_mode) 필드입니다. "
                "값은 반드시 객체의 배열이어야 하며, 각 배열 원소는 해당 필드의 하위 fields 정의를 따르는 "
                "하나의 행(row)을 나타냅니다. 처리 건수만큼 배열 원소를 추가하세요.\n"
            )
        message = (
            f"{prev_message}\n"
            f"{message}\n\n"
            f"[응답 형식 안내]\n"
            f"아래 양식 필드에 맞춰 반드시 JSON 형태로만 답변하세요. 다른 설명 없이 JSON만 출력하세요.\n"
            f"필드 정의:\n{field_schema}\n"
            f"출력 예시:\n{example_output}\n"
            f"{multidata_note}"
            f"주의: 출력 JSON의 최상위 키는 반드시 \"{form_id}\" 이어야 합니다."
            f" 이전 단계 입력 데이터([입력: ...])의 폼 ID를 출력 키로 사용하지 마세요.\n\n"
        )
    else:
        message = f"{prev_message}{message}"

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
    has_pending_interrupt: bool = False,
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
        has_pending_interrupt: 이 thread_id 의 체크포인트에 **실제로 재개 대기 중인
            interrupt** 가 있으면 True. 이것이 없는데 `Command(resume=...)` 를 보내면
            LangGraph 는 오류를 내지 않고 **그래프를 처음부터 다시 실행**해 같은 질문으로
            새 interrupt 를 만든다 → executor 가 중복 HITL 로 억제 → 사용자 화면에는
            아무 응답도 안 나오고 멈춘 것처럼 보인다(서버 재시작/InMemorySaver 유실,
            다중 파드 라우팅 등에서 발생). 그래서 재개는 반드시 이 값이 True 일 때만 한다.
    """
    use_graph_resume = bool(
        checkpointer is not None
        and checkpoint_thread_id
        and human_answer_stripped
        and str(run_state.get("tool_name") or "") == "request_human_input"
        and has_pending_interrupt
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


# ---------------------------------------------------------------------------
# 워크아이템 재개 — 크래시 재점유면 마지막 체크포인트부터 이어 간다
# ---------------------------------------------------------------------------

RESUME_FRESH = "fresh"
RESUME_RECLAIM = "reclaim"


def resume_kind_of(extras: Any) -> str:
    """SDK 가 실어 준 재개 사유(``extras["resume"]["kind"]``). 없으면 ``fresh``.

    SDK 의 새 심볼을 import 하지 않고 딕셔너리만 읽는다 — 재개 정보를 싣지 않는
    구버전 SDK 와 짝지어 배포돼도 이전 동작 그대로다.
    """
    resume = extras.get("resume") if isinstance(extras, dict) else None
    if isinstance(resume, dict):
        kind = str(resume.get("kind") or "").strip()
        if kind:
            return kind
    return RESUME_FRESH


def reclaim_continues_from_checkpoint(
    *,
    resume_kind: str,
    use_graph_resume: bool,
    has_pending_interrupt: bool,
    snapshot_next: Any,
) -> bool:
    """재점유된 실행을 새 입력 없이(``None``) 마지막 체크포인트부터 이어 갈 것인가.

    - 사람 답을 기다리는 interrupt 가 남아 있으면 그 재개(``Command(resume=)``)가 먼저다.
    - 그래프가 더 진행할 단계가 없으면(결과 저장 직전에 죽음) 이어 갈 것이 없다 —
      ``None`` 으로 돌리면 아무것도 하지 않고 끝나 빈 결과가 남는다. 이전처럼 새로 돈다.
    """
    return bool(
        resume_kind == RESUME_RECLAIM
        and not use_graph_resume
        and not has_pending_interrupt
        and snapshot_next
    )


async def choose_reclaim_inputs(
    graph: Any,
    config: dict[str, Any],
    inputs: Any,
    *,
    resume_kind: str,
    use_graph_resume: bool,
    has_pending_interrupt: bool,
) -> Any:
    """재점유면 그래프의 다음 단계를 보고 입력을 ``None`` 으로 바꾼다. 아니면 그대로.

    다음 단계(``next``)는 체크포인트만으로는 모르고 그래프가 계산하므로 그래프를
    만든 뒤에 판정한다. 같은 지시를 새 메시지로 덧붙이면 모델이 이미 받은 도구
    결과를 다시 조회한다(2026-10-06 E2: 3/3 재호출).
    """
    if resume_kind != RESUME_RECLAIM or use_graph_resume or has_pending_interrupt:
        return inputs
    thread_id = ((config or {}).get("configurable") or {}).get("thread_id")
    if not thread_id:
        return inputs
    try:
        snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:
        logger.warning("재점유 체크포인트 확인 실패 — 새 입력으로 실행 | thread_id=%s", thread_id, exc_info=True)
        return inputs
    snapshot_next = tuple(getattr(snap, "next", ()) or ())
    if reclaim_continues_from_checkpoint(
        resume_kind=resume_kind,
        use_graph_resume=use_graph_resume,
        has_pending_interrupt=has_pending_interrupt,
        snapshot_next=snapshot_next,
    ):
        logger.info("재점유 — 마지막 체크포인트부터 이어서 실행 | thread_id=%s next=%s", thread_id, list(snapshot_next))
        return None
    return inputs
