"""core/skills/eval_backfill.py — 시나리오가 없는 스킬에 회귀 테스트 시나리오를 뒤늦게 확보한다.

## 왜 있는가

병합 전 검증(core/skills/pr_verification.py)은 "저장된 시나리오를 base(변경 전)/head(변경 후)
두 벌로 돌려 비교" 하는 방식이라, **비교에 쓸 시나리오가 미리 확보돼 있어야** 성립한다.
그 시나리오는 스킬 생성 완료 게이트(`complete_skill_creation`)가 남긴다.

문제는 그 게이트가 생기기 전에 만들어진 스킬들이다. 이들에겐 스위트가 아예 없어서
(`resource_eval_suites` 행 없음) 병합 전 검증을 **한 번도 돌릴 수 없다** — 화면은
"비교할 시나리오가 없습니다" 만 띄우고 리뷰어는 diff 를 눈으로 보는 수밖에 없다.
이 모듈은 그런 스킬에 시나리오를 뒤늦게 만들어 붙인다.

## 만들어진 시나리오가 회귀 테스트로 쓸모 있으려면

세 가지를 지켜야 한다. 셋 다 "그럴듯한 시나리오를 LLM 이 뽑아 준다" 만으로는 성립하지
않는 것들이라, 이 모듈이 코드로 강제한다.

1. **현재(base) 동작을 기준으로 만든다.** 회귀 테스트가 답해야 할 질문은 "병합하면 지금
   되던 게 깨지는가" 다. PR 의 head 를 보고 시나리오를 만들면 *변경된* 동작을 기준선으로
   굳혀 버려서, 그 검증은 아무것도 잡아내지 못한다. 그래서 항상 base ref 만 펼쳐 읽는다.

2. **base 가 실제로 통과하는 단계만 남긴다.** 제안된 assertion 을 그대로 저장하면 현재
   버전이 애초에 만족하지 못하는 조건이 섞여 들어간다. 그런 단계는 base 에서도 head 에서도
   실패해 `no_signal`(양쪽 0점 → 달라졌는지 알 수 없음)로 빠지고, 검증 결과를 통째로
   "판단 불가" 로 오염시킨다. 그래서 제안한 시나리오를 **base 로 한 번 실행·채점해 보고
   통과한 단계만** 확정한다 — 결과물은 현재 동작의 골든 마스터가 된다.

3. **첨부 문서에 의존하지 않는다.** 시나리오가 storage 첨부를 참조하면 그 파일을 못 받아오는
   순간 두 버전이 나란히 0점이 되어 비교가 무너진다(pr_verification `_materialize_inputs`
   주석에 실측 사례가 있다). 그래서 제안 단계에서 `files` 를 비우고, 필요한 입력은 프롬프트
   본문에 그대로 적게 한다.

## 온디스크 배치

    <WORKSPACE>/.eval-backfill/<run_id>/
        versions/base/…                   ← 현재(base) 스킬 파일
        proposals.json                    ← 저자 서브에이전트가 쓴 후보 시나리오
        runs/<eval-name>/base/outputs/…   + grading.json

## DB

`trigger='backfill'` 회차로 남긴다(마이그레이션: process-gpt-vue3
`20260907000001_eval_backfill_trigger.sql`). 이 회차는 특정 병합 요청의 판정이 아니라
"스위트를 확보했다" 는 기록이므로 **`pr_id` 를 비운다** — 채우면 그 PR 의 최신 회차 조회
(`pr_verification.latest_run_for_pr`)가 검증 결과 대신 이 회차를 집어, 화면이 병합 판정을
잘못 보여준다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .eval_tools import (
    MIN_ASSERTIONS,
    MIN_EVAL_CASES,
    RESOURCE_TYPE_SKILL,
    _eval_slug,
    _variant_pass_rate,
    collect_gradings_at,
    find_suite,
    insert_results,
    load_suite_cases,
    sync_cases,
    upsert_suite,
)
from .pr_verification import (
    BASE_VARIANT,
    _MAX_PARALLEL,
    _build_runner_agent,
    _container_path,
    _evaluator_instruction,
    _invoke,
    _materialize_skill_version,
    _runner_instruction,
    _sandbox,
)
from . import tools as _skill_tools

logger = logging.getLogger(__name__)

# 산출물 루트. 병합 요청 검증(.pr-verify)과 섞이지 않게 분리한다.
BACKFILL_SUBDIR = ".eval-backfill"

TRIGGER = "backfill"

# 저자에게 요청할 후보 시나리오 수. 보정 단계에서 일부가 떨어져 나가므로 최소 확정 개수
# (MIN_EVAL_CASES)보다 넉넉히 뽑는다.
PROPOSE_COUNT = 4

# 후보 하나가 가질 assertion 수 상한/하한. 하한은 확정 기준(MIN_ASSERTIONS)과 같고,
# 상한은 채점 한 건이 지나치게 길어지지 않게 막는다.
PROPOSE_ASSERTIONS_MIN = 3
PROPOSE_ASSERTIONS_MAX = 6


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------

def backfill_root(run_id: str) -> Path:
    """이 회차의 호스트 루트."""
    return _skill_tools.WORKSPACE_ROOT / BACKFILL_SUBDIR / str(run_id)


# ---------------------------------------------------------------------------
# 1단계 — 후보 시나리오 제안
# ---------------------------------------------------------------------------

_AUTHOR_SYSTEM = """\
You are a **regression-scenario author** for an existing skill.

