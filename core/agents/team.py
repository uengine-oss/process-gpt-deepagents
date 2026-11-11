"""업무를 딥 에이전트에 맡길 때, 꼭 필요한 전문 에이전트만 골라 서브에이전트로 붙인다(팀 구성).

언제 고르나
  - 할 일(워크아이템) 경로이고, 액티비티에 에이전트가 직접 배정되지 않았고(설계자가 정한 팀이 없음),
    서브프로세스 · 액티비티 지정 도구/스킬도 없을 때. 사람이 자기 업무를 「에이전트에 맡기기」로 넘긴 경우가 여기다.
  - DEEPAGENTS_AUTO_TEAM=0 이면 고르지 않는다.

무엇을 후보로 하나
  - 같은 테넌트의 에이전트 프로필 중 agent_type 이 'agent'(또는 비어 있음)인 것 — 스킬 · MCP 묶음이라
    기존 _build_subagents 가 그대로 서브에이전트로 만든다. a2a · pgagent 는 서브에이전트로 돌릴 수 없어 뺀다.

어떻게 고르나
  - LLM 한 번. 채팅방 라우터(agent-router /route)는 "침묵은 예외"라 거의 늘 누군가를 고르지만, 여기서는 반대로
    그 프로필의 스킬 · 도구 · 전문 지식이 있어야만 제대로 할 수 있을 때만 고른다. 필요 없으면 아무도 고르지 않는다.
  - 고른 뒤에도 실제로 맡길지는 루트 딥 에이전트가 실행 중에 정한다(task 도구). 위임을 강제하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_TEAM = 3
MAX_CANDIDATES = 40
TEAM_AGENT_TYPES = ("", "agent")
SELECT_TIMEOUT_SECONDS = 40


@dataclass
class TeamChoice:
    agents: list[dict] = field(default_factory=list)
    reason: str = ""
    candidates: int = 0


def auto_team_enabled() -> bool:
    return os.getenv("DEEPAGENTS_AUTO_TEAM", "1").strip().lower() not in ("0", "false", "off", "no")


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _name(agent: dict) -> str:
    return str(agent.get("username") or agent.get("alias") or "").strip()


def team_candidates(rows: list[dict] | None) -> list[dict]:
    """서브에이전트로 돌릴 수 있고, 무엇을 하는지 적혀 있는 에이전트 프로필만 남긴다."""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("is_agent") is not True or not r.get("id"):
            continue
        if str(r.get("agent_type") or "").strip().lower() not in TEAM_AGENT_TYPES:
            continue
        if not _name(r):
            continue
        if not any(str(r.get(k) or "").strip() for k in ("description", "role", "goal", "skills", "tools")):
            continue
        out.append(r)
    return out[:MAX_CANDIDATES]


def _clip(value: Any, n: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _candidate_line(a: dict) -> str:
    parts = [f"id={a['id']}", f"이름={_name(a)}"]
    for label, key, n in (("설명", "description", 300), ("역할", "role", 120), ("목표", "goal", 160)):
        if str(a.get(key) or "").strip():
            parts.append(f"{label}={_clip(a.get(key), n)}")
    if skills := _csv(a.get("skills")):
        parts.append("스킬=" + ", ".join(skills[:8]))
    if tools := _csv(a.get("tools")):
        parts.append("도구(MCP)=" + ", ".join(tools[:8]))
    return "- " + " | ".join(parts)


def _field_line(f: Any) -> str:
    if not isinstance(f, dict):
        return ""
    label = str(f.get("text") or f.get("label") or f.get("key") or "").strip()
    return label


def build_team_prompt(*, activity_name: str, instruction: str, form_fields: Any, candidates: list[dict]) -> str:
    fields = [x for x in (_field_line(f) for f in (form_fields or [])) if x]
    return (
        "딥 에이전트가 아래 업무를 처리한다. 딥 에이전트는 문서 읽기 · 작성 · 계산 · 판단을 혼자 할 수 있다.\n"
        "아래 전문 에이전트(스킬 · MCP 도구 묶음) 가운데, 이 업무를 제대로 하려면 **꼭 필요한 것만** 골라라.\n\n"
        "## 고르는 기준\n"
        "- 그 에이전트의 스킬 · 도구(MCP) · 전문 지식이 있어야만 이 업무를 제대로 할 수 있을 때만 고른다.\n"
        "- 혼자 해도 되는 업무(입력값을 정리 · 확인 · 작성하는 일)라면 아무도 고르지 않는다. 대부분은 여기에 해당한다.\n"
        "- 이름이나 분야가 비슷하다는 이유만으로 고르지 않는다. 확실하지 않으면 고르지 않는다.\n"
        f"- 많아야 {MAX_TEAM}개.\n\n"
        "## 업무\n"
        f"- 업무 이름: {_clip(activity_name, 120) or '(없음)'}\n"
        f"- 지시 · 입력: {_clip(instruction, 1500) or '(없음)'}\n"
        f"- 채울 폼 항목: {', '.join(fields[:30]) or '(없음)'}\n\n"
        "## 전문 에이전트 후보\n"
        + "\n".join(_candidate_line(a) for a in candidates)
        + "\n\n## 답 (JSON 만)\n"
        '{"agent_ids": ["후보 id", ...], "reason": "고른 이유 또는 고르지 않은 이유를 한국어 한두 문장으로"}\n'
        "agent_ids 에는 위 후보 목록의 id 만 넣는다. 아무도 필요 없으면 빈 배열이다.\n"
    )


def parse_team_choice(text: Any, candidates: list[dict], max_n: int = MAX_TEAM) -> tuple[list[dict], str]:
    """LLM 답에서 고른 후보와 이유를 꺼낸다. 후보에 없는 id · 중복은 버리고, 많아야 max_n 개."""
    raw = text if isinstance(text, str) else str(text or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return [], ""
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return [], ""
    if not isinstance(obj, dict):
        return [], ""
    by_id = {str(a["id"]): a for a in candidates}
    picked: list[dict] = []
    for aid in obj.get("agent_ids") or []:
        a = by_id.get(str(aid))
        if a is not None and a not in picked:
            picked.append(a)
        if len(picked) >= max_n:
            break
    reason = " ".join(str(obj.get("reason") or "").split())[:300]
    return picked, reason


def _content_text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


async def load_agent_directory(tenant_id: str) -> list[dict]:
    """테넌트의 에이전트 프로필(users.is_agent) — SDK 가 배정 에이전트를 읽을 때와 같은 행 모양."""
    from core.storage.db import get_supabase

    def _call():
        return (
            get_supabase()
            .table("users")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("is_agent", True)
            .execute()
            .data
            or []
        )

    return await asyncio.to_thread(_call)


async def choose_team(
    *,
    tenant_id: str,
    activity_name: str,
    instruction: str,
    form_fields: Any = None,
    model: Any = None,
) -> TeamChoice:
    """팀을 고른다. 고를 후보가 없거나 실패하면 빈 팀(딥 에이전트 혼자)."""
    if not (tenant_id and auto_team_enabled()):
        return TeamChoice()
    try:
        candidates = team_candidates(await load_agent_directory(tenant_id))
    except Exception:
        logger.warning("팀 구성: 에이전트 목록 조회 실패 — 혼자 진행 | tenant_id=%s", tenant_id, exc_info=True)
        return TeamChoice()
    if not candidates:
        return TeamChoice()
    prompt = build_team_prompt(
        activity_name=activity_name, instruction=instruction, form_fields=form_fields, candidates=candidates
    )
    try:
        if model is None:
            from core.llm.model import _build_model

            model = _build_model()
        resp = await asyncio.wait_for(model.ainvoke(prompt), timeout=SELECT_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("팀 구성: 선택 호출 실패 — 혼자 진행 | tenant_id=%s", tenant_id, exc_info=True)
        return TeamChoice(candidates=len(candidates))
    picked, reason = parse_team_choice(_content_text(resp), candidates)
    logger.info(
        "팀 구성 | tenant_id=%s activity=%s candidates=%d picked=%s reason=%s",
        tenant_id, activity_name, len(candidates), [_name(a) for a in picked], reason,
    )
    return TeamChoice(agents=picked, reason=reason, candidates=len(candidates))


def team_message_hint(choice: TeamChoice) -> str:
    """사용자 메시지 앞에 붙일 안내 — 배정 에이전트와 달리 위임을 강제하지 않는다."""
    if not choice.agents:
        return ""
    names = ", ".join(re.sub(r"[^\w-]", "-", _name(a)).lower().strip("-") for a in choice.agents)
    return (
        f"[팀 구성] 이 업무에 도움이 될 수 있는 전문 에이전트: {names}. "
        f"({choice.reason})\n"
        "그 에이전트의 스킬 · 도구가 필요한 부분만 task 도구로 맡기고, 나머지는 직접 처리하세요. "
        "결과 JSON 은 직접 작성합니다.\n\n"
    )


def team_card_text(choice: TeamChoice) -> str:
    """화면의 「팀 구성」 카드 결과."""
    if choice.agents:
        return "함께 일할 에이전트: " + ", ".join(_name(a) for a in choice.agents) + (
            f"\n{choice.reason}" if choice.reason else ""
        )
    return "혼자 처리합니다 — 이 업무에 꼭 필요한 전문 에이전트가 없습니다." + (
        f"\n{choice.reason}" if choice.reason else ""
    )
