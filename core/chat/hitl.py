"""HITL (Human-in-the-Loop) tool and interrupt payload utilities."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from langgraph.types import Interrupt, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator


def normalize_hitl_payload(data: Any) -> Dict[str, Any]:
    """Normalize request_human_input args or interrupt values for UI emission."""
    try:
        args = AskUserArgs.model_validate(data if isinstance(data, dict) else {})
    except Exception:
        args = AskUserArgs()
    payload: Dict[str, Any] = {"question": args.question, "context": args.context}
    if args.options:
        payload["options"] = args.options
        payload["multi_select"] = bool(args.multi_select)
    return payload


def _interrupt_value_payload(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Interrupt):
        return None
    payload = getattr(value, "value", None)
    if payload is None:
        return None
    if isinstance(payload, dict):
        return normalize_hitl_payload(payload)
    return {"raw": payload}


def _interrupts_payload(interrupts: Any) -> Optional[Dict[str, Any]]:
    if not interrupts:
        return None
    try:
        first = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
        return _interrupt_value_payload(first) or {"raw": interrupts}
    except Exception:
        return {"raw": interrupts}


def extract_interrupt_payload(result: Any) -> Optional[Dict[str, Any]]:
    """Extract interrupt payload from a LangGraph execution result dict."""
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    return _interrupts_payload(interrupts)


def find_interrupt_payload(value: Any) -> Optional[Dict[str, Any]]:
    """Find a LangGraph interrupt payload in nested stream data.

    With subgraphs enabled, LangGraph may surface interrupts under update
    payloads or namespaced subgraph values rather than the final root values.
    """
    direct = extract_interrupt_payload(value)
    if direct:
        return direct
    direct_obj = _interrupt_value_payload(value)
    if direct_obj:
        return direct_obj
    if isinstance(value, dict):
        for item in value.values():
            found = find_interrupt_payload(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = find_interrupt_payload(item)
            if found:
                return found
    return None


class AskUserArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(default="", description="사용자에게 물어볼 질문 또는 요청.")
    context: str = Field(default="", description="결정을 돕기 위한 배경 설명(선택). 구조화된 선택지는 context에 텍스트로 나열하지 말고 options 필드를 채워라.")
    options: List[Dict[str, str]] = Field(
        default_factory=list,
        description=(
            "질문에 정해진 선택지가 있을 때만 채운다(예: 진입 모드 3가지, 업종 4가지, "
            "구축할 프로세스 후보 등). 각 항목은 {\"label\": <선택지 이름>, "
            "\"description\": <1줄 설명, 선택>} 형태. 이 필드를 채우면 프런트엔드가 실제 "
            "라디오/체크박스 선택 UI를 그린다 — context 안에 번호나 불릿으로 선택지를 "
            "나열하는 것은 UI에 반영되지 않으니 하지 마라. 자유 응답만 필요한 질문(정성적 "
            "설명 요청 등)이면 비워둔다."
        ),
    )
    multi_select: bool = Field(
        default=False,
        description="options를 여러 개 동시에 고를 수 있게 할지 여부. 상호 배타적 선택(진입 모드 등)이면 false, 복수 공존 가능(BMC 채널·자원 등)이면 true.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coalesce_question_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if str(out.get("question") or "").strip():
            return out
        for key in ("text", "message", "instruction", "prompt", "query", "task", "content"):
            v = out.get(key)
            if isinstance(v, str) and v.strip():
                out["question"] = v.strip()
                return out
        return out

    @model_validator(mode="after")
    def _ensure_question(self) -> AskUserArgs:
        if not (self.question or "").strip():
            object.__setattr__(
                self,
                "question",
                "추가 확인이 필요합니다. 원하시는 작업을 구체적으로 알려주세요.",
            )
        return self


def _ask_user_impl(
    question: str,
    context: str = "",
    options: Optional[List[Dict[str, str]]] = None,
    multi_select: bool = False,
) -> str:
    payload: Dict[str, Any] = {"question": question, "context": context}
    if options:
        payload["options"] = options
        payload["multi_select"] = bool(multi_select)
    response = interrupt(payload)
    if isinstance(response, dict):
        return response.get("answer", str(response))
    return str(response)


ask_user = StructuredTool.from_function(
    func=_ask_user_impl,
    # 프론트엔드(HITL 패널 감지)·프롬프트·executor resume 이 모두 `request_human_input` 이름을 기대한다.
    # (incoming merge 가 `ask_user` 로 바꿔 이름이 어긋나면서 에이전트가 도구를 못 불러 텍스트로 폴백하던 문제 수정.)
    name="request_human_input",
    description=(
        "사람 개입(HITL)이 필요할 때 호출하는 도구. "
        "명시적 인간 승인, 민감 정보 확인, 모호한 요청 명확화, 선택지 제시 등 "
        "모든 사용자 입력이 필요한 상황에서 사용한다. "
        "정해진 선택지 중 고르게 하는 질문이면 options 파라미터를 채워라 — "
        "그냥 프로즈로 나열하면 화면에 선택 UI로 반영되지 않는다. "
        "단, **로딩된 스킬(SKILL.md)이 question 텍스트 형식을 명시했다면 그 형식을 그대로 따른다** "
        "(예: bpmn-process-generation-skill 의 `[스킬]`/`[에이전트]`/`[DMN]` 섹션 + `• 라벨: 설명`). "
        "그 형식도 프런트엔드가 선택 UI로 렌더하며, 스킬은 선택 결과를 그 형식으로 되받아 처리한다. "
        "내부에서 LangGraph interrupt()를 발생시켜 그래프 실행을 일시 중단한다. "
        "단, GitHub 토큰·GitHub 인증 정보는 시스템이 자동으로 관리하므로 "
        "이 도구로 요청하지 마세요."
    ),
    args_schema=AskUserArgs,
    return_direct=False,
)


def get_hitl_tools() -> list[Any]:
    return [ask_user]


# ---------------------------------------------------------------------------
# 워크아이템 HITL 규칙
# ---------------------------------------------------------------------------

def hitl_tools_enabled(*, agent_mode: str, is_chat: bool) -> bool:
    """사람에게 묻는 도구를 줄 것인가.

    채팅은 준다. 워크아이템은 COMPLETE(에이전트가 제출까지 맡음)일 때만 준다 —
    DRAFT 는 어차피 사람이 초안을 검토한다. 물으면 SDK 가 작업을 HUMAN_ASKED 로
    두고, 답(feedback)이 오면 다시 집혀 interrupt 를 그 답으로 재개한다.

    워크아이템에 이 도구가 없으면 모델은 질문을 평문 최종 답변으로 내고, COMPLETE
    에서는 그 질문이 산출물로 SUBMITTED 되어 프로세스가 다음 단계로 넘어간다.
    """
    mode = str(agent_mode or "").upper()
    if mode == "DRAFT":
        return False
    return bool(is_chat) or mode == "COMPLETE"


def workitem_resume_answer(
    *,
    is_chat: bool,
    has_pending_interrupt: bool,
    human_answer: str,
    latest_feedback: str,
    run_state: dict,
) -> tuple[str, dict]:
    """워크아이템이 사람의 답을 받아 다시 집혔을 때 재개에 쓸 (답, run_state).

    워크아이템 경로에는 프론트가 human_response_answer/run_state 를 실어 주지 않고,
    답은 feedback 의 마지막 항목으로 온다. 재개 대기 중인 interrupt 가 있으면 그
    원문을 답으로 삼는다 — 새 메시지로 넣으면 응답 없는 request_human_input 호출이
    남은 대화에 덧붙여진다. 해당하지 않으면 받은 값을 그대로 돌려준다.
    """
    if is_chat or not has_pending_interrupt or human_answer or not latest_feedback:
        return human_answer, run_state
    return latest_feedback, {
        **(run_state or {}),
        "pending_field": (run_state or {}).get("pending_field") or "human_response",
        "tool_name": "request_human_input",
    }


def records_human_answered(*, is_chat: bool) -> bool:
    """재개할 때 human_answered 상태 이벤트를 낼 것인가.

    채팅에서만 낸다. events.event_type enum 에 human_answered 가 없어, 워크아이템
    경로에서 내면 같은 묶음의 이벤트까지 저장에 실패해 사라진다. 답은 화면이
    human_response 로 이미 남긴다.
    """
    return bool(is_chat)


# ---------------------------------------------------------------------------
# HITL 상태 유틸리티
# ---------------------------------------------------------------------------

def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split()).lower()


def is_duplicate_hitl_request(
    previous_run_state: dict,
    *,
    question: str,
    pending_field: str,
) -> bool:
    """중복된 HITL 요청인지 확인한다 (같은 필드·같은 질문이 이미 pending 상태).

    ⚠️ 비교는 `question` 만으로 한다 — 프론트가 되돌려주는 `run_state.last_question` 에
    질문만 담기기 때문이다(본문까지 합쳐 비교하면 절대 일치하지 않아 억제가 무력화된다).
    그 대신 프로세스 생성 스킬의 컨설팅 승인처럼 **질문 문구가 고정된** 경우에는, 초안만
    바뀐 정상 재질문도 여기서 True 가 될 수 있다. 그래서 억제 분기는 턴을 조용히 끝내지
    않고 질문 본문을 어시스턴트 메시지로 남긴다(executor `_emit_human_asked`) — 사용자는
    바뀐 초안을 그대로 보고 다시 답할 수 있다.
    """
    if not isinstance(previous_run_state, dict):
        return False
    prev_field = str(previous_run_state.get("pending_field") or "").strip().lower()
    if not prev_field:
        return False
    if prev_field != str(pending_field or "").strip().lower():
        return False
    prev_question = _normalize_text(previous_run_state.get("last_question"))
    now_question = _normalize_text(question)
    return bool(prev_question and now_question and prev_question == now_question)
