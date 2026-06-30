"""core/feedback/agent.py — Deep Agent 기반 피드백 처리.

피드백을 분석하여 스킬(SKILL)을 생성/수정/삭제/적재하는 Deep Agent를 실행한다.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from deepagents import create_deep_agent

from core.feedback.tools import create_feedback_skill_tools
from core.llm.model import _build_model
from core.skills.skills import _get_skills

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """당신은 에이전트 피드백을 분석하여 에이전트에 적재된 스킬(SKILL)을 개선하는 전문가입니다.

**역할:**
피드백을 분석하고, 에이전트 적재 스킬 또는 액티비티 설정 스킬을 확인한 뒤, skill-creator 가이드라인에 따라 스킬을 개선합니다.

**⚠️ 핵심 원칙: 행동하기 전에 깊이 생각하세요**
성급한 행동은 기존 스킬을 손상시킵니다. 모든 결정에는 명확한 근거가 필요합니다.

**단순 재시도 요청 처리:**
피드백이 "다시 시도", "재시도", "try again" 등의 단순 재시도 요청이면, 스킬 변경 없이 처리를 종료하세요.

**사용 가능한 도구:**
1. `list_agent_skills` - 에이전트 적재 스킬 + 액티비티 설정 스킬 조회
2. `get_skill_detail` - 스킬의 상세 내용(SKILL.md + 부가 파일) 조회
3. `commit_to_skill` - 스킬 생성/수정/삭제 (content에 SKILL.md 전체 내용 전달)
4. `attach_skills_to_agent` - 기존 스킬을 에이전트에 적재 (에이전트가 있을 때만 사용)

**⚠️ 액티비티 스킬 처리 원칙:**
- 액티비티 스킬은 프로세스 정의에 설정된 스킬이므로 `attach_skills_to_agent`로 적재하지 않습니다.
- 액티비티 스킬은 `commit_to_skill`로 내용만 개선합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 필수 추론 프레임워크
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### [STEP 1] 피드백 의도 분석
- 단순 재시도 요청인가? → 즉시 종료
- 핵심 지식은 무엇인가? (절차, 작업 순서)
- 새로운 절차인가? 기존 절차의 수정인가?
- 적용 조건/범위가 명시되어 있는가?

### [STEP 2] 에이전트 적재 스킬 확인
`list_agent_skills`로 에이전트에 적재된 스킬을 확인한 후,
`get_skill_detail`로 개선 대상 스킬의 상세 내용을 조회합니다:
- 현재 스킬의 구조, 절차, 조건은 무엇인가?
- 피드백이 어떤 스킬에 해당하는가?
- 기존 스킬에서 반드시 보존해야 할 부분은?

**스킬 판단 기준:**
- 목표/결과는 스킬 절차가 아님 ("사업부 의사결정에 기여" 등은 스킬이 아님)
- 스킬은 구체적 작업 절차·산출물만 담음

### [STEP 3] 관계 분석 및 작업 결정

**관계 유형:**
| 유형 | 정의 | 처리 |
|------|------|------|
| EXTENDS | 새 조건/케이스 추가 | 기존 스킬에 내용 추가 (UPDATE) |
| REFINES | 세부사항 변경 | 해당 부분만 수정 (UPDATE) |
| CONFLICTS | 상충/모순 | 판단 필요 |
| SUPERSEDES | 명시적 대체 | 삭제 후 새로 생성 |
| UNRELATED | 적재 스킬과 무관 | 새로 생성 (CREATE) |

### [STEP 4] 자기 검증
작업 전 반드시 확인:
- 내 판단이 틀렸다면, 다른 가능한 해석은?
- 이 작업 후 기존 스킬이 손상되는 부분이 있는가?
- 최종 결과가 피드백의 의도와 기존 스킬 모두를 반영하는가?

### [STEP 5] SKILL.md 작성 및 커밋

**skill-creator 가이드라인에 따라 SKILL.md를 직접 작성**한 뒤 `commit_to_skill(content=...)`로 전달합니다.

#### SKILL.md 작성 규칙 (skill-creator 기준)

**구조:**
```
---
name: skill-name
description: 트리거 조건 + 스킬이 하는 일. 구체적이고 적극적으로 작성.
---

# 스킬 제목

(Markdown body — 지시사항, 절차, 예시 등)
```

**작성 원칙:**
- YAML frontmatter의 `name`과 `description`은 필수
- `description`은 트리거링 메커니즘이므로 구체적으로 작성 (언제 사용하는지, 무엇을 하는지)
- 본문은 imperative form(명령형)으로 작성
- 왜(why)를 설명하여 모델이 의도를 이해하도록 함 — ALWAYS/NEVER 남발 대신 이유를 전달
- SKILL.md는 500줄 이하로 유지, 긴 참조는 references/ 파일로 분리
- 피드백에서 일반화할 수 있는 패턴을 추출 — 특정 사례에 과적합하지 않도록 주의
- 기존 스킬 수정 시 기존 구조를 최대한 유지하면서 피드백 내용을 반영

**예시 패턴:**
```markdown
## 출력 형식
ALWAYS use this exact template:
# [제목]
## 요약
## 상세 내용
```

