"""core/skills/eval_tools.py — 스킬 생성 시 "주요 시나리오" 확보 게이트.

## 왜 있는가

이 프로젝트가 필요로 하는 것은 **병합 요청을 병합하면 기존 동작이 깨지는지 병합 전에
확인하는 것**이다(core/skills/pr_verification.py). 그 검증은 "변경 전(base) 버전과
변경 후(head) 버전을 같은 시나리오로 돌려 비교" 하는 방식이라, **비교에 쓸 시나리오가
리소스마다 미리 확보돼 있어야** 성립한다.

그런데 딥 에이전트 채팅에서 스킬을 만들 때, 초안을 쓴 뒤 `test-runner` / `evaluator`
서브에이전트로 시나리오를 돌리는 단계가 실제로는 **한 번도 수행되지 않았다**
(전체 대화 기록에서 `task(subagent_type="test-runner")` 호출 0건). 루트 프롬프트가
SKILL.md 작성을 종료 상태로 규정하고 있었기 때문이다. 그 결과 어떤 스킬에도 시나리오가
남지 않았고, 병합 전 검증 자체가 불가능했다.

프롬프트만으로는 이 단계가 지켜지지 않는다는 것이 이미 증명됐으므로, 여기서는
**완료 자체를 도구로 막는다** — `complete_process_generation` 이 검증을 통과하지 못한
프로세스의 완료를 거부하는 것과 같은 방식이다.

## 온디스크 규약

    <base>/skills/<skill-name>/SKILL.md
    <base>/skills/<skill-name>/evals/evals.json          ← 시나리오(스킬과 함께 저장됨)
    <base>/.skill-evals/<skill-name>/iteration-<N>/
        <eval-name>/with_skill/outputs/…
        <eval-name>/with_skill/grading.json
        <eval-name>/without_skill/outputs/…
        <eval-name>/without_skill/grading.json

실행 산출물을 스킬 폴더 **밖**에 두는 이유: 프론트가 `skills/<name>/` 전체를 zip 으로
묶어 스킬로 저장하기 때문에, 반복 실행 산출물이 스킬 패키지에 섞이면 안 된다.
반대로 `evals/evals.json` 은 스킬과 함께 버전 관리되어야 하므로 안쪽에 둔다.

## DB

시나리오와 결과는 `resource_eval_suites / _cases / _runs / _results` 에 영속화된다
(마이그레이션: process-gpt-vue3 `20260904000001_resource_pr_verification.sql`).
테이블은 스킬·BPMN·DMN 공용이며, 이 모듈은 그중 `resource_type='skill'` 의
생성 시점(`trigger='creation'`) 기록만 담당한다. 병합 요청 검증
(`trigger='pull_request'`, variant base/head)은 pr_verification 모듈이 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from . import tools as _skill_tools
from .tools import (
    _bpmn_work_dir,
    _current_conversation_id,
    _current_tenant_id,
    _current_user_id,
    _resolve_ws_path,
)

logger = logging.getLogger(__name__)

# 실행 산출물 루트(스킬 폴더 바깥). 방 디렉터리 아래에 둔다.
EVAL_RUNS_SUBDIR = ".skill-evals"

# 스킬 하나를 "테스트했다" 고 인정하기 위한 최소 시나리오 수.
MIN_EVAL_CASES = 2

# 각 시나리오가 가져야 할 최소 assertion 수.
MIN_ASSERTIONS = 2

# 생성 시점 기준선 실행의 variant. 병합 요청 검증은 base/head 를 쓴다(pr_verification).
VARIANTS = ("with_skill", "without_skill")

# 생성 시점 대조군(스킬 없이 돌린 실행).
_BASELINE_VARIANTS = ("without_skill",)


# ---------------------------------------------------------------------------
# 경로 헬퍼
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """스킬 폴더명으로 안전한 kebab-case 슬러그(ASCII)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-.")
    return slug or "skill"


