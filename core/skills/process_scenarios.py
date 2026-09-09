"""core/skills/process_scenarios.py — 생성 시 검증이 통과시킨 케이스를 회귀 스위트로 승격한다.

## 왜 있는가

병합 전 검증(core/skills/pr_verification.py)은 "저장된 시나리오를 base(변경 전)/head(변경 후)
두 벌로 돌려 비교" 하는 방식이라, **비교에 쓸 시나리오가 미리 확보돼 있어야** 성립한다.
스킬은 생성 완료 게이트가 그 시나리오를 남긴다. 프로세스에는 그 자리가 없었는데, 실은
시나리오가 이미 만들어지고 있었다.

프로세스를 채팅으로 만들면 프론트가 draft 를 저장하고 completion 의 `/validate-and-improve`
를 부른다. 거기서 `ProcessValidator` 가

  1. 분기별 테스트 케이스를 모델로 만들고(`_build_test_plan`),
  2. 케이스마다 **새 인스턴스로 실제 실행**해 경로를 수집하고(`_run_trace`),
  3. 기대 경로와 대조해 결함을 낸다(`_diff`).

여기까지 이미 값을 다 치렀는데, 그 케이스는 리포트 문자열에만 남고 사라졌다. 이 모듈은
그중 **결함 없이 통과한 케이스**를 `resource_eval_*` 스위트로 옮긴다. 회귀 시나리오를 새로
만들지 않으므로 승격에는 모델 호출이 없다.

## 기준 경로를 모델의 기대 순서로 삼지 않는 이유

`expected_activity_order` 는 "액티비티 이름·설명이 의미하는 논리적으로 올바른 순서" 를
모델이 추론한 값이다. 생성 시 검증에서는 그게 맞다 — 정의의 시퀀스가 틀렸을 수 있으니
독립된 기준이 필요하다. 하지만 회귀 테스트가 답할 질문은 **"지금 되던 게 깨지는가"** 이므로
기준은 현재 실제로 되는 동작이어야 한다. 그래서 승격되는 기대값은 통과 회차의 실제 경로다
(completion `ProcessValidator._passing_cases` 가 이미 그렇게 추린다).

## 분기 판정을 함께 싣는 이유

경로만으로는 재생할 수 없다. 두 분기가 같은 액티비티로 향하면("고액 → 정밀검토",
"특수건 → 정밀검토") 경로에는 `정밀검토` 하나만 남아 어느 조건이 참이었는지 되찾을 수 없고,
나중에 한쪽 대상이 바뀌었을 때 깨진 변경을 "이상 없음" 으로 통과시킨다. 그래서 폴링
서비스가 `todolist.gateway_decisions` 에 남긴 판정을 케이스에 같이 싣는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .eval_tools import (
    find_suite,
    load_suite_cases,
    sync_cases,
    upsert_suite,
)

logger = logging.getLogger(__name__)

RESOURCE_TYPE_PROCESS = "bpmn"

# 회귀 실행기가 트레이스와 대조할 검사. 값 비교라 모델이 필요 없다.
CHECK_PATH_EQUALS = "path_equals"
CHECK_REACHED_END = "reached_end"


def _case_payload(case: dict) -> tuple[dict, dict]:
    """케이스 하나를 (입력, 기대값) 두 덩어리로 가른다."""
    inputs = {
        "activity_inputs": case.get("activity_inputs") or {},
        "gateway_decisions": case.get("gateway_decisions") or {},
    }
    expected = {
        "activity_order": [str(a) for a in (case.get("expected_activity_order") or [])],
        "reached_end": bool(case.get("reached_end")),
    }
    return inputs, expected


def _assertions(expected: dict) -> tuple[list[str], list[dict]]:
    """기대값을 (사람이 읽을 문구, 기계 검사) 쌍으로 만든다.

    프로세스 회귀의 단언은 전부 값 비교다. 의미 판단형 단언을 섞으면 채점에 모델이 끼어들어,
    "달라졌는지 보는" 일에 실행마다 흔들리는 판정이 들어온다.
    """
    order = expected["activity_order"]
    texts = [
        "실행 경로가 " + " → ".join(order) + " 다",
        "프로세스가 endEvent 까지 진행된다" if expected["reached_end"]
        else "프로세스가 endEvent 까지 진행되지 않는다",
    ]
    checks = [
        {"type": CHECK_PATH_EQUALS, "value": order},
        {"type": CHECK_REACHED_END, "value": expected["reached_end"]},
    ]
    return texts, checks


def to_eval_cases(passing_cases: list[dict]) -> list[dict]:
    """`/validate-and-improve` 의 passing_cases 를 스위트 케이스 모양으로 옮긴다.

    실행 경로가 없는 케이스는 버린다 — 비교할 기준이 없어 base/head 양쪽에서 판정 불가로
    빠지고, 회차 전체를 "판단 불가" 로 오염시킨다.
    """
    cases: list[dict] = []
    for position, raw in enumerate(passing_cases or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        inputs, expected = _case_payload(raw)
        if not name or not expected["activity_order"]:
            continue
        texts, checks = _assertions(expected)
        cases.append({
            "eval_name": name,
            "prompt": json.dumps(inputs, ensure_ascii=False),
            "expected_output": json.dumps(expected, ensure_ascii=False),
            # 첨부 문서를 참조하면 그 파일을 못 받아오는 순간 두 버전이 나란히 0점이 되어
            # 비교가 무너진다. 프로세스 시나리오는 입력을 본문에 다 담으므로 비워 둔다.
            "files": [],
            "assertions": texts,
            "checks": checks,
            "position": position,
        })
    return cases


def _assign_positions(cases: list[dict], existing: list[dict]) -> None:
    """스위트 안에서 겹치지 않는 표시 순서를 매긴다(제자리 수정).

    검증을 다시 돌리면 케이스 집합이 달라진다 — 분기가 늘거나 줄면 이름이 바뀌고, 그때마다
    새 목록의 0,1,2… 를 그대로 쓰면 **이미 있는 케이스와 같은 자리를 차지한다.** 실제로
    그렇게 겹쳤고, 그러면 `load_suite_cases` 의 position 정렬이 임의 순서가 되어 화면에
    보이는 시나리오 순서가 회차마다 흔들린다.

    이름이 이미 있으면 그 자리를 지키고, 새 이름만 뒤에 붙인다.
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


