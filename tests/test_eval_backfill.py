"""시나리오가 없는 스킬에 회귀 테스트 시나리오를 확보하는 경로(eval_backfill) 테스트.

이 기능이 지켜야 하는 것은 "시나리오를 몇 건 만들었는가" 가 아니라 **만들어진 시나리오가
회귀 테스트로 쓸모가 있는가** 다. 쓸모의 조건은 "현재(base) 버전이 실제로 통과하는
단계만 들어 있을 것" 이다 — 통과하지 못하는 단계가 섞이면 그 단계는 base/head 양쪽에서
실패해 `no_signal`(달라졌는지 알 수 없음)로 빠지고, 검증 결과를 판단 불가로 오염시킨다.

그래서 이 파일은 제안 파싱과 보정(통과 단계만 남기기) 판정을 검증한다.
"""

from __future__ import annotations

from core.skills import eval_backfill as ebf
from core.skills.eval_tools import MIN_ASSERTIONS


def _case(eval_name: str, assertions: list[str]) -> dict:
    return {
        "eval_name": eval_name,
        "prompt": f"{eval_name} 요청",
        "expected_output": None,
        "files": [],
        "assertions": assertions,
        "position": 0,
    }


def _graded(eval_name: str, steps: list[tuple[str, bool]]) -> dict:
    passed = sum(1 for _, ok in steps if ok)
    return {
        "eval_name": eval_name,
        "variant": "base",
        "passed": passed,
        "total": len(steps),
        "pass_rate": round(passed / len(steps), 4) if steps else None,
        "assertions": [{"text": t, "passed": ok, "evidence": ""} for t, ok in steps],
    }


# ---------------------------------------------------------------------------
# 제안 파싱
# ---------------------------------------------------------------------------

def test_제안을_시나리오로_정규화한다():
    cases = ebf.parse_proposals({
        "scenarios": [
            {
                "name": "계약 위험조항 검토",
                "prompt": "아래 계약서를 검토해줘\n제1조 ...",
                "expected_output": "위험 조항 목록",
                "assertions": ["위험 조항을 지적한다", "수정 문구를 제시한다"],
            }
        ]
    })

    assert len(cases) == 1
    assert cases[0]["eval_name"] == "계약-위험조항-검토"
    assert cases[0]["assertions"] == ["위험 조항을 지적한다", "수정 문구를 제시한다"]
    assert cases[0]["position"] == 0


def test_첨부_참조는_버린다():
    """첨부에 기대는 시나리오는 파일을 못 받아오는 순간 양쪽 다 0점이 되어 비교가 무너진다."""
    cases = ebf.parse_proposals({
        "scenarios": [{
            "name": "검토",
            "prompt": "첨부 문서를 검토해줘",
            "files": ["files/abcd.docx"],
            "assertions": ["A", "B"],
        }]
    })

    assert cases[0]["files"] == []


def test_assertion_이_모자란_제안은_버린다():
    cases = ebf.parse_proposals({
        "scenarios": [
            {"name": "부족", "prompt": "p", "assertions": ["하나뿐"]},
            {"name": "충분", "prompt": "p", "assertions": ["A", "B"]},
        ]
    })

    assert [c["eval_name"] for c in cases] == ["충분"]


def test_prompt_없는_제안은_버린다():
    cases = ebf.parse_proposals({"scenarios": [{"name": "빈", "prompt": "  ", "assertions": ["A", "B"]}]})

    assert cases == []


def test_이름이_겹치면_따로_구분한다():
    """같은 슬러그를 쓰면 두 시나리오가 한 디렉터리·한 DB 키를 공유해 한쪽이 덮어써진다."""
    cases = ebf.parse_proposals({
        "scenarios": [
            {"name": "검토", "prompt": "p1", "assertions": ["A", "B"]},
            {"name": "검토", "prompt": "p2", "assertions": ["C", "D"]},
        ]
    })

    assert [c["eval_name"] for c in cases] == ["검토", "검토-2"]