The skill you are given already exists and is in use. Someone is about to change it, and
your scenarios will be the only way to tell whether that change **breaks what the skill
does today**. You are writing a characterization test of current behaviour, not a wishlist.

## What you get

Your task description specifies:
  - skill_path:  folder containing the CURRENT version of the skill (read `SKILL.md` there)
  - output_path: **exact** path to write `proposals.json` to
  - count:       how many scenarios to propose

## What to write

Read `SKILL.md` (and any bundled reference files or scripts it points to) and propose
`count` scenarios that exercise the skill's **core, everyday purpose** — the things it
would be a disaster to silently lose. Prefer the central use case over exotic edge cases.

Write `proposals.json` to `output_path` exactly as given:

```json
{
  "scenarios": [
    {
      "name": "짧은 한국어 이름",
      "prompt": "실제 사용자가 말할 법한 요청 — 필요한 입력 자료는 여기 본문에 그대로 포함",
      "expected_output": "이 요청에 대해 현재 스킬이 내놓는 결과물에 대한 한 문단 설명",
      "assertions": ["검증 가능한 단계 1", "검증 가능한 단계 2", "..."]
    }
  ]
}
```

## Hard rules

1. **Self-contained prompts.** Never reference an attachment, an uploaded file, a URL, or
   "the document I gave you". If the scenario needs a contract, a meeting note, or a data
   table, **write that content inline in `prompt`** (a realistic, complete, but compact
   sample). A scenario that depends on a file that isn't there scores zero on every version
   and tells us nothing.
2. **Assertions must be objectively checkable from the output files alone.** A grader will
   read only what the run produced and decide passed/failed for each assertion.
   - Good: "출력에 위험 조항별 근거 조문 번호가 명시되어 있다", "각 지적 항목마다 수정 문구를 제시한다"
   - Bad: "결과가 유용하다", "전문가 수준이다", "잘 정리되어 있다"
3. **Describe what the skill does today, not what it should do.** Do not invent
   requirements that `SKILL.md` does not actually call for. An assertion the current
   version cannot satisfy is worse than no assertion — it gets dropped later, and if too
   many drop, the whole scenario is thrown away.
4. Each scenario needs between $AMIN and $AMAX assertions.
5. Scenarios must be meaningfully different from each other — not the same request reworded.