**UPDATE 시:**
1. `get_skill_detail`로 기존 SKILL.md 전체 내용을 읽음
2. 기존 내용을 기반으로 피드백 반영한 수정본을 작성
3. `commit_to_skill(operation="UPDATE", skill_id="...", content="수정된 전체 SKILL.md")`
   - Git 레포가 있는 스킬: 자동으로 피처 브랜치 + PR 생성 (스킬 주인이 리뷰 후 병합 결정)
   - Git 레포가 없는 스킬: workspace에 수정본 저장

**CREATE 시:**
1. skill-creator 가이드라인에 따라 새 SKILL.md를 작성
2. `commit_to_skill(operation="CREATE", skill_id="새-스킬-이름", content="전체 SKILL.md")`

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ⚠️ 주의사항
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- commit_to_skill의 content에는 반드시 완성된 전체 SKILL.md 내용을 전달하세요.
- UPDATE 시 반드시 get_skill_detail로 기존 내용을 먼저 확인하세요.
- UPDATE 시 skill_id는 반드시 기존 스킬 이름을 사용하세요.
- 스킬 변경이 필요하면 반드시 도구를 호출하세요. 말로만 결론을 내면 안 됩니다.
"""


def _format_feedback_input(
    agent_info: dict[str, Any],
    feedback_content: str,
    task_description: str = "",
    events: list[dict[str, Any]] | None = None,
) -> str:
    import json

    agent_info_text = (
        f"ID: {agent_info.get('id', '')}, "
        f"이름: {agent_info.get('name', '')}, "
        f"역할: {agent_info.get('role', '')}, "
        f"목표: {agent_info.get('goal', '')}"
    )

    events_summary = "없음"
    if events:
        lines = []
        for ev in events[:50]:
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

    return f"""다음 피드백을 분석하여 에이전트의 스킬을 개선해주세요:

**피드백 내용:**
{feedback_content}

**에이전트 정보:**
{agent_info_text}

**작업 지시사항:**
{task_description}

**이벤트 로그:**
{events_summary}

위 정보를 바탕으로 에이전트에 적재된 스킬을 확인하고, 필요한 스킬 개선을 수행하세요."""


async def process_feedback_with_deep_agent(
    agent_id: str,
    agent_info: dict[str, Any],
    feedback_content: str,
    task_description: str = "",
    events: list[dict[str, Any]] | None = None,
    activity_skills: list[str] | None = None,
    requester_id: str | None = None,
    requester_name: str | None = None,
    requester_email: str | None = None,
) -> dict[str, Any]:
    """Deep Agent를 사용하여 피드백을 처리하고 스킬을 개선한다."""
    try:
        logger.info("피드백 처리 시작: agent_id=%s", agent_id)

        tenant_id = agent_info.get("tenant_id", "")
        skill_tools = create_feedback_skill_tools(
            agent_id,
            feedback_content=feedback_content,
            activity_skills=activity_skills,
            tenant_id=tenant_id,
            requester_email=requester_email,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        llm = _build_model()
        # _get_skills는 글로벌 컬렉션(anthropics-skills)을 포함하므로
        # skill-creator 가이드라인이 SkillsMiddleware를 통해 자동 로드된다.
        skills_paths = _get_skills(tenant_id) or []

        agent = create_deep_agent(
            model=llm,
            tools=skill_tools,
            system_prompt=SYSTEM_PROMPT,
            skills=skills_paths if skills_paths else None,
            debug=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes", "on"),
        )

        input_text = _format_feedback_input(
            agent_info=agent_info,
            feedback_content=feedback_content,
            task_description=task_description,
            events=events,
        )

        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": input_text}]},
        )

        messages = result.get("messages", [])
        output = ""
        used_tools: list[str] = []

        for msg in messages:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    used_tools.append(tc.get("name", ""))
            if hasattr(msg, "type") and msg.type == "ai" and hasattr(msg, "content"):
                content = msg.content
                if isinstance(content, list):
                    content = "\n".join(
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if content:
                    output = content

        committed_tools = {"commit_to_skill", "attach_skills_to_agent"}
        did_commit = any(t in committed_tools for t in used_tools)

        output_lower = (output or "").lower()
        claims_mutation = any(
            kw in output_lower
            for kw in ["create", "update", "delete", "저장", "생성", "수정", "삭제", "커밋"]
        )
        claims_ignore = any(
            kw in output_lower
            for kw in ["ignore", "무시", "저장하지", "처리하지", "변경 불필요", "재시도"]
        )

        if not did_commit and claims_mutation and not claims_ignore:
            err = "Deep Agent가 변경 결론을 냈지만 도구를 호출하지 않아 실제 변경이 저장되지 않았습니다."
            logger.warning("say-do mismatch: agent_id=%s", agent_id)
            return {"output": output, "agent_id": agent_id, "error": err, "used_tools": used_tools}

        logger.info("피드백 처리 완료: agent_id=%s tools=%s", agent_id, used_tools)
        return {"output": output, "agent_id": agent_id, "used_tools": used_tools, "did_commit": did_commit}

    except Exception as e:
        logger.exception("피드백 처리 실패: agent_id=%s", agent_id)
        return {"output": "피드백 처리 중 에러 발생", "agent_id": agent_id, "error": str(e)}
