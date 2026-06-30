"""core/feedback/processor.py — LLM 기반 피드백→에이전트 매칭.

피드백을 분석해 각 에이전트별 학습 후보(learning_candidate)를 생성한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.llm.model import _build_model

logger = logging.getLogger(__name__)


async def match_feedback_to_agents(
    feedback: str,
    agents: list[dict[str, Any]],
    task_description: str = "",
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    llm = _build_model()

    agents_info = "\n".join(
        f"- 에이전트 ID: {a['id']}, 이름: {a.get('name','')}, 역할: {a.get('role','')}, 목표: {a.get('goal','')}"
        for a in agents
    )

    events_summary = "없음"
    if events:
        lines = []
        for ev in events[:30]:
            data_str = ""
            try:
                data_str = json.dumps(ev.get("data", {}), ensure_ascii=False)
                if len(data_str) > 300:
                    data_str = data_str[:300] + "...(truncated)"
            except Exception:
                data_str = str(ev.get("data", ""))[:300]
            lines.append(
                f"- time={ev.get('timestamp','')}, type={ev.get('event_type','')}, "
                f"status={ev.get('status','')}, crew_type={ev.get('crew_type','')}, data={data_str}"
            )
        events_summary = "\n".join(lines)

    prompt = f"""
다음 상황을 분석하여 각 에이전트에게 적절한 스킬 개선 피드백을 생성해주세요.

**작업 지시사항:**
{task_description}

**사용자 피드백 (시간순):**
{feedback}

**해당 작업의 이벤트 로그 (시간순, 실제 스킬/도구 사용 내역):**
{events_summary}

**에이전트 목록:**
{agents_info}

**상황 설명:**
에이전트들이 위의 작업지시사항에 따라 작업을 수행했지만, 사용자가 피드백을 제공했습니다.
이 피드백을 바탕으로 에이전트의 스킬(작업 절차)을 개선하기 위한 학습 후보를 생성하세요.

**피드백 처리 방식:**
- 가장 최신(time이 늦은) 피드백을 최우선으로 반영
- 이전 피드백들은 맥락 참고용
- 최신 피드백의 요구사항이 이전과 다르면 최신 것을 따름
- 자연스럽고 통합된 하나의 완전한 피드백으로 작성
- 최대 2500자까지 허용하여 상세히 작성

**스킬 개선 초점:**
- 피드백에서 절차(작업 순서, 단계별 프로세스)와 관련된 내용을 추출
- 기존 스킬의 수정이 필요한지, 새 스킬 생성이 필요한지 판단 힌트 제공
- 에이전트가 스킬을 개선할 수 있는 구체적이고 실행 가능한 가이드 제공

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- JSON 객체만 출력하세요

{{
  "agent_feedbacks": [
    {{
      "agent_id": "에이전트_ID",
      "agent_name": "에이전트_이름",
      "learning_candidate": {{
        "content": "시간순 피드백들을 통합한 자연스러운 스킬 개선 가이드",
        "intent_hint": "이 피드백이 어떤 스킬 개선을 요구하는지에 대한 요약 힌트"
      }}
    }}
  ]
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        cleaned = response.content.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        if parsed.get("agent_feedbacks"):
            for fb in parsed["agent_feedbacks"]:
                lc = fb.get("learning_candidate", {})
                logger.info(
                    "학습 후보: agent=%s hint=%s",
                    fb.get("agent_name", "?"),
                    lc.get("intent_hint", ""),
                )
        return parsed
    except json.JSONDecodeError:
        logger.warning("피드백 매칭 JSON 파싱 실패", exc_info=True)
        return {"agent_feedbacks": []}
    except Exception:
        logger.warning("피드백 매칭 실패", exc_info=True)
        return {"agent_feedbacks": []}