def _drop_cases_except(suite_id: str, keep_names: set[str]) -> int:
    """이번 집합에 없는 옛 케이스를 지운다.

    `resource_eval_results.case_id` 는 ON DELETE SET NULL 이라 지난 회차의 채점 기록은
    남는다 — 지워지는 것은 앞으로 무엇을 검증할지에 대한 정의뿐이다.
    """
    from ..storage.db import get_supabase

    sb = get_supabase()
    rows = (
        sb.table("resource_eval_cases")
        .select("id,eval_name")
        .eq("suite_id", suite_id)
        .execute()
    )
    stale = [r["id"] for r in (rows.data or []) if r["eval_name"] not in keep_names]
    for case_id in stale:
        sb.table("resource_eval_cases").delete().eq("id", case_id).execute()
    return len(stale)


def promote(
    *,
    tenant_id: str,
    resource_id: str,
    passing_cases: list[dict],
    replace: bool = False,
) -> dict:
    """통과한 케이스를 프로세스의 회귀 스위트에 반영한다.

    Args:
        replace: 이미 케이스가 있는 스위트를 갈아엎을지. 기본은 지키는 쪽이다 — 검증을 다시
            돌릴 때마다 기준이 새로 굳으면, 그 사이 들어온 변경이 기준에 섞여 회귀 테스트가
            "달라진 것" 을 못 보게 된다. 같은 이름의 케이스는 replace 없이도 갱신된다.

    Returns:
        {"suite_id", "case_count", "skipped"} 또는 {"error", "message"}.
    """
    cases = to_eval_cases(passing_cases)
    if not cases:
        return {
            "error": "no_cases",
            "message": (
                "통과한 검증 케이스가 없어 회귀 시나리오를 남기지 않았습니다. "
                "검증이 건너뛰어졌거나 모든 분기 케이스에 결함이 남은 경우입니다."
            ),
        }

    existing_suite = find_suite(tenant_id, resource_id, RESOURCE_TYPE_PROCESS)
    existing = load_suite_cases(existing_suite) if existing_suite else []
    dropped = 0

    if replace and existing_suite:
        # 프로세스 케이스는 이미 실엔진 실행으로 통과가 확인된 것들이라, 스킬 backfill 처럼
        # "새 시나리오가 확정될 때까지 기다렸다 교체" 할 필요가 없다. 이번 집합에 없는
        # 이름만 지운다.
        dropped = _drop_cases_except(existing_suite, {c["eval_name"] for c in cases})
        existing = []

    _assign_positions(cases, existing)

    suite_id = upsert_suite(tenant_id, resource_id, RESOURCE_TYPE_PROCESS)
    ids = sync_cases(tenant_id, suite_id, cases)
    logger.info(
        "process-scenarios: 승격 완료 resource=%s suite=%s 케이스 %d건(기존 %d건, 삭제 %d건)",
        resource_id, suite_id, len(ids), len(existing), dropped,
    )
    return {
        "suite_id": suite_id,
        "case_count": len(ids),
        "dropped_count": dropped,
        "skipped": False,
    }


