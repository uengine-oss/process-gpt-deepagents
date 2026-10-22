"""프로세스 병합 전 검증 회차가 끝까지 도는지 고정한다.

판정은 실행 엔진(completion `/regression-run`)이 하고, 이 서비스는 회차 관리만 한다 —
기준선 재사용 판단, base/head 비교, 결과 기록. 여기서는 그 관리가 맞는지 본다.

여기서 지키는 핵심은 **비용이 새지 않는 것**이다. 회차가 도는 동안

- 모델을 한 번도 부르지 않는다 (`_build_test_plan`·채점 서브에이전트·조건 판정 전부 없음),
- 실행 엔진 인스턴스를 하나도 만들지 않는다 (`/initiate` 호출 없음),
- 기준선은 같은 버전이면 다시 계산하지 않는다.

이 셋 중 하나라도 깨지면 회귀 한 번이 생성 한 번만큼 든다. 스킬 회귀에서 같은 누수를
하나씩 찾아 막았던 기록이 `tests/test_eval_token_savings.py` 에 있다.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from core.skills import pr_verification as prv


def _definition(deep_target="deep_review"):
    return {
        "processDefinitionId": "mini",
        "activities": [{"id": a, "type": "userTask"} for a in
                       ("apply", "deep_review", "simple_review", "register")],
        "gateways": [{"id": "gw_amount", "type": "exclusiveGateway"}],
        "events": [{"id": "start_event", "type": "startEvent"},
                   {"id": "end_event", "type": "endEvent"}],
        "sequences": [
            {"id": "s_start_apply", "source": "start_event", "target": "apply"},
            {"id": "s_apply_gw", "source": "apply", "target": "gw_amount"},
            {"id": "s_gw_deep", "source": "gw_amount", "target": deep_target},
            {"id": "s_gw_simple", "source": "gw_amount", "target": "simple_review"},
            {"id": "s_deep_register", "source": "deep_review", "target": "register"},
            {"id": "s_simple_register", "source": "simple_review", "target": "register"},
            {"id": "s_register_end", "source": "register", "target": "end_event"},
        ],
    }


def _cases():
    return [{
        "id": "case-1",
        "eval_name": "고액 승인 경로",
        "prompt": json.dumps({
            "activity_inputs": {"apply": {"amount": 700000}},
            "gateway_decisions": {"gw_amount": {"selected": ["s_gw_deep"]}},
        }, ensure_ascii=False),
        "expected_output": json.dumps({"activity_order": ["apply", "deep_review", "register"]}),
        "files": [],
        "assertions": ["실행 경로가 apply → deep_review → register 다",
                       "프로세스가 endEvent 까지 진행된다"],
        "checks": [{"type": "path_equals", "value": ["apply", "deep_review", "register"]},
                   {"type": "reached_end", "value": True}],
        "position": 0,
    }]


def _replay(definition, case):
    """테스트용 최소 재생기 — 실행 엔진이 하는 판정을 흉내 낸다."""
    inputs = json.loads(case["prompt"])
    selected = (inputs["gateway_decisions"].get("gw_amount") or {}).get("selected") or []
    if not selected:
        return None
    target = next(s["target"] for s in definition["sequences"] if s["id"] == selected[0])
    order = ["apply", target, "register"]
    expected = case["checks"][0]["value"]
    passed = 1 + int(order == expected)
    return {
        "passed": passed, "total": 2, "pass_rate": passed / 2,
        "assertions": [
            {"text": case["assertions"][0], "passed": order == expected,
             "evidence": f"기대 {' → '.join(expected)} / 실제 {' → '.join(order)}"},
            {"text": case["assertions"][1], "passed": True, "evidence": "endEvent 도달=True"},
        ],
    }


@pytest.fixture
def wired(monkeypatch):
    """회차 하나를 도는 데 필요한 바깥 세계를 전부 대신한다."""
    state = {
        "definitions": {"v1.0": _definition(), "v1.1": _definition()},
        "inserted": [],
        "finished": {},
        "failed": None,
        "reused": {},
        "engine_calls": [],
    }

    async def _engine_run(*, tenant_id, resource_type, resource_id, base_ref, head_ref,
                          cases, skip_base=None):
        state["engine_calls"].append({"resource_type": resource_type, "cases": len(cases)})
        if head_ref not in state["definitions"] or base_ref not in state["definitions"]:
            missing = "head" if head_ref not in state["definitions"] else "base"
            return {"error": "version_unavailable", "variant": missing,
                    "message": f"{missing}({head_ref}) 를 가져오지 못했습니다: 없음"}
        results, undecided = [], []
        for case in cases:
            for variant, ref in (("base", base_ref), ("head", head_ref)):
                graded = _replay(state["definitions"][ref], case)
                if graded is None:
                    undecided.append({"eval_name": case["eval_name"], "variant": variant,
                                      "reason": "분기 판정 기록이 없습니다."})
                    continue
                results.append({"eval_name": case["eval_name"], "variant": variant, **graded})
        return {"results": results, "undecided": undecided,
                "base_digest": f"digest-{base_ref}", "head_digest": f"digest-{head_ref}",
                "mode": "replay", "llm_calls": 0, "engine_instances": 0}

    from core.skills import regression_client
    monkeypatch.setattr(regression_client, "run", _engine_run)
    monkeypatch.setattr(prv, "reusable_baseline_results", lambda **kw: state["reused"])
    monkeypatch.setattr(prv, "insert_results",
                        lambda **kw: state["inserted"].extend(kw["results"]))
    monkeypatch.setattr(prv, "_finish", lambda run_id, **f: state["finished"].update(f))
    monkeypatch.setattr(prv, "fail_run",
                        lambda run_id, msg: state.update(failed=msg))
    monkeypatch.setattr(prv, "_sb", lambda: MagicMock())
    return state


def _run(head_ref="v1.1"):
    return {"id": "run-1", "resource_id": "mini", "resource_type": "bpmn",
            "base_ref": "v1.0", "head_ref": head_ref}


def test_run_delegates_to_the_engine_and_calls_no_model(wired, monkeypatch):
    """회차가 도는 동안 이 서비스는 모델도 샌드박스도 부르지 않는다."""
    called = []
    # 모델·샌드박스로 가는 유일한 통로를 막아 둔다. 하나라도 불리면 테스트가 깨진다.
    monkeypatch.setattr(prv, "_sandbox",
                        lambda t: called.append("sandbox") or MagicMock())
    monkeypatch.setattr(prv, "_build_runner_agent",
                        lambda *a, **k: called.append("agent") or MagicMock())

    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(), cases=_cases(), suite_id="s1"))

    assert wired["failed"] is None
    assert called == []
    assert wired["engine_calls"] == [{"resource_type": "bpmn", "cases": 1}]
    summary = wired["finished"]["summary"]
    assert summary["mode"] == "replay"
    assert summary["llm_calls"] == 0
    assert summary["engine_instances"] == 0


def test_identical_versions_report_nothing_broken(wired):
    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(), cases=_cases(), suite_id="s1"))

    assert wired["finished"]["broken_count"] == 0
    assert wired["finished"]["pass_rate"] == 1.0
    assert wired["finished"]["base_pass_rate"] == 1.0


def test_changed_branch_target_shows_up_as_broken(wired):
    """분기 대상이 바뀌어 경로가 달라지면 '깨진 단계' 로 잡힌다."""
    wired["definitions"]["v1.1"] = _definition(deep_target="simple_review")  # noqa: E501

    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(), cases=_cases(), suite_id="s1"))

    assert wired["finished"]["broken_count"] >= 1
    broken = wired["finished"]["summary"]["broken"]
    assert broken[0]["eval_name"] == "고액 승인 경로"
    assert "simple_review" in broken[0]["evidence"]


def test_baseline_is_not_recomputed_when_reusable(wired):
    """같은 버전의 기준선을 다시 계산하지 않는다.

    재생은 순수 함수라 결과가 같지만, 다시 도는 것 자체가 낭비이고 스킬 회귀에서 같은
    자리가 실제 누수였다.
    """
    wired["reused"] = {"고액 승인 경로": {
        "eval_name": "고액 승인 경로", "variant": "base",
        "passed": 2, "total": 2, "pass_rate": 1.0,
        "assertions": [{"text": "실행 경로가 apply → deep_review → register 다", "passed": True},
                       {"text": "프로세스가 endEvent 까지 진행된다", "passed": True}],
        "outputs_dir": None,
    }}

    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(), cases=_cases(), suite_id="s1"))

    variants = [r["variant"] for r in wired["inserted"]]
    assert variants.count("base") == 1      # 재사용분 하나뿐 — 다시 계산하지 않았다
    assert variants.count("head") == 1


def test_undecidable_case_is_not_silently_passed(wired):
    """판정하지 못한 시나리오는 결과에 남는다.

    조용히 두면 broken_count=0 이라 검증을 통과한 것처럼 보인다.
    """
    cases = _cases()
    cases[0]["prompt"] = json.dumps({"activity_inputs": {}, "gateway_decisions": {}})

    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(), cases=cases, suite_id="s1"))

    assert wired["failed"] is not None or wired["finished"].get("summary", {}).get("undecided")


def test_missing_version_fails_the_run_with_a_reason(wired):
    asyncio.run(prv.execute_run(tenant_id="tn", run=_run(head_ref="v9.9"),
                                cases=_cases(), suite_id="s1"))

    assert wired["failed"]
    assert "head" in wired["failed"]
