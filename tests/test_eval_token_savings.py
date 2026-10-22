"""회귀 검증이 같은 답을 얻으려고 모델을 반복해서 부르지 않는지.

세 가지 누수를 막는다.

1. 기준선(base) 재실행 — base 는 기준 브랜치의 특정 커밋에 대해 고정인데 검증할 때마다
   다시 실행하고 다시 채점했다. 비용이 두 배일 뿐 아니라 같은 PR 을 두 번 검증하면
   기준선 점수가 흔들려 "이 변경으로 통과하게 된 단계" 가 변경과 무관하게 떴다.
2. 기계로 검사 가능한 assertion 을 모델로 채점 — evals.json 은 이미
   `{"type":"contains","value":"…"}` 형태를 담고 있는데 문자열로 뭉개 채점 서브에이전트에
   넘겼다. 문자열 포함 여부를 확인하려고 LLM 을 부르는 셈이었다.
3. 대조군(without_skill) 재실행 — 스킬을 붙이지 않고 돌린 실행이라 SKILL.md 를 고쳐도
   결과가 달라질 수 없는데 회차마다 다시 돌렸다.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.skills import eval_tools


# --------------------------------------------------------------------------- 1
def _results_row(name: str, texts: list[str]) -> dict:
    return {
        "eval_name": name,
        "variant": "base",
        "passed": len(texts),
        "total": len(texts),
        "pass_rate": 1.0,
        "assertions": [{"text": t, "passed": True, "evidence": "e"} for t in texts],
        "outputs_dir": f"/workspace/old/{name}",
        "run_id": "run-1",
    }


def _supabase(runs: list[dict], results: list[dict]):
    """테이블별로 같은 mock 을 돌려준다 — 호출마다 새로 만들면 호출 기록이 사라진다."""
    tables: dict[str, MagicMock] = {}

    def table(name):
        if name not in tables:
            q = MagicMock()
            for m in ("select", "eq", "in_", "order", "limit"):
                getattr(q, m).return_value = q
            q.execute.return_value = MagicMock(
                data=runs if name == "resource_eval_runs" else results
            )
            tables[name] = q
        return tables[name]

    sb = MagicMock()
    sb.table.side_effect = table
    return sb


def test_baseline_from_same_commit_is_reused():
    cases = [{"eval_name": "a", "assertions": ["x", "y"]}]
    sb = _supabase([{"id": "run-1"}], [_results_row("a", ["x", "y"])])

    with patch.object(eval_tools, "_sb", return_value=sb):
        reused = eval_tools.reusable_baseline_results(
            tenant_id="t", resource_id="skill", digest="deadbeef", variant="base", cases=cases
        )

    assert set(reused) == {"a"}
    assert reused["a"]["passed"] == 2
    assert reused["a"]["outputs_dir"] == "/workspace/old/a", "근거를 다시 볼 경로가 유지돼야 한다"


def test_baseline_is_not_reused_when_assertions_changed():
    """문구가 바뀐 시나리오까지 재사용하면 다른 기준으로 매긴 점수를 비교하게 된다."""
    cases = [{"eval_name": "a", "assertions": ["x", "z"]}]  # 저장된 건 x, y
    sb = _supabase([{"id": "run-1"}], [_results_row("a", ["x", "y"])])

    with patch.object(eval_tools, "_sb", return_value=sb):
        reused = eval_tools.reusable_baseline_results(
            tenant_id="t", resource_id="skill", digest="deadbeef", variant="base", cases=cases
        )

    assert reused == {}


def test_baseline_reuse_queries_columns_that_exist():
    """실제 스키마에 없는 컬럼으로 정렬하면 조회가 통째로 실패해 재사용이 늘 꺼진다.

    처음 구현에서 `created_at` 으로 정렬했는데 resource_eval_runs 에는 그 컬럼이 없어
    (있는 것은 started_at) 매번 42703 으로 떨어졌고, 폴백 때문에 겉으로는 멀쩡히 동작하면서
    절감만 조용히 사라졌다. 모킹된 단위 테스트로는 안 잡히므로 컬럼명을 여기서 고정한다.
    """
    sb = _supabase([], [])
    with patch.object(eval_tools, "_sb", return_value=sb):
        eval_tools.reusable_baseline_results(
            tenant_id="t", resource_id="s", digest="d", variant="base",
            cases=[{"eval_name": "a", "assertions": ["x"]}],
        )

    runs_table = sb.table("resource_eval_runs")
    ordered = [c.args[0] for c in runs_table.order.call_args_list if c.args]
    assert ordered == ["started_at"], f"resource_eval_runs 에 없는 컬럼으로 정렬한다: {ordered}"


def test_baseline_reuse_needs_a_digest():
    """digest 가 없으면 어느 버전의 결과인지 알 수 없으므로 재사용하지 않는다."""
    with patch.object(eval_tools, "_sb", side_effect=AssertionError("조회하면 안 된다")):
        assert eval_tools.reusable_baseline_results(
            tenant_id="t", resource_id="s", digest="", variant="base",
            cases=[{"eval_name": "a", "assertions": ["x"]}],
        ) == {}


# --------------------------------------------------------------------------- 2
def test_structured_assertions_keep_their_check():
    texts, checks = eval_tools.normalize_assertions([
        {"type": "contains", "value": "판정: 승인"},
        {"type": "not_contains", "value": "TODO"},
        "판단 근거가 문서에 드러난다",
    ])
    assert "판정: 승인" in texts[0], texts
    assert checks[0] == {"type": "contains", "value": "판정: 승인"}
    assert checks[1] == {"type": "not_contains", "value": "TODO"}
    assert checks[2] is None, "의미 판단이 필요한 문장은 기계 검사로 만들지 않는다"


def test_storage_round_trip_preserves_checks():
    texts, checks = eval_tools.normalize_assertions([{"type": "contains", "value": "승인"}, "근거"])
    stored = eval_tools.assertions_for_storage(texts, checks)
    again_texts, again_checks = eval_tools.normalize_assertions(stored)
    assert again_texts == texts
    assert again_checks == checks


def test_plain_assertions_are_stored_as_before():
    texts, checks = eval_tools.normalize_assertions(["a", "b"])
    assert eval_tools.assertions_for_storage(texts, checks) == ["a", "b"]


def test_all_mechanical_assertions_are_graded_in_code(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_text("판정: 승인\n근거를 적었다\n", encoding="utf-8")

    texts, checks = eval_tools.normalize_assertions([
        {"type": "contains", "value": "판정: 승인"},
        {"type": "not_contains", "value": "판정: 반려"},
    ])
    grading = eval_tools.grade_mechanically(outputs, texts, checks)

    assert grading is not None, "모델 없이 채점할 수 있어야 한다"
    assert grading["summary"] == {"passed": 2, "failed": 0, "total": 2, "pass_rate": 1.0}
    assert grading["graded_by"] == "code"
    # grading.json 계약(text/passed/evidence)을 그대로 지킨다.
    assert all({"text", "passed", "evidence"} <= set(e) for e in grading["expectations"])


def test_failing_mechanical_assertion_is_reported(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_text("판정: 반려\n", encoding="utf-8")

    texts, checks = eval_tools.normalize_assertions([{"type": "contains", "value": "판정: 승인"}])
    grading = eval_tools.grade_mechanically(outputs, texts, checks)

    assert grading["summary"]["passed"] == 0
    assert grading["expectations"][0]["passed"] is False


def test_semantic_assertion_still_goes_to_the_model(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "report.md").write_text("판정: 승인", encoding="utf-8")

    texts, checks = eval_tools.normalize_assertions([
        {"type": "contains", "value": "판정: 승인"},
        "판단 근거가 설득력 있게 정리돼 있다",
    ])
    assert eval_tools.grade_mechanically(outputs, texts, checks) is None


def test_missing_outputs_is_not_graded_as_all_failed(tmp_path):
    """실행이 실패해 산출물이 없는 것을 '전부 실패' 로 바꾸면 base/head 비교가 어긋난다."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    texts, checks = eval_tools.normalize_assertions([{"type": "contains", "value": "승인"}])
    assert eval_tools.grade_mechanically(outputs, texts, checks) is None


