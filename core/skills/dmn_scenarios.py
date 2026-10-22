"""core/skills/dmn_scenarios.py — 의사결정 회귀 시나리오를 스위트에 반영한다.

시나리오를 **만드는 일은 여기서 하지 않는다.** 규칙 표를 읽어 파생하는 것은 판정 규칙과
같은 지식이라 실행 엔진(completion `regression/dmn_derive.py`)이 맡고, 이 모듈은 받아 온
케이스를 `resource_eval_*` 스위트에 넣는 일만 한다. 사본을 여기 두면 표 해석이 갈라져
"화면에서는 승인인데 검증은 반려" 가 된다.

기준은 **변경 전(base) 버전**이다 — 변경 후를 보고 만들면 바뀐 결론을 기준선으로 굳혀
버려서 그 검증이 아무것도 잡아내지 못한다(그래서 base_ref 를 그대로 넘긴다).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .eval_tools import find_suite, load_suite_cases, sync_cases, upsert_suite

logger = logging.getLogger(__name__)

RESOURCE_TYPE_DMN = "dmn"

def _assign_positions(cases: list[dict], existing: list[dict]) -> None:
    """스위트 안에서 겹치지 않는 표시 순서를 매긴다(제자리 수정).

    규칙이 늘거나 줄면 케이스 이름이 바뀐다. 새 목록의 0,1,2… 를 그대로 쓰면 이미 있는
    케이스와 같은 자리를 차지해 정렬이 임의 순서가 된다(프로세스에서 실제로 겹쳤다).
    """
    taken = {c["eval_name"]: c.get("position") or 0 for c in existing}
    next_pos = (max(taken.values()) + 1) if taken else 0
    for case in cases:
        known = taken.get(case["eval_name"])
        if known is None:
            case["position"] = next_pos
            taken[case["eval_name"]] = next_pos
            next_pos += 1
        else:
            case["position"] = known


async def promote(
    *,
    tenant_id: str,
    resource_id: str,
    base_ref: str = "",
    replace: bool = False,
) -> dict:
    """변경 전 규칙 표에서 파생한 시나리오를 스위트에 반영한다."""
    from . import regression_client

    try:
        derived = await regression_client.derive_dmn_cases(
            tenant_id=tenant_id, resource_id=resource_id, base_ref=base_ref
        )
    except regression_client.RegressionUnavailable as e:
        return {"error": "engine_unavailable", "message": str(e)}
    if derived.get("error"):
        return derived

    cases = derived.get("cases") or []
    if not cases:
        return {
            "error": "no_cases",
            "message": (
                f"'{resource_id}' 의 의사결정 표에서 시나리오를 만들지 못했습니다. "
                "규칙 행이 없거나 DMN 을 읽을 수 없습니다."
            ),
        }

    existing_suite = find_suite(tenant_id, resource_id, RESOURCE_TYPE_DMN)
    existing = load_suite_cases(existing_suite) if existing_suite else []
    dropped = 0
    if replace and existing_suite:
        from .process_scenarios import _drop_cases_except

        dropped = await asyncio.to_thread(
            _drop_cases_except, existing_suite, {c["eval_name"] for c in cases}
        )
        existing = []

    _assign_positions(cases, existing)

    suite_id = await asyncio.to_thread(
        upsert_suite, tenant_id, resource_id, RESOURCE_TYPE_DMN, base_ref
    )
    ids = await asyncio.to_thread(sync_cases, tenant_id, suite_id, cases)
    logger.info(
        "dmn-scenarios: 확보 완료 resource=%s suite=%s 기준=%s 케이스 %d건(기존 %d건, 삭제 %d건)",
        resource_id, suite_id, base_ref or "(현재 표)", len(ids), len(existing), dropped,
    )
    return {
        "suite_id": suite_id,
        "case_count": len(ids),
        "dropped_count": dropped,
        # 어느 버전 기준으로 굳혔는지 — 화면이 그대로 되읽어 보여 준다.
        "baseline_ref": base_ref,
        "skipped": False,
    }


def summary(tenant_id: str, resource_id: str) -> dict[str, Any]:
    """이 의사결정에 회귀 시나리오가 있는지, 몇 건인지."""
    suite_id = find_suite(tenant_id, resource_id, RESOURCE_TYPE_DMN)
    if not suite_id:
        return {"suite_id": "", "case_count": 0, "cases": []}
    cases = load_suite_cases(suite_id)
    return {
        "suite_id": suite_id,
        "case_count": len(cases),
        "cases": [{"eval_name": c["eval_name"], "position": c["position"]} for c in cases],
    }
