"""core/agents/skill_creator_first_read.py — 스킬 생성 요청이면 첫 도구 호출을 skill-creator 읽기로 고정한다.

## 왜 필요한가

스킬 생성 절차(평가 계획·테스트·채점·완료 게이트)와 그 도구(`plan_skill_eval` 등)는 전역
`skill-creator` 스킬이 연다. 기본 프롬프트에 "먼저 skill-creator 스킬의 SKILL.md 를 읽는다" 를
적었지만, gpt-5 는 4회 모두 첫 호출로 `task` 를 불러 같은 이름의 skill-creator **서브에이전트**에
작성을 맡겼고, 스킬은 34~36번째 호출에야 읽었다. 그 전까지 평가를 임의 경로로 진행해 결과가
흔들리고 시간이 늘었다(bind-skill-tools verification 2026-10-16 gpt-5 절).

프롬프트 문장만으로는 순서가 지켜지지 않으므로, 스킬 생성 요청인 턴의 첫 모델 호출을 모델에
보내지 않고 `read_file(skill-creator SKILL.md)` 호출로 대신한다. 이 호출은 실제 도구 노드에서
실행되어 실행 기록에 첫 도구 호출로 남고, SkillsMiddleware 는 이것을 스킬 읽기로 보고 그 스킬이
여는 도구를 다음 요청부터 보인다.

다음 경우에는 아무것도 하지 않는다.
- 이번 턴의 사용자 메시지가 스킬 생성 요청이 아니다.
- 이번 턴에 이미 모델 응답이 있다(첫 모델 호출만 대신한다).
- 이 대화에서 skill-creator 를 이미 읽었거나 고정했다.
- 이 에이전트의 스킬 목록에 skill-creator 가 없다.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

SKILL_CREATOR_NAME = "skill-creator"

# 이전 측정에서 모델이 이 SKILL.md 를 처음 읽을 때 쓴 값(limit 1000 = 전체)과 같다.
_READ_LIMIT = 1000

# "스킬"과 만들다 계열 동사 사이에는 조사·수식어만 허용한다. 그래야 "nda 스킬로 검토해서
# 메일 작성해줘"(스킬 사용 + 다른 것 작성)를 스킬 생성으로 보지 않는다. 과거형("생성된 스킬",
# "만들었던")은 이미 있는 것을 가리키므로 뺀다.
_KO_CREATE = re.compile(
    r"(?:스킬|skill)"
    r"(?:\s|을|를|로|으로|도|좀|하나|한\s*개|새로|새롭게)*"
    r"(?:만들(?!었)|생성(?![된한했됐])|작성(?![된한했됐])|제작(?![된한했됐])|개발(?![된한했됐]))",
    re.IGNORECASE,
)
_KO_SKILLIFY = re.compile(r"스킬\s*화\s*(?:해|하자|하고|할)")
_KO_NEW_SKILL = re.compile(r"(?:새|신규)\s*스킬\s*(?:을|를)?\s*(?:만들|생성|작성)")
_EN_CREATE = re.compile(
    r"\b(?:create|make|build|write|author|generate)\b(?:\s+(?:a|an|the|new|me|us|one|another|reusable|custom))*"
    r"(?:\s+[\w-]+){0,4}?\s+skills?\b",
    re.IGNORECASE,
)


def is_skill_creation_request(text: str) -> bool:
    """사용자 문장이 스킬을 새로 만들어 달라는 요청인지 본다."""
    if not text:
        return False
    return bool(
        _KO_CREATE.search(text) or _KO_SKILLIFY.search(text) or _KO_NEW_SKILL.search(text) or _EN_CREATE.search(text)
    )


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_user_turn(msg: Any) -> bool:
    """사용자가 보낸 메시지인가(고정 스킬 본문·게이트 안내처럼 시스템이 끼운 사람 메시지는 아님)."""
    if not isinstance(msg, HumanMessage):
        return False
    if (msg.additional_kwargs or {}).get("lc_source"):
        return False
    return not _text(msg.content).startswith("[skill-eval-gate]")


def _skill_creator_path(state: Any) -> str:
    for skill in (state or {}).get("skills_metadata") or []:
        if skill.get("name") == SKILL_CREATOR_NAME and skill.get("path"):
            return str(skill["path"])
    return ""


def _already_read(messages: list[Any], path: str) -> bool:
    """이 대화에서 skill-creator 를 오류 없이 읽었거나 고정했는가."""
    read_ids = {
        c.get("id")
        for m in messages
        if isinstance(m, AIMessage)
        for c in (m.tool_calls or [])
        if c.get("name") == "read_file" and (c.get("args") or {}).get("file_path") == path
    }
    for m in messages:
        if isinstance(m, ToolMessage) and m.tool_call_id in read_ids and m.status != "error":
            return True
        if isinstance(m, HumanMessage):
            kw = m.additional_kwargs or {}
            if kw.get("lc_source") == "pinned_skill" and (kw.get("skill") or {}).get("name") == SKILL_CREATOR_NAME:
                return True
    return False


def forced_first_read(messages: list[Any], state: Any) -> AIMessage | None:
    """이번 모델 호출을 skill-creator 읽기로 대신해야 하면 그 응답을, 아니면 None 을 돌려준다."""
    last_user = next((i for i in range(len(messages) - 1, -1, -1) if _is_user_turn(messages[i])), None)
    if last_user is None:
        return None
    if any(isinstance(m, AIMessage) for m in messages[last_user + 1:]):
        return None
    if not is_skill_creation_request(_text(messages[last_user].content)):
        return None
    path = _skill_creator_path(state)
    if not path or _already_read(messages, path):
        return None
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "read_file",
            "args": {"file_path": path, "limit": _READ_LIMIT},
            "id": f"call_skill_creator_{uuid.uuid4().hex[:12]}",
        }],
    )


class SkillCreatorFirstReadMiddleware(AgentMiddleware):
    """스킬 생성 요청인 턴의 첫 모델 호출을 skill-creator SKILL.md 읽기로 대신한다."""

    def _forced(self, request: Any) -> ModelResponse | None:
        msg = forced_first_read(list(request.messages), request.state)
        if msg is None:
            return None
        logger.info("[skill-creator-first] 스킬 생성 요청 — 첫 도구 호출로 %s 를 읽는다", msg.tool_calls[0]["args"]["file_path"])
        return ModelResponse(result=[msg])

    def wrap_model_call(self, request, handler):
        return self._forced(request) or handler(request)

    async def awrap_model_call(self, request, handler):
        return self._forced(request) or await handler(request)
