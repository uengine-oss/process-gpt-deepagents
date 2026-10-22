"""생성 시 검증이 통과시킨 케이스가 회귀 스위트로 옮겨지는지 고정한다.

프로세스 회귀 시나리오는 새로 만들지 않는다. `/validate-and-improve` 가 이미 분기별
케이스를 모델로 만들고 실행 엔진으로 돌려 통과를 확인했으므로, 그중 결함 없이 통과한 것을
그대로 옮겨 오면 된다. 여기서 지키는 것:

- 승격에는 모델 호출이 없다. 있으면 회귀 한 번이 생성 한 번만큼 든다.
- 기준값은 실제 실행 경로다(모델이 추론한 기대 순서가 아니라).
- 단언은 전부 값 비교다. 의미 판단형이 섞이면 채점에 모델이 끼어든다.
- 실행 경로가 없는 케이스는 버린다 — base/head 양쪽에서 판정 불가로 빠져 회차를 오염시킨다.
"""

from __future__ import annotations

import json

import pytest

from core.skills import process_scenarios as ps


_DEFAULT_ORDER = ["apply", "detail_review", "register"]


def _passing_case(name="고액 승인 경로", order=_DEFAULT_ORDER, decisions=None, reached_end=True):
    return {
        "name": name,
        "activity_inputs": {"apply": {"amount": 1200000}},
        "expected_activity_order": list(order),
        "proposed_activity_order": ["apply", "detail_review", "register"],
        "gateway_decisions": decisions if decisions is not None else {
            "gw_amount": {"selected": ["seq_high"]}
        },
        "reached_end": reached_end,
    }


def test_case_carries_inputs_and_branch_decisions():
    """입력값과 분기 판정이 함께 실린다.

    경로만 남기면 두 분기가 같은 액티비티로 향할 때 어느 조건이 참이었는지 되찾을 수 없어,
    엔진 없이 재생할 수 없다.
    """
    cases = ps.to_eval_cases([_passing_case()])

    assert len(cases) == 1
    prompt = json.loads(cases[0]["prompt"])
    assert prompt["activity_inputs"] == {"apply": {"amount": 1200000}}
    assert prompt["gateway_decisions"] == {"gw_amount": {"selected": ["seq_high"]}}


def test_expected_output_is_the_executed_path():
    cases = ps.to_eval_cases([_passing_case(order=["a", "b", "c"])])

    expected = json.loads(cases[0]["expected_output"])
    assert expected["activity_order"] == ["a", "b", "c"]
    assert expected["reached_end"] is True


def test_all_assertions_are_mechanical():
    """단언에 기계 검사가 빠지면 그 케이스는 채점이 모델로 넘어간다."""
    cases = ps.to_eval_cases([_passing_case()])

    checks = cases[0]["checks"]
    assert len(checks) == len(cases[0]["assertions"])
    assert all(isinstance(c, dict) and c.get("type") for c in checks)
    assert {c["type"] for c in checks} == {ps.CHECK_PATH_EQUALS, ps.CHECK_REACHED_END}
    assert checks[0]["value"] == ["apply", "detail_review", "register"]


def test_case_without_a_path_is_dropped():
    cases = ps.to_eval_cases([_passing_case(order=[])])

    assert cases == []


def test_case_without_a_name_is_dropped():
    """이름은 base↔head 대조 키다. 없으면 어느 케이스와 비교할지 정할 수 없다."""
    cases = ps.to_eval_cases([_passing_case(name="  ")])

    assert cases == []


def test_positions_follow_input_order():
    cases = ps.to_eval_cases([
        _passing_case(name="승인"),
        _passing_case(name="반려", order=["apply", "reject"]),
    ])

    assert [c["eval_name"] for c in cases] == ["승인", "반려"]
    assert [c["position"] for c in cases] == [0, 1]


def test_scenarios_never_reference_attachments():
    """첨부를 참조하면 파일을 못 받아오는 순간 두 버전이 나란히 실패해 비교가 무너진다."""
    cases = ps.to_eval_cases([_passing_case()])

    assert cases[0]["files"] == []


def test_promote_refuses_when_nothing_passed(monkeypatch):
    """통과한 케이스가 없으면 빈 스위트를 만들지 않는다.

    빈 스위트가 생기면 화면은 "시나리오 있음" 으로 보이는데 검증은 비교할 게 없어 실패한다.
    """
    called = []
    monkeypatch.setattr(ps, "upsert_suite", lambda *a, **k: called.append(a) or "suite-1")

    result = ps.promote(tenant_id="tn", resource_id="proc-1", passing_cases=[])

    assert result["error"] == "no_cases"
    assert called == []


def test_promote_writes_cases_to_the_process_suite(monkeypatch):
    seen = {}

    def _upsert(tenant_id, resource_id, resource_type):
        seen["suite"] = (tenant_id, resource_id, resource_type)
        return "suite-1"

    def _sync(tenant_id, suite_id, cases):
        seen["cases"] = cases
        return {c["eval_name"]: f"case-{i}" for i, c in enumerate(cases)}

    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "")
    monkeypatch.setattr(ps, "upsert_suite", _upsert)
    monkeypatch.setattr(ps, "sync_cases", _sync)

    result = ps.promote(
        tenant_id="tn", resource_id="proc-1",
        passing_cases=[_passing_case(), _passing_case(name="반려", order=["apply", "reject"])],
    )

    assert seen["suite"] == ("tn", "proc-1", "bpmn")
    assert result["case_count"] == 2
    assert [c["eval_name"] for c in seen["cases"]] == ["고액 승인 경로", "반려"]