def summary(tenant_id: str, resource_id: str) -> dict[str, Any]:
    """이 프로세스에 회귀 시나리오가 있는지, 몇 건인지."""
    suite_id = find_suite(tenant_id, resource_id, RESOURCE_TYPE_PROCESS)
    if not suite_id:
        return {"suite_id": "", "case_count": 0, "cases": []}
    cases = load_suite_cases(suite_id)
    return {
        "suite_id": suite_id,
        "case_count": len(cases),
        "cases": [{"eval_name": c["eval_name"], "position": c["position"]} for c in cases],
    }


# ---------------------------------------------------------------------------
# 시나리오가 없는 프로세스에 뒤늦게 확보하기
#
# 위 promote 는 생성 시 실엔진 검증이 통과시킨 케이스를 옮기는 일이라, 그 게이트를 거치지
# 않고 만들어진 프로세스 — 대부분의 기존 프로세스 — 에는 옮길 케이스가 없다. 그런 프로세스는
# 병합 요청을 열어도 "비교할 시나리오가 없습니다" 로 끝나 리뷰어가 diff 를 눈으로 보는
# 수밖에 없었다.
#
# 병합 전 검증은 실엔진을 쓰지 않고 저장된 갈림길 판정으로 정의를 재생할 뿐이므로
# (completion `regression/process_replay.py`), 시나리오에 필요한 것은 실행 기록이 아니라
# 정의뿐이다. 그래서 파생은 재생과 같은 자리(실행기)에 두고 여기서는 받아 담기만 한다 —
# 의사결정(dmn_scenarios)과 같은 구조다.
# ---------------------------------------------------------------------------

async def build(
    *,
    tenant_id: str,
    resource_id: str,
    base_ref: str = "",
    replace: bool = False,
) -> dict:
    """변경 전 정의에서 파생한 시나리오를 스위트에 반영한다.

    기준은 **변경 전(base) 버전**이다 — 변경 후를 보고 만들면 바뀐 흐름을 기준선으로 굳혀
    버려서 그 검증이 아무것도 잡아내지 못한다(그래서 base_ref 를 그대로 넘긴다).
    """
    from . import regression_client

    try:
        derived = await regression_client.derive_process_cases(
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
            "message": f"'{resource_id}' 의 프로세스 정의에서 시나리오를 만들지 못했습니다.",
        }

    existing_suite = find_suite(tenant_id, resource_id, RESOURCE_TYPE_PROCESS)
    existing = load_suite_cases(existing_suite) if existing_suite else []
    dropped = 0
    if replace and existing_suite:
        dropped = await asyncio.to_thread(
            _drop_cases_except, existing_suite, {c["eval_name"] for c in cases}
        )
        existing = []

    _assign_positions(cases, existing)

    suite_id = await asyncio.to_thread(upsert_suite, tenant_id, resource_id, RESOURCE_TYPE_PROCESS)
    ids = await asyncio.to_thread(sync_cases, tenant_id, suite_id, cases)
    logger.info(
        "process-scenarios: 확보 완료 resource=%s suite=%s 케이스 %d건(기존 %d건, 삭제 %d건)",
        resource_id, suite_id, len(ids), len(existing), dropped,
    )
    return {
        "suite_id": suite_id,
        "case_count": len(ids),
        "dropped_count": dropped,
        # 만들지 못한 갈림길을 숨기지 않는다 — 화면이 그대로 보여줄 수 있게 실어 보낸다.
        "skipped_reasons": derived.get("skipped_reasons") or [],
        "skipped": False,
    }