def test_배열만_와도_읽는다():
    cases = ebf.parse_proposals([{"name": "검토", "prompt": "p", "assertions": ["A", "B"]}])

    assert len(cases) == 1


def test_형식이_아니면_빈_목록():
    assert ebf.parse_proposals("nope") == []
    assert ebf.parse_proposals({}) == []


# ---------------------------------------------------------------------------
# 저자 프롬프트
# ---------------------------------------------------------------------------

def test_저자_프롬프트가_그대로_완성된다():
    """본문에 JSON 예시가 있어 str.format 을 쓰면 중괄호가 치환 필드로 해석돼 죽는다."""
    text = ebf.author_system()

    assert "$AMIN" not in text and "$AMAX" not in text
    assert f"between {ebf.PROPOSE_ASSERTIONS_MIN} and {ebf.PROPOSE_ASSERTIONS_MAX}" in text
    # JSON 예시가 이스케이프 없이 그대로 남아 있어야 모델이 형식을 따라 쓸 수 있다.
    assert '"scenarios"' in text
    assert "{{" not in text


# ---------------------------------------------------------------------------
# 보정 — 현재 버전이 통과한 단계만 남긴다
# ---------------------------------------------------------------------------

def test_현재_버전이_통과한_단계만_남긴다():
    cases = [_case("검토", ["A", "B", "C"])]
    results = [_graded("검토", [("A", True), ("B", False), ("C", True)])]

    kept, dropped = ebf.finalize_cases(cases, results)

    assert dropped == []
    assert kept[0]["assertions"] == ["A", "C"]


def test_통과_단계가_모자라면_시나리오를_통째로_버린다():
    """단계가 하나뿐인 시나리오는 기준선으로 쓰기엔 신호가 너무 얇다."""
    cases = [_case("검토", ["A", "B", "C"])]
    results = [_graded("검토", [("A", True), ("B", False), ("C", False)])]

    kept, dropped = ebf.finalize_cases(cases, results)

    assert kept == []
    assert dropped[0]["eval_name"] == "검토"
    assert dropped[0]["kept_assertions"] == 1
    assert str(MIN_ASSERTIONS) in dropped[0]["reason"]


def test_채점되지_않은_시나리오는_버린다():
    """실행이 실패한 시나리오를 통과로 치면 기준선이 거짓이 된다."""
    cases = [_case("실행됨", ["A", "B"]), _case("실행안됨", ["C", "D"])]
    results = [_graded("실행됨", [("A", True), ("B", True)])]

    kept, dropped = ebf.finalize_cases(cases, results)

    assert [c["eval_name"] for c in kept] == ["실행됨"]
    assert dropped[0]["eval_name"] == "실행안됨"


def test_채점_문구가_조금_달라도_순서로_짝을_찾는다():
    """채점기가 문구를 다듬어 적는 경우가 있다 — 개수가 맞으면 순서로 대응시킨다."""
    cases = [_case("검토", ["위험 조항을 지적한다", "수정 문구를 제시한다"])]
    results = [_graded("검토", [("위험 조항 지적", True), ("수정 문구 제시", True)])]

    kept, _ = ebf.finalize_cases(cases, results)

    assert kept[0]["assertions"] == ["위험 조항을 지적한다", "수정 문구를 제시한다"]


def test_짝을_찾지_못하면_통과로_치지_않는다():
    """확신할 수 없는 단계를 기준선에 넣으면 나중에 no_signal 로 빠져 결과를 흐린다."""
    cases = [_case("검토", ["A", "B", "C"])]
    results = [_graded("검토", [("전혀 다른 문구", True), ("또 다른 문구", True)])]

    kept, dropped = ebf.finalize_cases(cases, results)

    assert kept == []
    assert dropped[0]["kept_assertions"] == 0


def test_남은_시나리오의_position_을_다시_매긴다():
    cases = [_case("버려질것", ["A", "B"]), _case("남을것", ["C", "D"])]
    cases[1]["position"] = 1
    results = [
        _graded("버려질것", [("A", False), ("B", False)]),
        _graded("남을것", [("C", True), ("D", True)]),
    ]

    kept, _ = ebf.finalize_cases(cases, results)

    assert len(kept) == 1
    assert kept[0]["position"] == 0