@pytest.mark.parametrize("replace", [True, False])
def test_existing_suite_is_reused_not_duplicated(monkeypatch, replace):
    """같은 프로세스를 다시 검증해도 스위트는 하나다."""
    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "load_suite_cases", lambda sid: [
        {"eval_name": "고액 승인 경로", "position": 0}
    ])
    monkeypatch.setattr(ps, "_drop_cases_except", lambda sid, keep: 0)
    monkeypatch.setattr(ps, "upsert_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "sync_cases", lambda t, s, cases: {c["eval_name"]: "id" for c in cases})

    result = ps.promote(
        tenant_id="tn", resource_id="proc-1",
        passing_cases=[_passing_case()], replace=replace,
    )

    assert result["suite_id"] == "suite-1"


# --------------------------------------------------------------------------- 순서
def test_new_cases_do_not_take_an_existing_case_position(monkeypatch):
    """검증을 다시 돌려 케이스가 늘어도 표시 순서가 겹치지 않는다.

    처음엔 새 목록의 0,1,2… 를 그대로 썼는데, 두 번째 승격에서 새 케이스가 이미 있는
    케이스와 같은 position 을 받았다. position 정렬이 임의 순서가 되어 화면의 시나리오
    순서가 회차마다 흔들린다.
    """
    seen = {}
    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "load_suite_cases", lambda sid: [
        {"eval_name": "고액 승인 경로", "position": 0},
        {"eval_name": "일반 경로", "position": 1},
    ])
    monkeypatch.setattr(ps, "upsert_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "sync_cases", lambda t, s, cases: seen.update(cases=cases) or {
        c["eval_name"]: "id" for c in cases
    })

    ps.promote(
        tenant_id="tn", resource_id="proc-1",
        passing_cases=[
            _passing_case(name="고액 승인 경로"),          # 이미 있음 → 자리 유지
            _passing_case(name="반려 경로", order=["apply", "reject"]),  # 신규
        ],
    )

    by_name = {c["eval_name"]: c["position"] for c in seen["cases"]}
    assert by_name["고액 승인 경로"] == 0          # 기존 자리를 지킨다
    assert by_name["반려 경로"] == 2               # 기존 최대(1) 다음
    assert by_name["반려 경로"] != 1               # '일반 경로' 자리를 뺏지 않는다


def test_replace_drops_cases_missing_from_the_new_set(monkeypatch):
    dropped = {}
    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "load_suite_cases", lambda sid: [
        {"eval_name": "옛 경로", "position": 0},
    ])
    monkeypatch.setattr(ps, "_drop_cases_except",
                        lambda sid, keep: dropped.update(keep=keep) or 1)
    monkeypatch.setattr(ps, "upsert_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "sync_cases", lambda t, s, cases: {c["eval_name"]: "id" for c in cases})

    result = ps.promote(
        tenant_id="tn", resource_id="proc-1",
        passing_cases=[_passing_case(name="새 경로")], replace=True,
    )

    assert dropped["keep"] == {"새 경로"}
    assert result["dropped_count"] == 1


# --------------------------------------------------------------------------- 기준 버전
async def test_build_records_the_baseline_version(monkeypatch):
    """어느 버전 기준으로 굳혔는지 스위트에 남는다.

    리뷰어는 시나리오를 다시 만들 때 기준 버전을 고를 수 있다(이 요청이 갈라져 나온 버전 /
    지금 운영 중인 버전). 통과·실패 숫자는 그 기준에 대한 상대값이라, 무엇을 기준으로 삼았는지
    적어 두지 않으면 화면이 "무엇에 비해 깨졌는가" 를 말할 수 없다.
    """
    from core.skills import regression_client as rc

    seen = {}

    async def _derive(*, tenant_id, resource_id, base_ref):
        seen["derive_ref"] = base_ref
        return {"cases": [{"eval_name": "고액 승인 경로", "prompt": "{}", "assertions": [], "checks": []}]}

    def _upsert(tenant_id, resource_id, resource_type, baseline_ref=""):
        seen["baseline_ref"] = baseline_ref
        return "suite-1"

    monkeypatch.setattr(rc, "derive_process_cases", _derive)
    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "")
    monkeypatch.setattr(ps, "upsert_suite", _upsert)
    monkeypatch.setattr(ps, "sync_cases", lambda t, s, cases: {c["eval_name"]: "case-0" for c in cases})

    result = await ps.build(tenant_id="tn", resource_id="proc-1", base_ref="v1.0", replace=True)

    # 고른 버전이 파생과 기록에 같은 값으로 가야 한다 — 한쪽만 바뀌면 화면의 기준 표기가
    # 실제로 시나리오를 뽑은 버전과 어긋나 거짓을 말한다.
    assert seen["derive_ref"] == "v1.0"
    assert seen["baseline_ref"] == "v1.0"
    assert result["baseline_ref"] == "v1.0"


def test_without_replace_nothing_is_deleted(monkeypatch):
    """기본은 지키는 쪽이다 — 검증을 다시 돌릴 때마다 옛 기준이 사라지면 안 된다."""
    monkeypatch.setattr(ps, "find_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "load_suite_cases", lambda sid: [{"eval_name": "옛 경로", "position": 0}])
    monkeypatch.setattr(ps, "_drop_cases_except",
                        lambda *a: (_ for _ in ()).throw(AssertionError("삭제하면 안 된다")))
    monkeypatch.setattr(ps, "upsert_suite", lambda *a, **k: "suite-1")
    monkeypatch.setattr(ps, "sync_cases", lambda t, s, cases: {c["eval_name"]: "id" for c in cases})

    result = ps.promote(tenant_id="tn", resource_id="proc-1",
                        passing_cases=[_passing_case(name="새 경로")])

    assert result["dropped_count"] == 0