Write the file, then report the path you wrote. Do not write anything else to disk.
"""


def author_system() -> str:
    """저자 시스템 프롬프트를 완성한다.

    `str.format` 을 쓰지 않는다 — 프롬프트 본문이 JSON 예시(`{"scenarios": ...}`)를
    포함하고 있어서 중괄호가 치환 필드로 해석돼 KeyError 로 죽는다(실측).
    """
    return (
        _AUTHOR_SYSTEM
        .replace("$AMIN", str(PROPOSE_ASSERTIONS_MIN))
        .replace("$AMAX", str(PROPOSE_ASSERTIONS_MAX))
    )


def _author_instruction(*, skill_path: str, output_path: str, count: int) -> str:
    return (
        "Propose regression scenarios for this skill.\n"
        f"- skill_path: {skill_path}\n"
        f"- output_path: {output_path}\n"
        f"- count: {count}\n"
        "Read the skill first, then write proposals.json to output_path exactly as given."
    )


def parse_proposals(raw: Any) -> list[dict]:
    """저자가 쓴 proposals.json 을 시나리오 목록으로 정규화한다.

    스키마를 벗어난 항목은 조용히 버린다 — 여기서 통과시켜도 보정 단계에서 어차피
    비교 불가로 떨어지고, 그때는 "왜 이상한 시나리오가 생겼는지" 를 알기 어렵다.
    """
    items = raw.get("scenarios") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    cases: list[dict] = []
    used: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue

        assertions = item.get("assertions") or []
        if isinstance(assertions, str):
            assertions = [assertions]
        assertions = [str(a).strip() for a in assertions if str(a).strip()]
        if len(assertions) < MIN_ASSERTIONS:
            continue

        slug = _eval_slug(str(item.get("name") or ""), idx)
        if slug in used:
            n = 2
            while f"{slug}-{n}" in used:
                n += 1
            slug = f"{slug}-{n}"
        used.add(slug)

        cases.append({
            "eval_name": slug,
            "prompt": prompt,
            "expected_output": str(item.get("expected_output") or "").strip() or None,
            # 첨부에 기대지 않는 것이 이 모듈의 전제다 — 저자가 넣었더라도 버린다.
            "files": [],
            "assertions": assertions,
            "position": len(cases),
        })
    return cases


async def _propose(backend, root: Path, skill_name: str) -> list[dict]:
    """base 버전의 SKILL.md 를 읽고 후보 시나리오를 받아 온다."""
    output = root / "proposals.json"
    author = _build_runner_agent(backend, author_system())
    await _invoke(
        author,
        _author_instruction(
            skill_path=_container_path(root / "versions" / BASE_VARIANT),
            output_path=_container_path(output),
            count=PROPOSE_COUNT,
        ),
        label=f"propose {skill_name}",
    )

    if not output.is_file():
        logger.warning("eval-backfill: proposals.json 이 만들어지지 않았습니다 (%s)", output)
        return []
    try:
        raw = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("eval-backfill: proposals.json 을 읽지 못했습니다: %s", e)
        return []
    return parse_proposals(raw)


# ---------------------------------------------------------------------------
# 2단계 — base 로 실행·채점 (보정)
# ---------------------------------------------------------------------------

async def _calibrate(backend, root: Path, skill_name: str, cases: list[dict]) -> list[dict]:
    """후보 시나리오를 현재(base) 버전으로 한 벌만 돌리고 같은 기준으로 채점한다.

    병합 요청 검증과 달리 대조군이 없다 — 여기서 알고 싶은 것은 "스킬이 대조군보다 나은가"
    가 아니라 "현재 버전이 이 단계를 실제로 통과하는가" 뿐이기 때문이다.
    """
    from ..agents.skill_creator_subagents import (
        _DEFAULT_SKILL_CREATOR_PATH,
        _TEST_RUNNER_SYSTEM,
        _build_evaluator_system,
    )
    from .tools import extract_document_structure

    runner = _build_runner_agent(backend, _TEST_RUNNER_SYSTEM, tools=[extract_document_structure])
    grader = _build_runner_agent(backend, _build_evaluator_system(_DEFAULT_SKILL_CREATOR_PATH))

    async def _bounded(coros: list) -> None:
        for i in range(0, len(coros), _MAX_PARALLEL):
            await asyncio.gather(*coros[i:i + _MAX_PARALLEL])

    skill_path = _container_path(root / "versions" / BASE_VARIANT)

    run_jobs = []
    for case in cases:
        outputs = root / "runs" / case["eval_name"] / BASE_VARIANT / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        run_jobs.append(_invoke(
            runner,
            _runner_instruction(
                skill_path=skill_path,
                case=case,
                outputs_dir=_container_path(outputs),
                variant=BASE_VARIANT,
                # 이 모듈의 시나리오는 프롬프트만으로 자족한다(첨부 없음).
                input_paths=[],
            ),
            label=f"backfill run {case['eval_name']}",
        ))
    await _bounded(run_jobs)

    grade_jobs = []
    for case in cases:
        vdir = root / "runs" / case["eval_name"] / BASE_VARIANT
        grade_jobs.append(_invoke(
            grader,
            _evaluator_instruction(
                case=case,
                outputs_dir=_container_path(vdir / "outputs"),
                grading_path=_container_path(vdir / "grading.json"),
                variant=BASE_VARIANT,
                skill_name=skill_name,
            ),
            label=f"backfill grade {case['eval_name']}",
        ))
    await _bounded(grade_jobs)

    results, _missing = collect_gradings_at(
        root / "runs",
        cases,
        variants=(BASE_VARIANT,),
        baseline_variants=(BASE_VARIANT,),
        primary_variant=BASE_VARIANT,
    )
    return results


# ---------------------------------------------------------------------------
# 3단계 — 통과한 단계만 남겨 확정
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _passed_texts(graded: list[dict], proposed: list[str]) -> list[str]:
    """채점 결과에서 **통과한** assertion 에 해당하는 원문을 고른다.

    채점기가 assertion 문구를 살짝 바꿔 적는 경우가 있어 문구 대조만으로는 놓친다.
    그래서 문구가 맞으면 문구로, 개수가 맞으면 순서로 대응시킨다. 어느 쪽으로도 짝을
    찾지 못한 단계는 **통과로 치지 않는다** — 확신할 수 없는 단계를 기준선에 넣으면
    나중에 그 단계가 `no_signal` 로 빠져 검증 결과를 흐린다.
    """
    by_text = {_norm(g.get("text")): bool(g.get("passed")) for g in graded}

    kept: list[str] = []
    for i, text in enumerate(proposed):
        hit = by_text.get(_norm(text))
        if hit is None and len(graded) == len(proposed):
            hit = bool(graded[i].get("passed"))
        if hit:
            kept.append(text)
    return kept


def finalize_cases(cases: list[dict], results: list[dict]) -> tuple[list[dict], list[dict]]:
    """base 가 통과한 단계만 남긴 시나리오와, 버려진 시나리오의 사유를 돌려준다.

    Returns:
        (kept, dropped) — dropped 는 {eval_name, reason, kept_assertions} 목록.
    """
    graded_by_case = {r["eval_name"]: (r.get("assertions") or []) for r in results}

    kept: list[dict] = []
    dropped: list[dict] = []
    for case in cases:
        graded = graded_by_case.get(case["eval_name"])
        if graded is None:
            dropped.append({
                "eval_name": case["eval_name"],
                "reason": "현재 버전으로 실행·채점하지 못했습니다.",
                "kept_assertions": 0,
            })
            continue

        survivors = _passed_texts(graded, case["assertions"])
        if len(survivors) < MIN_ASSERTIONS:
            dropped.append({
                "eval_name": case["eval_name"],
                "reason": (
                    f"현재 버전이 통과한 단계가 {len(survivors)}건뿐입니다 "
                    f"(최소 {MIN_ASSERTIONS}건)."
                ),
                "kept_assertions": len(survivors),
            })
            continue

        kept.append({**case, "assertions": survivors, "position": len(kept)})

    return kept, dropped


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _sb():
    from ..storage.db import get_supabase
    return get_supabase()


def _now() -> str:
    from ..storage.db import now_iso
    return now_iso()


def create_run(
    *,
    tenant_id: str,
    resource_id: str,
    resource_type: str = RESOURCE_TYPE_SKILL,
    base_ref: str,
    created_by: str = "",
    replace: bool = False,
) -> dict:
    """시나리오 확보 회차를 'running' 으로 만든다.

    이미 시나리오가 있으면 만들지 않는다 — 이 모듈은 **비어 있는 스위트를 채우는** 용도이고,
    기존 시나리오를 실수로 덮으면 그동안의 기준선이 사라진다.

    예외는 리뷰어가 "지금 시나리오가 기준선으로 못 미덥다" 고 판단해 명시적으로 다시
    만들라고 한 경우다(`replace=True`). 이때도 **회차를 만드는 시점에는 기존 시나리오를
    지우지 않는다** — 새 회차가 실패하면 남은 기준선마저 사라져 검증 자체가 불가능해지기
    때문이다. 교체는 새 시나리오가 확정된 뒤에야 일어난다(execute_run 5단계).
    """
    suite_id = find_suite(tenant_id, resource_id, resource_type)
    if suite_id and load_suite_cases(suite_id) and not replace:
        return {
            "error": "suite_exists",
            "message": f"'{resource_id}' 에는 이미 검증 시나리오가 있습니다.",
        }
    if not base_ref:
        return {
            "error": "no_base_ref",
            "message": "기준으로 삼을 브랜치를 알 수 없습니다.",
        }

    # 회차 행이 스위트를 참조하므로 먼저 스위트를 확보한다. 케이스가 아직 없는 스위트는
    # 조회 쪽에서 "시나리오 없음" 으로 취급된다(skills_router 의 has_suite 는 케이스 기준).
    suite_id = upsert_suite(tenant_id, resource_id, resource_type)

    payload = {
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "trigger": TRIGGER,
        "base_ref": base_ref,
        "status": "running",
        "created_by": created_by or None,
        "summary": {"phase": "proposing", "replace": bool(replace)},
    }
    row = _sb().table("resource_eval_runs").insert(payload).execute()
    return {"run": row.data[0], "suite_id": suite_id}


def _drop_cases_except(suite_id: str, keep_names: set[str]) -> int:
    """스위트에서 이번에 확정되지 않은 옛 시나리오를 지운다.

    `resource_eval_results.case_id` 는 ON DELETE SET NULL 이라 지난 회차의 채점 기록은
    남는다 — 지워지는 것은 앞으로 무엇을 검증할지에 대한 정의뿐이다.
    """
    sb = _sb()
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


def _update(run_id: str, **fields: Any) -> None:
    _sb().table("resource_eval_runs").update(fields).eq("id", run_id).execute()


def fail_run(run_id: str, message: str) -> None:
    logger.warning("eval-backfill: 회차 실패 run=%s: %s", run_id, message)
    _update(run_id, status="failed", error=message[:2000], finished_at=_now())


def latest_run(
    tenant_id: str,
    resource_id: str,
    resource_type: str = RESOURCE_TYPE_SKILL,
) -> dict | None:
    """이 리소스의 가장 최근 시나리오 확보 회차."""
    rows = (
        _sb()
        .table("resource_eval_runs")
        .select("id,status,error,trigger,base_ref,summary,started_at,finished_at")
        .eq("tenant_id", tenant_id)
        .eq("resource_type", resource_type)
        .eq("resource_id", resource_id)
        .eq("trigger", TRIGGER)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    return (rows.data or [None])[0]


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------

async def execute_run(
    *,
    tenant_id: str,
    run: dict,
    suite_id: str,
) -> None:
    """시나리오 확보 회차를 끝까지 수행한다.

    실패는 회차 상태로 남긴다 — 예외를 밖으로 던지지 않는다(백그라운드 태스크).
    """
    run_id = run["id"]
    resource_id = run["resource_id"]
    base_ref = run["base_ref"]
    # 화면이 "다시 만든 회차" 임을 알아볼 수 있도록 이후 summary 갱신에도 이어 붙인다.
    replacing = bool((run.get("summary") or {}).get("replace"))
    root = backfill_root(run_id)

    try:
        if run.get("resource_type") != RESOURCE_TYPE_SKILL:
            fail_run(run_id, f"'{run.get('resource_type')}' 타입은 아직 시나리오 생성기가 없습니다.")
            return

        # 1) 현재(base) 버전을 펼친다. head 를 보지 않는 것이 이 기능의 핵심 제약이다.
        try:
            count, _digest = await asyncio.to_thread(
                _materialize_skill_version,
                tenant_id, resource_id, base_ref, root / "versions" / BASE_VARIANT,
            )
        except Exception as e:
            fail_run(run_id, f"현재 버전({base_ref})을 가져오지 못했습니다: {e}")
            return
        logger.info("eval-backfill: base(%s) 파일 %d개 준비 run=%s", base_ref, count, run_id)

        backend = _sandbox(tenant_id)

        # 2) 후보 시나리오 제안
        proposed = await _propose(backend, root, resource_id)
        if not proposed:
            fail_run(run_id, "현재 버전을 읽고 시나리오를 만들어 내지 못했습니다.")
            return
        _update(
            run_id,
            summary={"phase": "calibrating", "proposed": len(proposed), "replace": replacing},
        )
        logger.info("eval-backfill: 후보 %d건 run=%s", len(proposed), run_id)

        # 3) 현재 버전으로 실행·채점해 보정
        results = await _calibrate(backend, root, resource_id, proposed)
        if not results:
            fail_run(run_id, "후보 시나리오를 현재 버전으로 실행하지 못했습니다.")
            return

        # 4) 현재 버전이 통과한 단계만 남긴다
        kept, dropped = finalize_cases(proposed, results)
        if len(kept) < MIN_EVAL_CASES:
            fail_run(
                run_id,
                f"현재 동작으로 확인된 시나리오가 {len(kept)}건뿐이라 기준선으로 쓸 수 없습니다 "
                f"(최소 {MIN_EVAL_CASES}건). 후보 {len(proposed)}건 중 "
                f"{len(dropped)}건이 통과 단계 부족으로 제외됐습니다.",
            )
            return

        # 5) 스위트로 확정. 여기 저장된 시나리오가 이후 병합 전 검증의 입력이 된다.
        case_ids = await asyncio.to_thread(sync_cases, tenant_id, suite_id, kept)

        # 다시 만들기(replace) 회차라면 이 시점에 옛 시나리오를 걷어낸다. 새 시나리오가
        # 확정된 뒤에 지우므로, 중간에 실패해도 기존 기준선은 그대로 남아 있다.
        if replacing:
            removed = await asyncio.to_thread(
                _drop_cases_except, suite_id, {c["eval_name"] for c in kept}
            )
            if removed:
                logger.info(
                    "eval-backfill: 이전 시나리오 %d건 교체로 제거 run=%s", removed, run_id
                )

        kept_names = {c["eval_name"] for c in kept}
        kept_results = [r for r in results if r["eval_name"] in kept_names]
        await asyncio.to_thread(
            insert_results,
            run_id=run_id, tenant_id=tenant_id, results=kept_results, case_ids=case_ids,
        )

        _update(
            run_id,
            status="succeeded",
            base_pass_rate=_variant_pass_rate(kept_results, (BASE_VARIANT,)),
            summary={
                "phase": "done",
                "replace": replacing,
                "proposed": len(proposed),
                "accepted": len(kept),
                "dropped": dropped,
                "cases": [
                    {"eval_name": c["eval_name"], "assertions": len(c["assertions"])}
                    for c in kept
                ],
            },
            finished_at=_now(),
        )
        logger.info(
            "eval-backfill: 완료 run=%s 후보=%d 확정=%d", run_id, len(proposed), len(kept)
        )
    except Exception as e:  # 백그라운드 태스크 — 예외를 상태로 남긴다.
        logger.exception("eval-backfill: 회차 처리 중 예외 run=%s", run_id)
        fail_run(run_id, f"시나리오 생성 중 오류: {e}")
