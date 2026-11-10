"""스트림에서 사용자에게 보일 글은 메인 에이전트가 쓴 것뿐이다.

`graph.astream(..., subgraphs=True)` 는 `task` 도구로 부른 서브에이전트(예: skill-creator)의 토큰도
네임스페이스 `("tools:<id>", …)` 로 함께 흘려보낸다. 이를 그대로 사용자 답에 붙이면 서브에이전트가
메인 에이전트에게 돌려주는 영수증(「created: /workspace/.bpmn/…/SKILL.md 영업 제안서 작성 자동화」)이
최종 답 앞에 붙어 저장된다(VM 2026-11-10, 3장 프롬프트). 서브에이전트의 결과는 도구 결과로 메인
에이전트에게 가고, 사람에게 말하는 것은 메인 에이전트의 몫이다.
"""

from __future__ import annotations

from typing import Any


def is_subagent_namespace(namespace: Any) -> bool:
    """도구 안에서 돈 하위 그래프(서브에이전트)의 네임스페이스인가. 루트 그래프는 `()` 다."""
    if not namespace:
        return False
    parts = namespace if isinstance(namespace, (tuple, list)) else (namespace,)
    return any(str(p).startswith("tools:") for p in parts)