# 파일시스템에서 디렉터리명으로 쓸 수 없는 문자(+ 제어문자)만 걷어낸다.
_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _eval_slug(name: str, position: int) -> str:
    """시나리오 이름 → 디렉터리명.

    ASCII 로만 좁히면 한국어 이름("한국어 회의록에서 …")이 통째로 빈 문자열이 되어
    모든 케이스가 같은 폴더/같은 DB 키로 뭉개진다(실제로 그렇게 덮어써졌다).
    경로에 못 쓰는 문자만 제거하고 유니코드는 살린다.
    """
    slug = _UNSAFE_PATH_CHARS.sub("-", str(name or ""))
    slug = re.sub(r"\s+", "-", slug).strip("-. ")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80] or f"eval-{position + 1}"


def eval_runs_root(skill_name: str) -> Path:
    """이 대화방의 `<base>/.skill-evals/<skill-name>/` (host 경로)."""
    return _bpmn_work_dir() / EVAL_RUNS_SUBDIR / _safe_name(skill_name)


def eval_runs_container_root(skill_name: str) -> str:
    """에이전트에게 알려줄 컨테이너 기준 실행 산출물 경로."""
    host = eval_runs_root(skill_name)
    try:
        # 모듈 속성으로 읽는다 — 테스트가 tools.WORKSPACE_ROOT 를 갈아끼울 수 있어야 한다.
        rel = host.relative_to(_skill_tools.WORKSPACE_ROOT)
    except ValueError:
        return str(host)
    return f"/workspace/{rel.as_posix()}"


def _resolve_skill_dir(skill_path: str) -> Path:
    """도구 인자로 받은 스킬 경로(SKILL.md 든 폴더든)를 스킬 루트 디렉터리로 정규화."""
    p = _resolve_ws_path(skill_path)
    if p.name.upper() == "SKILL.MD":
        return p.parent
    return p


def _latest_iteration(runs_root: Path) -> int:
    """가장 큰 번호의 iteration-N 을 찾는다. 없으면 0."""
    best = 0
    if runs_root.is_dir():
        for child in runs_root.iterdir():
            m = re.fullmatch(r"iteration-(\d+)", child.name)
            if child.is_dir() and m:
                best = max(best, int(m.group(1)))
    return best


def next_iteration(skill_name: str) -> int:
    """다음에 쓸 iteration 번호."""
    return _latest_iteration(eval_runs_root(skill_name)) + 1


# ---------------------------------------------------------------------------
# evals.json / grading.json 읽기
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_eval_cases(skill_dir: Path) -> tuple[list[dict], str]:
    """`<skill>/evals/evals.json` 에서 시나리오 목록을 읽는다.

    Returns:
        (cases, error). error 가 비어 있지 않으면 cases 는 신뢰할 수 없다.
    """
    evals_file = skill_dir / "evals" / "evals.json"
    if not evals_file.is_file():
        return [], f"{evals_file.name} 이 없습니다 ({evals_file})."

    data = _read_json(evals_file)
    if data is None:
        return [], f"{evals_file} 를 JSON 으로 읽을 수 없습니다."

    raw = data.get("evals") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return [], f"{evals_file} 의 `evals` 배열이 비어 있습니다."

    cases: list[dict] = []
    used: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("eval_name") or item.get("id") or "")
        slug = _eval_slug(name, idx)
        # 같은 이름이 둘 이상이면 뒤에 번호를 붙인다 — 이름이 겹치면 두 시나리오가
        # 같은 디렉터리를 공유해 한쪽 결과가 덮어써지고 DB 키도 충돌한다.
        if slug in used:
            n = 2
            while f"{slug}-{n}" in used:
                n += 1
            slug = f"{slug}-{n}"
        used.add(slug)
        assertions = item.get("assertions") or []
        if isinstance(assertions, str):
            assertions = [assertions]
        cases.append({
            "eval_name": slug,
            "prompt": str(item.get("prompt") or "").strip(),
            "expected_output": str(item.get("expected_output") or "").strip() or None,
            "files": list(item.get("files") or []),
            "assertions": [str(a) for a in assertions if str(a).strip()],
            "position": idx,
        })
    return cases, ""