# ---------------------------------------------------------------------------
# API 계약
# ---------------------------------------------------------------------------

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

TENANT = "t1"
SKILL = "contract-renewal-reviewer"
_PR = {"id": "pr-1", "resource_type": "skill", "resource_id": SKILL, "base_branch": "main"}


@pytest.fixture
def client():
    from core.api.skills_router import routes

    return TestClient(Starlette(routes=routes), headers={"X-Tenant-Id": TENANT})


def test_시나리오_생성을_시작하면_202와_회차를_돌려준다(client, monkeypatch):
    import core.api.skills_router as router

    started: dict = {}

    async def _execute(**kw):
        started.update(kw)

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: _PR)
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: None)
    monkeypatch.setattr(
        ebf, "create_run",
        lambda **kw: {"run": {"id": "bf-1", "status": "running", "base_ref": kw["base_ref"]},
                      "suite_id": "suite-1"},
    )
    monkeypatch.setattr(ebf, "execute_run", _execute)

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification/scenarios")

    assert r.status_code == 202, r.text
    assert r.json()["backfill"]["id"] == "bf-1"


def test_기준은_head_가_아니라_base_브랜치다(client, monkeypatch):
    """head 를 보고 만들면 바뀐 동작을 기준선으로 굳혀 회귀를 못 잡는다."""
    import core.api.skills_router as router

    captured: dict = {}

    async def _execute(**kw):
        pass

    def _create(**kw):
        captured.update(kw)
        return {"run": {"id": "bf-1", "status": "running"}, "suite_id": "suite-1"}

    pr = {**_PR, "base_branch": "main", "branch_name": "feature/변경"}
    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: pr)
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: None)
    monkeypatch.setattr(ebf, "create_run", _create)
    monkeypatch.setattr(ebf, "execute_run", _execute)

    client.post(f"/skills/{SKILL}/pull-requests/7/verification/scenarios")

    assert captured["base_ref"] == "main"


def test_이미_시나리오가_있으면_409(client, monkeypatch):
    """기존 기준선을 새로 만든 것으로 덮으면 그동안의 비교 근거가 사라진다."""
    import core.api.skills_router as router

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: _PR)
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: None)
    monkeypatch.setattr(
        ebf, "create_run",
        lambda **kw: {"error": "suite_exists", "message": "이미 있습니다."},
    )

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification/scenarios")

    assert r.status_code == 409
    assert r.json()["error"] == "suite_exists"


def test_이미_생성_중이면_새_회차를_만들지_않는다(client, monkeypatch):
    import core.api.skills_router as router

    running = {"id": "bf-1", "status": "running"}
    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: _PR)
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: running)
    monkeypatch.setattr(
        ebf, "create_run",
        lambda **kw: pytest.fail("이미 생성 중인데 새 회차를 만들었다"),
    )

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification/scenarios")

    assert r.status_code == 200
    assert r.json()["backfill"]["id"] == "bf-1"


def test_존재하지_않는_병합요청이면_404(client, monkeypatch):
    import core.api.skills_router as router

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: None)

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification/scenarios")

    assert r.status_code == 404


def test_케이스가_없는_스위트는_시나리오_없음으로_본다(client, monkeypatch):
    """생성 회차가 스위트 행을 먼저 만들므로, 스위트 존재만 보면 '있다' 로 오인한다."""
    import core.api.skills_router as router
    from core.skills import eval_tools, pr_verification as prv

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: _PR)
    monkeypatch.setattr(prv, "latest_run_for_pr", lambda *a, **kw: None)
    monkeypatch.setattr(eval_tools, "find_suite", lambda *a, **kw: "suite-1")
    monkeypatch.setattr(eval_tools, "load_suite_cases", lambda _sid: [])
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: {"id": "bf-1", "status": "running"})

    body = client.get(f"/skills/{SKILL}/pull-requests/7/verification").json()

    assert body["has_suite"] is False
    assert body["backfill"]["status"] == "running"