# --------------------------------------------------------------------------- 3
@pytest.fixture
def skill_workspace(tmp_path, monkeypatch):
    """대화방 작업 디렉터리를 임시 경로로 격리한다."""
    ws = tmp_path / "workspace"
    room = ws / ".bpmn" / "room-1"
    room.mkdir(parents=True)
    monkeypatch.setattr(eval_tools._skill_tools, "tenant_workspace_root", lambda tenant_id=None: ws)
    monkeypatch.setattr(eval_tools, "_bpmn_work_dir", lambda: room)
    return room


def _write_skill(room: Path, evals: list[dict]) -> Path:
    skill = room / "skills" / "demo-skill"
    (skill / "evals").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: d\n---\n", encoding="utf-8")
    (skill / "evals" / "evals.json").write_text(
        json.dumps({"skill_name": "demo-skill", "evals": evals}, ensure_ascii=False), encoding="utf-8"
    )
    return skill


def _cases(n: int = 2) -> list[dict]:
    return [
        {
            "id": f"c{i}", "name": f"case-{i}", "prompt": "p", "expected_output": "e",
            "files": [], "assertions": ["첫째 조건", "둘째 조건"],
        }
        for i in range(n)
    ]


def test_control_runs_are_carried_over_to_the_next_iteration(skill_workspace):
    skill = _write_skill(skill_workspace, _cases())

    # iteration-1 이 이미 돌아 대조군 결과가 남아 있는 상태를 만든다.
    runs_root = eval_tools.eval_runs_root("demo-skill") / "iteration-1"
    for name in ("case-0", "case-1"):
        for variant in ("with_skill", "without_skill"):
            d = runs_root / name / variant
            (d / "outputs").mkdir(parents=True)
            (d / "outputs" / "report.md").write_text(f"{name}/{variant}", encoding="utf-8")
            (d / "grading.json").write_text(
                json.dumps({"expectations": [], "summary": {"passed": 1, "total": 2}}), encoding="utf-8"
            )
    # with_skill 하나를 지워 "아직 덜 끝난 회차" 로 만든다(= 새 회차를 계획해야 하는 상황).
    (runs_root / "case-0" / "with_skill" / "grading.json").unlink()

    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill)}))

    assert out["iteration"] == 2
    variants = sorted({r["variant"] for r in out["runs"]})
    assert variants == ["with_skill"], f"대조군을 다시 돌리면 안 된다: {out['runs']}"

    # 이월된 결과가 새 회차 자리에 실제로 있어야 이후 채점 수집이 성립한다.
    carried = eval_tools.eval_runs_root("demo-skill") / "iteration-2" / "case-0" / "without_skill"
    assert (carried / "grading.json").is_file()
    assert (carried / "outputs" / "report.md").read_text(encoding="utf-8") == "case-0/without_skill"


def test_first_iteration_runs_both_variants(skill_workspace):
    """앞 회차가 없으면 이월할 것이 없으므로 대조군도 돌린다."""
    skill = _write_skill(skill_workspace, _cases())
    out = json.loads(eval_tools.plan_skill_eval.invoke({"skill_path": str(skill)}))

    assert out["iteration"] == 1
    assert sorted({r["variant"] for r in out["runs"]}) == ["with_skill", "without_skill"]