def _grading_summary(grading: dict) -> dict:
    """grading.json → {passed,total,pass_rate,assertions} 로 정규화.

    grader.md 가 규정한 형식은 `expectations: [{text, passed, evidence}]` 다.
    summary 블록이 있으면 그 수치를 우선 신뢰하되, 없으면 직접 센다.
    """
    exps = grading.get("expectations")
    if not isinstance(exps, list):
        exps = []
    norm = [
        {
            "text": str(e.get("text") or ""),
            "passed": bool(e.get("passed")),
            "evidence": str(e.get("evidence") or ""),
        }
        for e in exps
        if isinstance(e, dict)
    ]
    total = len(norm)
    passed = sum(1 for e in norm if e["passed"])

    summary = grading.get("summary")
    if isinstance(summary, dict):
        try:
            total = int(summary.get("total", total))
            passed = int(summary.get("passed", passed))
        except (TypeError, ValueError):
            pass

    return {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else None,
        "assertions": norm,
    }


def collect_gradings_at(
    root: Path,
    cases: list[dict],
    variants: tuple[str, ...] = VARIANTS,
    baseline_variants: tuple[str, ...] = _BASELINE_VARIANTS,
    primary_variant: str = "with_skill",
) -> tuple[list[dict], list[dict]]:
    """실행 루트 디렉터리에서 케이스별 채점 결과를 모은다.

    생성 시점(with_skill/without_skill)과 병합 요청 검증(head/base)이 같은 디렉터리
    규약을 쓰므로 variant 이름만 바꿔 그대로 재사용한다.

    Returns:
        (results, missing) — results 는 DB 에 넣을 행 목록,
        missing 은 아직 채점 결과가 없는 {eval_name, variant, expected_path} 목록.
    """
    results: list[dict] = []
    missing: list[dict] = []

    for case in cases:
        eval_dir = root / case["eval_name"]
        found_baseline = False
        for variant in variants:
            grading_file = eval_dir / variant / "grading.json"
            if not grading_file.is_file():
                if variant == primary_variant:
                    missing.append({
                        "eval_name": case["eval_name"],
                        "variant": variant,
                        "expected_path": str(grading_file),
                    })
                continue
            grading = _read_json(grading_file)
            if not isinstance(grading, dict):
                missing.append({
                    "eval_name": case["eval_name"],
                    "variant": variant,
                    "expected_path": str(grading_file),
                    "reason": "JSON 파싱 실패",
                })
                continue
            if variant in baseline_variants:
                found_baseline = True
            summary = _grading_summary(grading)
            results.append({
                "eval_name": case["eval_name"],
                "variant": variant,
                "outputs_dir": str(eval_dir / variant / "outputs"),
                **summary,
            })
        if not found_baseline:
            baseline = baseline_variants[0]
            missing.append({
                "eval_name": case["eval_name"],
                "variant": baseline,
                "expected_path": str(eval_dir / baseline / "grading.json"),
                "reason": f"대조군({baseline}) 채점 결과가 없습니다.",
            })

    return results, missing


