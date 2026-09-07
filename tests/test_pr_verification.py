"""병합 전 검증(pre-merge verification) 테스트.

검증의 목적은 "이 병합 요청을 병합하면 **기존 동작이 깨지는가**" 를 병합 전에 답하는 것이다.
따라서 핵심은 통과율 숫자가 아니라 **base(변경 전)에서 통과하던 단계가 head(변경 후)에서
깨졌는지**를 정확히 골라내는 것이고, 이 파일이 그 판정과 API 계약을 검증한다.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from core.skills import pr_verification as prv

TENANT = "t1"
SKILL = "nda-review"


def _result(eval_name: str, variant: str, steps: list[tuple[str, bool]], evidence: str = "") -> dict:
    passed = sum(1 for _, ok in steps if ok)
    return {
        "eval_name": eval_name,
        "variant": variant,
        "passed": passed,
        "total": len(steps),
        "pass_rate": round(passed / len(steps), 4) if steps else None,
        "assertions": [
            {"text": t, "passed": ok, "evidence": "" if ok else evidence} for t, ok in steps
        ],
    }


# ---------------------------------------------------------------------------
# 변경 전 → 변경 후 비교
# ---------------------------------------------------------------------------

def test_통과하던_단계가_깨지면_broken_으로_잡는다():
    results = [
        _result("계약검토", "base", [("독소조항 지적", True), ("수정문구 제시", True)]),
        _result("계약검토", "head", [("독소조항 지적", True), ("수정문구 제시", False)], "출력에 없음"),
    ]

    diff = prv.diff_results(results)

    assert diff["broken_count"] == 1
    assert diff["broken"][0]["eval_name"] == "계약검토"
    assert diff["broken"][0]["step"] == "수정문구 제시"
    assert diff["broken"][0]["evidence"] == "출력에 없음"
    assert diff["fixed_count"] == 0


def test_실패하던_단계가_통과하면_fixed_로_잡는다():
    results = [
        _result("계약검토", "base", [("보험 요구 확인", False)], "언급 없음"),
        _result("계약검토", "head", [("보험 요구 확인", True)]),
    ]

    diff = prv.diff_results(results)

    assert diff["fixed_count"] == 1
    assert diff["fixed"][0]["step"] == "보험 요구 확인"
    assert diff["broken_count"] == 0


def test_양쪽_다_실패한_단계는_회귀가_아니다():
    """원래도 안 되던 것은 이 병합이 깨뜨린 게 아니다 — 병합을 막을 근거가 못 된다."""
    results = [
        _result("계약검토", "base", [("보험 요구 확인", False)]),
        _result("계약검토", "head", [("보험 요구 확인", False)]),
    ]

    diff = prv.diff_results(results)

    assert diff["broken_count"] == 0
    assert diff["fixed_count"] == 0


def test_통과율이_같아도_다른_단계가_깨졌으면_잡아낸다():
    """총점만 비교하면 놓치는 경우 — 단계 단위로 봐야 한다."""
    results = [
        _result("계약검토", "base", [("A", True), ("B", False)]),
        _result("계약검토", "head", [("A", False), ("B", True)]),
    ]

    diff = prv.diff_results(results)

    assert diff["broken_count"] == 1 and diff["broken"][0]["step"] == "A"
    assert diff["fixed_count"] == 1 and diff["fixed"][0]["step"] == "B"


def test_채점_문구_순서가_바뀌어도_같은_단계끼리_비교한다():
    results = [
        _result("계약검토", "base", [("A", True), ("B", True)]),
        _result("계약검토", "head", [("B", True), ("A", False)]),
    ]

    diff = prv.diff_results(results)

    assert diff["broken_count"] == 1
    assert diff["broken"][0]["step"] == "A"


def test_한쪽_실행이_없으면_통과로_치지_않고_비교불가로_보고한다():
    """실행 실패를 조용히 '이상 없음' 으로 바꾸면 검증이 있으나 마나다."""
    results = [_result("계약검토", "head", [("A", True)])]

    diff = prv.diff_results(results)

    assert diff["incomparable"] == ["계약검토"]
    assert diff["broken_count"] == 0
    assert diff["per_case"]["계약검토"]["comparable"] is False


def test_시나리오별로_깨진_단계를_묶어_보고한다():
    results = [
        _result("c1", "base", [("A", True)]),
        _result("c1", "head", [("A", False)]),
        _result("c2", "base", [("B", True)]),
        _result("c2", "head", [("B", True)]),
    ]

    diff = prv.diff_results(results)

    assert len(diff["per_case"]["c1"]["broken"]) == 1
    assert diff["per_case"]["c2"]["broken"] == []
    assert diff["per_case"]["c2"]["head"] == {"passed": 1, "total": 1, "pass_rate": 1.0}


# ---------------------------------------------------------------------------
# 회차 생성 가드
# ---------------------------------------------------------------------------

def test_시나리오가_없는_리소스는_검증_회차를_만들지_않는다(monkeypatch):
    """비교할 기준이 없는데 회차만 만들면 '검증했다' 는 착시를 준다."""
    monkeypatch.setattr(prv, "find_suite", lambda *a, **kw: "")

    out = prv.create_run(tenant_id=TENANT, pr={"id": "pr-1", "resource_type": "skill", "resource_id": SKILL})

    assert out["error"] == "no_suite"
    assert "시나리오가 없습니다" in out["message"]


def test_스위트는_있는데_케이스가_비면_거부한다(monkeypatch):
    monkeypatch.setattr(prv, "find_suite", lambda *a, **kw: "suite-1")
    monkeypatch.setattr(prv, "load_suite_cases", lambda _sid: [])

    out = prv.create_run(tenant_id=TENANT, pr={"id": "pr-1", "resource_type": "skill", "resource_id": SKILL})

    assert out["error"] == "no_cases"


def test_회차는_PR_과_base_head_브랜치를_함께_기록한다(monkeypatch):
    """어떤 병합 요청의 어느 두 버전을 비교했는지가 남아야 병합 판단의 근거가 된다."""
    captured = {}

    class _T:
        def insert(self, payload):
            captured.update(payload)
            return self

        def execute(self):
            return type("R", (), {"data": [{"id": "run-1", **captured}]})()

    monkeypatch.setattr(prv, "find_suite", lambda *a, **kw: "suite-1")
    monkeypatch.setattr(prv, "load_suite_cases", lambda _sid: [{"id": "c1", "eval_name": "c1"}])
    monkeypatch.setattr(prv, "_sb", lambda: type("S", (), {"table": lambda _s, _n: _T()})())

    out = prv.create_run(
        tenant_id=TENANT,
        pr={
            "id": "pr-1",
            "resource_type": "skill",
            "resource_id": SKILL,
            "base_branch": "main",
            "branch_name": "feature/v2",
        },
        created_by="user-1",
    )

    assert out["run"]["id"] == "run-1"
    assert captured["pr_id"] == "pr-1"
    assert captured["base_ref"] == "main"
    assert captured["head_ref"] == "feature/v2"
    assert captured["trigger"] == "pull_request"
    assert captured["status"] == "running"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """테넌트 가드는 요청이 어느 테넌트인지 밝히길 요구한다(스텁 신원이 2개 테넌트 소속)."""
    from core.api.skills_router import routes

    return TestClient(Starlette(routes=routes), headers={"X-Tenant-Id": TENANT})


def test_존재하지_않는_병합요청이면_404(client, monkeypatch):
    import core.api.skills_router as router

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: None)

    r = client.get(f"/skills/{SKILL}/pull-requests/7/verification")

    assert r.status_code == 404


def test_검증_조회는_회차와_시나리오를_함께_돌려준다(client, monkeypatch):
    import core.api.skills_router as router
    from core.skills import eval_backfill as ebf, eval_tools

    pr = {"id": "pr-1", "resource_type": "skill", "resource_id": SKILL}
    run = {"id": "run-1", "status": "succeeded", "broken_count": 1, "fixed_count": 0}
    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: pr)
    monkeypatch.setattr(prv, "latest_run_for_pr", lambda *a, **kw: run)
    monkeypatch.setattr(prv, "run_results", lambda *a, **kw: [{"eval_name": "c1", "variant": "head"}])
    monkeypatch.setattr(eval_tools, "find_suite", lambda *a, **kw: "suite-1")
    monkeypatch.setattr(eval_tools, "load_suite_cases", lambda _sid: [{"eval_name": "c1", "prompt": "p"}])
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: None)

    r = client.get(f"/skills/{SKILL}/pull-requests/7/verification")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["broken_count"] == 1
    assert body["results"][0]["variant"] == "head"
    assert body["has_suite"] is True
    assert body["cases"][0]["eval_name"] == "c1"


def test_아직_실행_전이면_회차없이_시나리오만_준다(client, monkeypatch):
    """실행 전에도 '무엇을 검증하게 되는지' 는 보여야 한다."""
    import core.api.skills_router as router
    from core.skills import eval_backfill as ebf, eval_tools

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: {"id": "pr-1", "resource_type": "skill", "resource_id": SKILL})
    monkeypatch.setattr(prv, "latest_run_for_pr", lambda *a, **kw: None)
    monkeypatch.setattr(eval_tools, "find_suite", lambda *a, **kw: "suite-1")
    monkeypatch.setattr(eval_tools, "load_suite_cases", lambda _sid: [{"eval_name": "c1", "prompt": "p"}])
    monkeypatch.setattr(ebf, "latest_run", lambda *a, **kw: None)

    body = client.get(f"/skills/{SKILL}/pull-requests/7/verification").json()

    assert body["run"] is None
    assert body["has_suite"] is True
    assert len(body["cases"]) == 1


def test_시나리오가_없으면_검증_실행_요청이_409(client, monkeypatch):
    import core.api.skills_router as router

    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: {"id": "pr-1", "resource_type": "skill", "resource_id": SKILL})
    monkeypatch.setattr(prv, "latest_run_for_pr", lambda *a, **kw: None)
    monkeypatch.setattr(prv, "create_run", lambda **kw: {"error": "no_suite", "message": "시나리오가 없습니다."})

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification")

    assert r.status_code == 409
    assert r.json()["error"] == "no_suite"


def test_이미_실행_중이면_새_회차를_만들지_않는다(client, monkeypatch):
    import core.api.skills_router as router

    running = {"id": "run-1", "status": "running"}
    created = []
    monkeypatch.setattr(router, "_find_pr_record", lambda *a, **kw: {"id": "pr-1", "resource_type": "skill", "resource_id": SKILL})
    monkeypatch.setattr(prv, "latest_run_for_pr", lambda *a, **kw: running)
    monkeypatch.setattr(prv, "create_run", lambda **kw: created.append(kw) or {"error": "x", "message": "x"})

    r = client.post(f"/skills/{SKILL}/pull-requests/7/verification")

    assert r.status_code == 200
    assert r.json()["run"]["status"] == "running"
    assert created == [], "이미 돌고 있는데 회차를 또 만들면 안 된다"


# ---------------------------------------------------------------------------
# 시나리오 입력 문서 준비
#
# 시나리오 프롬프트는 대개 "첨부한 NDA 를 검토해줘" 처럼 첨부 문서를 전제로 쓰인다.
# 그 문서를 실제로 놓아주지 않으면 두 버전 모두 "원문 없음" 으로 전부 실패하고,
# broken_count 가 0 이라 검증을 통과한 것처럼 보인다. 실제로 그렇게 나왔다.
# ---------------------------------------------------------------------------

def test_양쪽_다_0점인_시나리오는_통과가_아니라_신호없음으로_표시한다():
    results = [
        _result("첨부검토", "base", [("A", False), ("B", False)]),
        _result("첨부검토", "head", [("A", False), ("B", False)]),
        _result("정상", "base", [("C", True)]),
        _result("정상", "head", [("C", True)]),
    ]

    diff = prv.diff_results(results)

    assert diff["broken_count"] == 0
    assert diff["no_signal"] == ["첨부검토"], "둘 다 0점이면 '이상 없음' 이 아니라 신호 없음이다"


def _stub_downloader(tmp_path, calls):
    class _DL:
        def invoke(self, args):
            calls.append(args["storage_path"])
            local = tmp_path / "in" / args["storage_path"].split("/")[-1]
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"PK\x03\x04binary")
            return str(local)

    return _DL()


class _Parser:
    def __init__(self, text="--- Body Text ---\n계약서 본문"):
        self.text = text
        self.seen = []

    def invoke(self, args):
        self.seen.append(args["file_path"])
        return self.text


def test_바이너리_문서는_텍스트로_풀어서_넘긴다(tmp_path, monkeypatch):
    """`.docx` 를 그대로 넘기면 실행기가 read_file 로 읽고, deepagents 가 그것을 문서 첨부
    블록으로 모델에 보낸다. 그 블록을 거부하는 모델에서는 요청이 400 으로 죽어 산출물이
    하나도 남지 않는다(실측). 그래서 바이너리를 모델 앞에 두지 않는다."""
    from core.skills import tools as skill_tools

    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path)
    calls = []
    parser = _Parser()
    monkeypatch.setattr("core.storage.storage.download_file", _stub_downloader(tmp_path, calls))
    monkeypatch.setattr(skill_tools, "extract_document_structure", parser)

    resolved, failed = prv._materialize_inputs(
        tmp_path / "run", {"eval_name": "c1", "files": ["files/a.docx", "files/b.pdf"]}
    )

    assert calls == ["files/a.docx", "files/b.pdf"]
    assert failed == []
    assert len(parser.seen) == 2, "문서는 파서를 거쳐야 한다"
    assert all(p.endswith(".txt") for p in resolved), resolved
    assert all(p.startswith("/workspace/") for p in resolved), resolved
    assert (tmp_path / "in" / "a.docx.txt").read_text(encoding="utf-8").endswith("계약서 본문")


def test_이미_텍스트인_입력은_그대로_넘긴다(tmp_path, monkeypatch):
    from core.skills import tools as skill_tools

    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path)
    parser = _Parser()
    monkeypatch.setattr("core.storage.storage.download_file", _stub_downloader(tmp_path, []))
    monkeypatch.setattr(skill_tools, "extract_document_structure", parser)

    resolved, _ = prv._materialize_inputs(tmp_path / "run", {"eval_name": "c1", "files": ["files/a.md"]})

    assert parser.seen == [], "텍스트 파일까지 파서에 태울 이유가 없다"
    assert resolved[0].endswith("a.md")


def test_문서_파싱_실패는_준비_실패로_보고한다(tmp_path, monkeypatch):
    from core.skills import tools as skill_tools

    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("core.storage.storage.download_file", _stub_downloader(tmp_path, []))
    monkeypatch.setattr(skill_tools, "extract_document_structure", _Parser("ERROR: unsupported"))

    resolved, failed = prv._materialize_inputs(tmp_path / "run", {"eval_name": "c1", "files": ["files/a.docx"]})

    assert resolved == []
    assert failed == ["files/a.docx"]


def test_입력_문서_준비_실패는_숨기지_않는다(tmp_path, monkeypatch):
    from core.skills import tools as skill_tools

    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", tmp_path)

    class _DL:
        def invoke(self, _args):
            return "ERROR: not found"

    monkeypatch.setattr("core.storage.storage.download_file", _DL())

    resolved, failed = prv._materialize_inputs(tmp_path / "run", {"eval_name": "c1", "files": ["files/x.docx"]})

    assert resolved == []
    assert failed == ["files/x.docx"]


def test_실행_지시문에_실제_파일_경로를_싣는다():
    inst = prv._runner_instruction(
        skill_path="/workspace/.pr-verify/r1/versions/head",
        case={"eval_name": "c1", "prompt": "첨부한 NDA 를 검토해줘"},
        outputs_dir="/workspace/out",
        variant="head",
        input_paths=["/workspace/.pr-verify/r1/inputs/c1/nda.docx"],
    )

    assert "/workspace/.pr-verify/r1/inputs/c1/nda.docx" in inst
    assert "already on disk" in inst


def test_첨부가_없는_시나리오는_없다고_명시한다():
    inst = prv._runner_instruction(
        skill_path="none",
        case={"eval_name": "c1", "prompt": "요약해줘"},
        outputs_dir="/workspace/out",
        variant="base",
        input_paths=[],
    )

    assert "no input files" in inst