def collect_gradings(skill_name: str, iteration: int, cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """생성 시점 실행(iteration-N)의 채점 결과를 모은다."""
    return collect_gradings_at(eval_runs_root(skill_name) / f"iteration-{iteration}", cases)


def _variant_pass_rate(results: list[dict], variants: tuple[str, ...]) -> float | None:
    """해당 variant 들의 assertion 을 전부 합산한 통과율."""
    passed = sum(r["passed"] for r in results if r["variant"] in variants)
    total = sum(r["total"] for r in results if r["variant"] in variants)
    return round(passed / total, 4) if total else None


def skill_digest(skill_dir: Path) -> str:
    """SKILL.md 본문의 sha256 — 같은 내용이면 같은 값(회귀 비교 기준)."""
    skill_md = skill_dir / "SKILL.md"
    try:
        return hashlib.sha256(skill_md.read_bytes()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 검증(게이트)
# ---------------------------------------------------------------------------

def validate_eval_artifacts(skill_dir: Path, iteration: int | None = None) -> dict:
    """스킬 폴더와 실행 산출물이 "테스트를 실제로 돌렸다" 는 조건을 만족하는지 본다.

    Returns:
        {"ok": bool, "problems": [...], "next_steps": [...], "cases": [...],
         "results": [...], "iteration": N, "skill_name": "..."}
    """
    skill_name = skill_dir.name
    problems: list[str] = []
    next_steps: list[str] = []

    if not (skill_dir / "SKILL.md").is_file():
        return {
            "ok": False,
            "skill_name": skill_name,
            "problems": [f"SKILL.md 가 없습니다 ({skill_dir}/SKILL.md)."],
            "next_steps": ["먼저 SKILL.md 를 write_file 로 작성하세요."],
            "cases": [],
            "results": [],
            "iteration": iteration or 0,
        }

    cases, err = load_eval_cases(skill_dir)
    if err:
        problems.append(err)
        next_steps.append(
            f"`{skill_dir}/evals/evals.json` 에 실제 사용자가 말할 법한 테스트 시나리오를 "
            f"{MIN_EVAL_CASES}건 이상 작성하세요. 각 항목은 "
            '{"id","name","prompt","expected_output","files","assertions"} 를 가집니다.'
        )
        return {
            "ok": False,
            "skill_name": skill_name,
            "problems": problems,
            "next_steps": next_steps,
            "cases": [],
            "results": [],
            "iteration": iteration or 0,
        }

    if len(cases) < MIN_EVAL_CASES:
        problems.append(f"시나리오가 {len(cases)}건뿐입니다 (최소 {MIN_EVAL_CASES}건).")
        next_steps.append("evals.json 에 시나리오를 더 추가하세요.")

    for case in cases:
        if not case["prompt"]:
            problems.append(f"시나리오 '{case['eval_name']}' 에 prompt 가 없습니다.")
        if len(case["assertions"]) < MIN_ASSERTIONS:
            problems.append(
                f"시나리오 '{case['eval_name']}' 의 assertions 가 "
                f"{len(case['assertions'])}건뿐입니다 (최소 {MIN_ASSERTIONS}건)."
            )
            next_steps.append(
                f"'{case['eval_name']}' 에 객관적으로 검증 가능한 assertion 을 추가하세요."
            )

    if iteration is None:
        iteration = _latest_iteration(eval_runs_root(skill_name)) or 1

    results, missing = collect_gradings(skill_name, iteration, cases)
    if missing:
        runs_root = eval_runs_container_root(skill_name)
        problems.append(
            f"iteration-{iteration} 에 채점 결과(grading.json)가 없는 실행이 "
            f"{len(missing)}건 있습니다."
        )
        next_steps.append(
            "아직 실행하지 않은 시나리오가 있습니다. 각 시나리오마다 "
            "`task(subagent_type=\"test-runner\")` 를 **with_skill 과 without_skill 두 건 모두, "
            "같은 턴에 한꺼번에** 띄우세요. outputs_dir 은 "
            f"`{runs_root}/iteration-{iteration}/<eval-name>/<variant>/outputs/` 입니다. "
            "실행이 끝나면 각 시나리오마다 `task(subagent_type=\"evaluator\")` 로 채점해 "
            "같은 디렉터리의 `../grading.json` 을 만들게 하세요."
        )

    return {
        "ok": not problems,
        "skill_name": skill_name,
        "problems": problems,
        "next_steps": next_steps,
        "missing": missing,
        "cases": cases,
        "results": results,
        "iteration": iteration,
    }


# ---------------------------------------------------------------------------
# DB 영속화
# ---------------------------------------------------------------------------

def _sb():
    from ..storage.db import get_supabase
    return get_supabase()


RESOURCE_TYPE_SKILL = "skill"


def upsert_suite(
    tenant_id: str,
    resource_id: str,
    resource_type: str = RESOURCE_TYPE_SKILL,
) -> str:
    """스위트 행을 확보하고 id 를 돌려준다."""
    sb = _sb()
    existing = (
        sb.table("resource_eval_suites")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("resource_type", resource_type)
        .eq("resource_id", resource_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"]

    payload = {
        "tenant_id": tenant_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "source_conversation_id": _current_conversation_id.get() or None,
        "created_by": _current_user_id.get() or None,
    }
    inserted = sb.table("resource_eval_suites").insert(payload).execute()
    return inserted.data[0]["id"]


def find_suite(
    tenant_id: str,
    resource_id: str,
    resource_type: str = RESOURCE_TYPE_SKILL,
) -> str:
    """스위트 id 를 찾는다. 없으면 빈 문자열(=검증할 시나리오가 아직 없음)."""
    rows = (
        _sb()
        .table("resource_eval_suites")
        .select("id")
        .eq("tenant_id", tenant_id)
        .eq("resource_type", resource_type)
        .eq("resource_id", resource_id)
        .limit(1)
        .execute()
    )
    return (rows.data or [{}])[0].get("id", "")


def load_suite_cases(suite_id: str) -> list[dict]:
    """스위트에 보관된 시나리오를 position 순으로 읽는다."""
    rows = (
        _sb()
        .table("resource_eval_cases")
        .select("id,eval_name,prompt,expected_output,files,assertions,position")
        .eq("suite_id", suite_id)
        .order("position")
        .execute()
    )
    return [
        {
            "id": r["id"],
            "eval_name": r["eval_name"],
            "prompt": r["prompt"] or "",
            "expected_output": r.get("expected_output"),
            "files": r.get("files") or [],
            "assertions": r.get("assertions") or [],
            "position": r.get("position") or 0,
        }
        for r in (rows.data or [])
    ]


def sync_cases(tenant_id: str, suite_id: str, cases: list[dict]) -> dict[str, str]:
    """시나리오를 스위트에 반영하고 {eval_name: case_id} 를 돌려준다.

    같은 이름의 케이스는 최신 내용으로 갱신한다 — 리소스를 고치면서 시나리오도 함께
    다듬는 흐름이 정상이고, 병합 전후 비교는 케이스 이름 기준으로 이뤄지기 때문이다.
    """
    sb = _sb()
    existing = (
        sb.table("resource_eval_cases")
        .select("id,eval_name")
        .eq("suite_id", suite_id)
        .execute()
    )
    by_name = {row["eval_name"]: row["id"] for row in (existing.data or [])}

    ids: dict[str, str] = {}
    for case in cases:
        payload = {
            "suite_id": suite_id,
            "tenant_id": tenant_id,
            "eval_name": case["eval_name"],
            "prompt": case["prompt"],
            "expected_output": case["expected_output"],
            "files": case["files"],
            "assertions": case["assertions"],
            "position": case["position"],
        }
        case_id = by_name.get(case["eval_name"])
        if case_id:
            sb.table("resource_eval_cases").update(payload).eq("id", case_id).execute()
            ids[case["eval_name"]] = case_id
        else:
            row = sb.table("resource_eval_cases").insert(payload).execute()
            new_id = row.data[0]["id"]
            by_name[case["eval_name"]] = new_id
            ids[case["eval_name"]] = new_id
    return ids


def insert_results(
    *,
    run_id: str,
    tenant_id: str,
    results: list[dict],
    case_ids: dict[str, str],
) -> None:
    """회차의 variant 별 채점 결과를 기록한다."""
    rows = [
        {
            "run_id": run_id,
            "case_id": case_ids.get(r["eval_name"]),
            "tenant_id": tenant_id,
            "eval_name": r["eval_name"],
            "variant": r["variant"],
            "passed": r["passed"],
            "total": r["total"],
            "pass_rate": r["pass_rate"],
            "assertions": r["assertions"],
            "outputs_dir": r.get("outputs_dir"),
        }
        for r in results
    ]
    if rows:
        _sb().table("resource_eval_results").insert(rows).execute()


def persist_creation_run(
    *,
    tenant_id: str,
    skill_name: str,
    skill_dir: Path,
    cases: list[dict],
    results: list[dict],
) -> dict:
    """스킬 생성 시점의 시나리오 + 기준선 결과를 기록한다.

    여기서 보관된 시나리오가 이후 모든 병합 요청 검증(base/head 비교)의 입력이 된다.
    """
    suite_id = upsert_suite(tenant_id, skill_name)
    case_ids = sync_cases(tenant_id, suite_id, cases)

    pass_rate = _variant_pass_rate(results, ("with_skill",))
    baseline_rate = _variant_pass_rate(results, _BASELINE_VARIANTS)

    per_case: dict[str, dict] = {}
    for r in results:
        per_case.setdefault(r["eval_name"], {})[r["variant"]] = {
            "passed": r["passed"],
            "total": r["total"],
            "pass_rate": r["pass_rate"],
        }

    run_payload = {
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "resource_type": RESOURCE_TYPE_SKILL,
        "resource_id": skill_name,
        "trigger": "creation",
        "head_digest": skill_digest(skill_dir),
        "status": "succeeded",
        "pass_rate": pass_rate,
        "base_pass_rate": baseline_rate,
        "summary": {"per_case": per_case, "conversation_id": _current_conversation_id.get() or None},
        "created_by": _current_user_id.get() or None,
        "finished_at": _now_iso(),
    }
    run_row = _sb().table("resource_eval_runs").insert(run_payload).execute()
    run_id = run_row.data[0]["id"]
    insert_results(run_id=run_id, tenant_id=tenant_id, results=results, case_ids=case_ids)

    return {
        "suite_id": suite_id,
        "run_id": run_id,
        "trigger": "creation",
        "pass_rate": pass_rate,
        "baseline_pass_rate": baseline_rate,
        "per_case": per_case,
        "case_count": len(cases),
    }


def _now_iso() -> str:
    from ..storage.db import now_iso
    return now_iso()


# ---------------------------------------------------------------------------
# 도구
# ---------------------------------------------------------------------------

@tool
def plan_skill_eval(skill_path: str) -> str:
    """**스킬 테스트 3단계 중 1단계.** SKILL.md 초안과 `evals/evals.json` 을 쓴 뒤 호출한다.

    이번 회차의 iteration 번호와, 각 시나리오를 어느 경로에 실행해야 하는지
    (with_skill / without_skill 각각의 outputs_dir)를 계산해 돌려준다.
    여기서 받은 경로를 그대로 `task(subagent_type="test-runner")` 의 outputs_dir 로 넘겨야
    이후 `complete_skill_creation` 이 채점 결과를 찾을 수 있다.

    Args:
        skill_path: 스킬 폴더 또는 그 안의 SKILL.md 경로
                    (예: /workspace/.bpmn/<room>/skills/nda-review).

    Returns:
        {"iteration", "skill_name", "runs", "evaluator_grading_paths"} JSON.
        실패 시 "ERROR: ...".
    """
    skill_dir = _resolve_skill_dir(skill_path)
    if not (skill_dir / "SKILL.md").is_file():
        return f"ERROR: SKILL.md 를 찾을 수 없습니다: {skill_dir}/SKILL.md"

    cases, err = load_eval_cases(skill_dir)
    if err:
        return (
            f"ERROR: {err} 먼저 `{skill_dir}/evals/evals.json` 에 시나리오를 "
            f"{MIN_EVAL_CASES}건 이상 쓰세요. 각 항목은 "
            '{"id","name","prompt","expected_output","files","assertions"} 형태이며 '
            f"assertions 는 {MIN_ASSERTIONS}건 이상이어야 합니다."
        )

    skill_name = skill_dir.name

    # 이미 돌린 회차의 채점이 지금 evals.json 기준으로 모두 갖춰져 있으면 재실행이 낭비다.
    # (DB 저장 실패 등으로 complete 이 한 번 튕겼을 때 에이전트가 전체 실행을 처음부터
    #  다시 돌리다 재귀 한도에 걸리던 문제.)
    latest = _latest_iteration(eval_runs_root(skill_name))
    if latest:
        _, missing = collect_gradings(skill_name, latest, cases)
        if not missing:
            return json.dumps({
                "skill_name": skill_name,
                "iteration": latest,
                "runs": [],
                "already_complete": True,
                "next": (
                    f"iteration-{latest} 의 채점 결과가 이미 모두 있습니다. 다시 실행하지 말고 "
                    f"`complete_skill_creation(\"{skill_dir}\")` 를 바로 호출하세요."
                ),
            }, ensure_ascii=False, indent=2)

    iteration = latest + 1
    root = f"{eval_runs_container_root(skill_name)}/iteration-{iteration}"

    runs = []
    grading_paths = []
    for case in cases:
        for variant in ("with_skill", "without_skill"):
            runs.append({
                "eval_name": case["eval_name"],
                "variant": variant,
                "prompt": case["prompt"],
                "files": case["files"],
                "skill_path": str(skill_dir) if variant == "with_skill" else "none",
                "outputs_dir": f"{root}/{case['eval_name']}/{variant}/outputs",
            })
        grading_paths.append({
            "eval_name": case["eval_name"],
            "assertions": case["assertions"],
            "with_skill_grading_path": f"{root}/{case['eval_name']}/with_skill/grading.json",
            "without_skill_grading_path": f"{root}/{case['eval_name']}/without_skill/grading.json",
        })

    return json.dumps({
        "skill_name": skill_name,
        "iteration": iteration,
        "runs": runs,
        "evaluator_grading_paths": grading_paths,
        "next": (
            f"위 runs {len(runs)}건을 **모두 같은 턴에** task(subagent_type='test-runner') 로 띄우고, "
            "끝나면 각 시나리오마다 task(subagent_type='evaluator') 로 채점한 뒤 "
            "complete_skill_creation 을 호출하세요."
        ),
    }, ensure_ascii=False, indent=2)


@tool
def complete_skill_creation(skill_path: str) -> str:
    """**스킬 테스트 3단계 중 3단계 — 스킬 생성 완료 게이트.**

    테스트가 실제로 수행됐는지 확인하고, 통과했을 때만 완료로 인정한다.
    사용자에게 "스킬을 생성했어요" 라고 답하기 전에 **반드시** 이 도구가 성공해야 한다.

    확인하는 것:
      - `<skill>/SKILL.md` 존재
      - `<skill>/evals/evals.json` 에 시나리오 2건 이상, 각 assertion 2건 이상
      - 각 시나리오마다 with_skill 과 대조군(without_skill)의 `grading.json` 이 모두 존재

    통과하면 시나리오와 채점 결과를 DB(resource_eval_suites/_cases/_runs/_results)에
    저장한다. **여기 보관된 시나리오가, 나중에 이 스킬의 병합 요청을 검토할 때
    "변경 전 vs 변경 후" 를 비교하는 검증의 입력이 된다** — 그래서 이 단계를 건너뛴
    스킬은 병합 전에 기존 동작이 깨지는지 확인할 수단이 없다.

    Args:
        skill_path: 스킬 폴더 또는 SKILL.md 경로.

    Returns:
        성공 시 실행 요약 JSON. 실패 시 "ERROR: ..." + 무엇을 더 해야 하는지.
    """
    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        return "ERROR: tenant_id 가 설정되지 않았습니다. ask_user 를 호출하지 말고 중단하세요."

    skill_dir = _resolve_skill_dir(skill_path)
    report = validate_eval_artifacts(skill_dir)

    if not report["ok"]:
        return "ERROR: " + json.dumps({
            "message": (
                "테스트를 마치지 않아 완료할 수 없습니다. "
                "사용자에게 완료를 알리지 말고 아래 남은 일을 먼저 끝내세요."
            ),
            "skill": str(skill_dir),
            "problems": report["problems"],
            "next_steps": report["next_steps"],
            "missing_runs": report.get("missing", []),
        }, ensure_ascii=False, indent=2)

    try:
        summary = persist_creation_run(
            tenant_id=tenant_id,
            skill_name=report["skill_name"],
            skill_dir=skill_dir,
            cases=report["cases"],
            results=report["results"],
        )
    except Exception as e:
        logger.exception("스킬 평가 결과 저장 실패 skill=%s", report["skill_name"])
        return f"ERROR: 평가 결과 DB 저장에 실패했습니다: {e}"

    summary["message"] = (
        "테스트 완료. 이 시나리오는 이후 이 스킬의 병합 요청을 검토할 때 "
        "변경 전/후 비교에 재사용됩니다. 이제 사용자에게 스킬 생성 완료를 알려도 됩니다."
    )
    return json.dumps(summary, ensure_ascii=False, indent=2)


def get_skill_eval_tools() -> list:
    """스킬 생성 시 시나리오를 확보하기 위한 도구 목록.

    병합 요청 검증은 에이전트가 아니라 서버가 결정론적으로 오케스트레이션하므로
    (core/skills/pr_verification.py) 여기에 도구로 노출하지 않는다.
    """
    return [plan_skill_eval, complete_skill_creation]
