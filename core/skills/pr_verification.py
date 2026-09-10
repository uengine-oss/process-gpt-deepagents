"""core/skills/pr_verification.py — 병합 전 검증(pre-merge verification).

## 무엇을 하는가

"이 병합 요청을 병합하면 **기존 동작이 깨지는가**" 를 병합 **전에** 답한다.

리뷰어가 병합 요청 화면에서 검증을 실행하면:

  1. 그 리소스에 보관된 주요 시나리오를 꺼내고(`resource_eval_cases`),
  2. **base(변경 전, 보통 main)** 버전과 **head(변경 후, PR 브랜치)** 버전을
     Git 에서 각각 받아 워크스페이스에 펼친 뒤,
  3. 같은 시나리오를 두 버전으로 각각 실행하고(test-runner),
  4. 같은 기준으로 채점해(evaluator),
  5. **base 에서 통과하던 단계가 head 에서 깨진 것**을 골라낸다.

`broken` 이 하나라도 있으면 그 병합은 기존 동작을 깨뜨린다는 뜻이고, 화면은 그것을
병합 버튼 옆에 붙여 리뷰어가 병합 전에 보게 한다.

## 왜 에이전트가 아니라 서버가 돌리는가

이건 CI 성격의 검사다. "모델이 잊지 않고 두 버전을 다 돌렸기를 바라는" 구조여선 안 된다
(실제로 스킬 생성 파이프라인에서 모델이 테스트 단계를 통째로 건너뛰던 전례가 있다).
그래서 **오케스트레이션은 이 모듈의 코드가 결정론적으로 수행**하고, 모델은 개별 실행
단위(시나리오 1건 실행 / 채점 1건)만 담당한다.

## 온디스크 배치

    <WORKSPACE>/.pr-verify/<run_id>/
        versions/base/…            ← 변경 전 리소스 파일
        versions/head/…            ← 변경 후 리소스 파일
        inputs/<eval-name>/…       ← 시나리오 첨부 문서(내려받아 **텍스트로 풀어** 둔 것)
        runs/<eval-name>/base/outputs/…   + grading.json
        runs/<eval-name>/head/outputs/…   + grading.json

산출물은 회차가 끝나도 지우지 않는다 — "왜 깨졌는지" 를 확인할 자료가 이것뿐이다.
보존 기간(`PR_VERIFY_RETENTION_DAYS`, 기본 7일)이 지난 회차만 정리한다.

## 현재 지원 범위

`resource_type='skill'` 만 실행기가 있다. 테이블·API·화면은 bpmn/dmn 공용으로
설계돼 있어, 각 타입의 실행기만 추가하면 같은 자리에 끼워진다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .eval_tools import (
    _grading_summary,  # noqa: F401  (재사용: grading.json 정규화)
    _variant_pass_rate,
    collect_gradings_at,
    find_suite,
    grade_mechanically,
    insert_results,
    load_suite_cases,
    reusable_baseline_results,
)
from . import tools as _skill_tools

logger = logging.getLogger(__name__)

# 검증 산출물 루트. 대화방과 무관하므로 .bpmn/<room> 이 아니라 별도 디렉터리를 쓴다.
PR_VERIFY_SUBDIR = ".pr-verify"

BASE_VARIANT = "base"
HEAD_VARIANT = "head"
_VARIANTS = (BASE_VARIANT, HEAD_VARIANT)

# 한 번에 띄우는 실행 수 상한. 시나리오 × 2벌이 한꺼번에 몰리면 샌드박스/모델 호출이
# 서로를 굶기므로 적당히 묶어서 돌린다.
_MAX_PARALLEL = 4

# 리소스 파일 중 검증에 불필요한(그리고 용량만 큰) 경로.
_SKIP_PREFIXES = (".git/", "evals/", "iteration-")


# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------

def verify_root(run_id: str) -> Path:
    """이 검증 회차의 호스트 루트."""
    return _skill_tools.WORKSPACE_ROOT / PR_VERIFY_SUBDIR / str(run_id)


def _container_path(host: Path) -> str:
    """호스트 경로를 샌드박스 컨테이너 경로(/workspace/...)로 바꾼다."""
    try:
        rel = host.relative_to(_skill_tools.WORKSPACE_ROOT)
    except ValueError:
        return str(host)
    return f"/workspace/{rel.as_posix()}"


# ---------------------------------------------------------------------------
# 버전 내려받기
# ---------------------------------------------------------------------------

def _materialize_skill_version(tenant_id: str, skill_name: str, ref: str, dest: Path) -> tuple[int, str]:
    """스킬 레포의 `ref` 시점 파일을 `dest` 에 펼친다.

    Returns:
        (파일 수, SKILL.md 의 sha256). 레포/브랜치가 없으면 예외.
    """
    from .git_providers.factory import get_provider

    provider = get_provider(tenant_id)
    owner = provider.username
    tree = provider.list_repo_tree(owner, skill_name, ref)

    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    digest = ""
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not path or any(path.startswith(p) for p in _SKIP_PREFIXES):
            continue
        raw = provider.fetch_file(owner, skill_name, ref, path)
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        count += 1
        if path == "SKILL.md":
            digest = hashlib.sha256(raw).hexdigest()

    if not (dest / "SKILL.md").is_file():
        raise ValueError(f"'{ref}' 브랜치에 SKILL.md 가 없습니다.")
    return count, digest


# ---------------------------------------------------------------------------
# 시나리오 입력 파일
# ---------------------------------------------------------------------------

# 텍스트로 미리 풀어 두지 않아도 되는(이미 텍스트인) 확장자.
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".html", ".htm", ".yaml", ".yml"}


def _materialize_inputs(root: Path, case: dict) -> tuple[list[str], list[str]]:
    """시나리오의 입력 파일을 스토리지에서 받아 **텍스트로 풀어** 컨테이너 경로로 돌려준다.

    두 가지를 동시에 해결한다.

    1. 시나리오 프롬프트는 보통 "첨부한 계약서를 검토해줘" 처럼 **첨부 문서를 전제로** 쓰인다.
       그 문서를 워크스페이스에 놓아주지 않으면 두 버전 모두 "원문이 없어 검토 불가" 를 내고
       모든 단계가 실패한다 — base/head 가 나란히 0점이 되어 비교가 성립하지 않는다.

    2. 그렇다고 `.docx` 를 그대로 건네면 실행기가 `read_file` 로 읽고, deepagents 는 그것을
       **문서 첨부 블록(`type: "file"`)** 으로 모델에 넘긴다. 그 블록을 거부하는 모델
       (gpt-5.4 등)에서는 요청이 400 으로 죽어 산출물이 하나도 남지 않는다.
       "read_file 하지 마라" 고 프롬프트에 써도 모델은 계속 그렇게 했다(실측).
       그래서 **바이너리 문서를 모델 앞에 아예 두지 않는다** — 여기서 텍스트로 풀어
       그 텍스트 파일 경로만 건넨다.

    Returns:
        (컨테이너 경로 목록, 실패한 storage 경로 목록)
    """
    from ..storage.storage import download_file
    from .tools import extract_document_structure

    paths = [str(f).strip() for f in (case.get("files") or []) if str(f).strip()]
    if not paths:
        return [], []

    dest = root / "inputs" / case["eval_name"]
    dest.mkdir(parents=True, exist_ok=True)

    resolved: list[str] = []
    failed: list[str] = []
    for storage_path in paths:
        local = download_file.invoke({"storage_path": storage_path, "dest_dir": str(dest)})
        if not isinstance(local, str) or local.startswith("ERROR"):
            logger.warning("pr-verify: 입력 파일 준비 실패 %s (%s)", storage_path, local)
            failed.append(storage_path)
            continue

        local_path = Path(local)
        if local_path.suffix.lower() in _TEXT_SUFFIXES:
            resolved.append(_container_path(local_path))
            continue

        text = extract_document_structure.invoke({"file_path": str(local_path)})
        if not isinstance(text, str) or text.startswith("ERROR") or not text.strip():
            logger.warning("pr-verify: 문서 파싱 실패 %s (%s)", storage_path, str(text)[:200])
            failed.append(storage_path)
            continue

        as_text = local_path.with_suffix(local_path.suffix + ".txt")
        as_text.write_text(text, encoding="utf-8")
        resolved.append(_container_path(as_text))

    return resolved, failed


# ---------------------------------------------------------------------------
# 실행 단위 (모델이 담당하는 최소 단위)
# ---------------------------------------------------------------------------

def _build_runner_agent(backend, system_prompt: str, tools: list | None = None):
    from deepagents import create_deep_agent

    from ..llm.model import _build_model

    return create_deep_agent(
        model=_build_model(),
        tools=tools or [],
        system_prompt=system_prompt,
        backend=backend,
    )


async def _invoke(agent, instruction: str, *, label: str) -> None:
    """서브에이전트 한 건을 돌린다. 실패해도 전체 검증을 멈추지 않는다.

    한 실행이 실패하면 그 variant 의 grading.json 이 남지 않고, 검증 결과는
    "비교 불가" 로 보고된다 — 실패를 조용히 통과로 바꾸지 않는 것이 중요하다.
    """
    try:
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": instruction}]},
            {"recursion_limit": 60},
        )
    except Exception:
        logger.exception("pr-verify: 실행 실패 (%s)", label)


def _runner_instruction(
    *, skill_path: str, case: dict, outputs_dir: str, variant: str, input_paths: list[str]
) -> str:
    files_line = (
        json.dumps(input_paths, ensure_ascii=False)
        if input_paths
        else '[] (no input files — work from the prompt alone)'
    )
    return (
        "Execute this evaluation run.\n"
        f"- variant: {variant}\n"
        f"- skill_path: {skill_path}\n"
        f"- prompt: {case['prompt']}\n"
        f"- input_files: {files_line}\n"
        f"- outputs_dir: {outputs_dir}\n"
        "The input files are already on disk at the paths above — read them with read_file "
        "(or the skill's own scripts). Save every produced file to outputs_dir exactly as given."
    )


def _evaluator_instruction(*, case: dict, outputs_dir: str, grading_path: str, variant: str, skill_name: str) -> str:
    return (
        "Grade this evaluation run.\n"
        f"- eval_name: {case['eval_name']}\n"
        f"- variant: {variant}\n"
        f"- skill_name: {skill_name}\n"
        f"- outputs_dir: {outputs_dir}\n"
        f"- grading_path: {grading_path}\n"
        f"- assertions: {json.dumps(case.get('assertions') or [], ensure_ascii=False)}\n"
        "Grade the base and head runs by the same standard — the comparison between them "
        "is the whole point. Write grading.json to grading_path exactly as given."
    )


# ---------------------------------------------------------------------------
# 비교 (변경 전 → 변경 후로 달라진 단계)
# ---------------------------------------------------------------------------

def _align(base_assertions: list[dict], head_assertions: list[dict]) -> list[tuple[dict | None, dict | None]]:
    """같은 단계끼리 짝짓는다. 문구가 같으면 문구로, 아니면 순서로 맞춘다."""
    by_text: dict[str, dict] = {}
    for a in base_assertions:
        by_text.setdefault(a.get("text", ""), a)

    pairs: list[tuple[dict | None, dict | None]] = []
    used: set[int] = set()
    for idx, head in enumerate(head_assertions):
        base = by_text.get(head.get("text", ""))
        if base is not None:
            used.add(base_assertions.index(base))
        elif idx < len(base_assertions):
            base = base_assertions[idx]
            used.add(idx)
        pairs.append((base, head))

    for idx, base in enumerate(base_assertions):
        if idx not in used and base not in [p[0] for p in pairs]:
            pairs.append((base, None))
    return pairs


def diff_results(results: list[dict]) -> dict:
    """base/head 결과를 시나리오별로 비교해 달라진 단계를 뽑는다.

    Returns:
        {"per_case": {...}, "broken": [...], "fixed": [...],
         "incomparable": [...], "no_signal": [...], "broken_count": N, "fixed_count": N}
        - broken: base 에서 통과하던 단계가 head 에서 깨진 것 (병합 시 위험)
        - fixed:  base 에서 실패하던 단계가 head 에서 통과하게 된 것
        - incomparable: 두 버전 중 한쪽 실행이 없어 비교할 수 없는 시나리오
        - no_signal: 두 버전 모두 0점이라 아무 신호도 못 얻은 시나리오
                     (보통 입력 문서가 준비되지 않아 둘 다 아무것도 못 한 경우)
    """
    by_case: dict[str, dict[str, dict]] = {}
    for r in results:
        by_case.setdefault(r["eval_name"], {})[r["variant"]] = r

    per_case: dict[str, dict] = {}
    broken: list[dict] = []
    fixed: list[dict] = []
    incomparable: list[str] = []

    for eval_name, variants in by_case.items():
        base = variants.get(BASE_VARIANT)
        head = variants.get(HEAD_VARIANT)
        if not base or not head:
            incomparable.append(eval_name)
            per_case[eval_name] = {
                "comparable": False,
                "base": _brief(base),
                "head": _brief(head),
            }
            continue

        case_broken: list[dict] = []
        case_fixed: list[dict] = []
        for b, h in _align(base["assertions"], head["assertions"]):
            if b is None or h is None:
                continue
            if b["passed"] and not h["passed"]:
                entry = {"eval_name": eval_name, "step": h.get("text") or b.get("text"),
                         "evidence": h.get("evidence") or ""}
                case_broken.append(entry)
                broken.append(entry)
            elif not b["passed"] and h["passed"]:
                entry = {"eval_name": eval_name, "step": h.get("text") or b.get("text"),
                         "evidence": h.get("evidence") or ""}
                case_fixed.append(entry)
                fixed.append(entry)

        per_case[eval_name] = {
            "comparable": True,
            "base": _brief(base),
            "head": _brief(head),
            "broken": case_broken,
            "fixed": case_fixed,
        }

    # 두 버전 모두 한 단계도 통과하지 못한 시나리오는 "달라진 게 없다" 가 아니라
    # **아무 신호도 못 얻었다**는 뜻이다(보통 입력 문서가 없어 둘 다 아무것도 못 한 경우).
    # 이것을 조용히 두면 broken_count=0 이라 검증을 통과한 것처럼 보인다.
    no_signal = [
        name for name, v in per_case.items()
        if v.get("comparable")
        and (v.get("base") or {}).get("passed") == 0
        and (v.get("head") or {}).get("passed") == 0
    ]

    return {
        "per_case": per_case,
        "broken": broken,
        "fixed": fixed,
        "incomparable": incomparable,
        "no_signal": no_signal,
        "broken_count": len(broken),
        "fixed_count": len(fixed),
    }


def _brief(row: dict | None) -> dict | None:
    """시나리오 한 벌의 결과 요약.

    통과 수만 남기면 화면에는 `2/2 → 0/2` 만 보이고 **무엇이 나왔길래 실패인지**는
    사라진다. 첫 단언의 근거(`결론 '(매칭 없음)' (기대 '20')`)를 함께 실어, 변경 전/후
    실제 산출값을 그 자리에서 읽을 수 있게 한다.
    """
    if not row:
        return None
    assertions = row.get("assertions") or []
    return {
        "passed": row["passed"],
        "total": row["total"],
        "pass_rate": row["pass_rate"],
        "evidence": (assertions[0].get("evidence") or "") if assertions else "",
    }


# ---------------------------------------------------------------------------
# 회차 수명주기
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
    pr: dict,
    created_by: str = "",
) -> dict:
    """검증 회차 행을 'running' 으로 만들고 돌려준다.

    시나리오가 아직 없는 리소스면 회차를 만들지 않고 사유를 돌려준다 — 실행할 게 없다.
    """
    resource_type = pr.get("resource_type") or "skill"
    resource_id = pr.get("resource_id") or ""

    suite_id = find_suite(tenant_id, resource_id, resource_type)
    if not suite_id:
        return {
            "error": "no_suite",
            "message": (
                f"'{resource_id}' 에 저장된 검증 시나리오가 없습니다. "
                "이 리소스는 시나리오를 확보하는 단계를 거치지 않고 만들어져, "
                "병합 전에 기존 동작이 깨지는지 비교할 기준이 없습니다."
            ),
        }

    cases = load_suite_cases(suite_id)
    if not cases:
        return {"error": "no_cases", "message": f"'{resource_id}' 스위트에 시나리오가 없습니다."}

    payload = {
        "suite_id": suite_id,
        "tenant_id": tenant_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "trigger": "pull_request",
        "pr_id": pr["id"],
        "base_ref": pr.get("base_branch") or "main",
        "head_ref": pr.get("branch_name") or "",
        "status": "running",
        "created_by": created_by or None,
        "summary": {"case_count": len(cases)},
    }
    row = _sb().table("resource_eval_runs").insert(payload).execute()
    return {"run": row.data[0], "suite_id": suite_id, "cases": cases}


def _finish(run_id: str, **fields: Any) -> None:
    _sb().table("resource_eval_runs").update(
        {"finished_at": _now(), **fields}
    ).eq("id", run_id).execute()


def fail_run(run_id: str, message: str) -> None:
    logger.warning("pr-verify: 회차 실패 run=%s: %s", run_id, message)
    _finish(run_id, status="failed", error=message[:2000])


async def _run_via_engine(
    *, tenant_id: str, run: dict, cases: list[dict], resource_type: str, label: str
) -> None:
    """회차 하나를 실행 엔진(completion)에 위임해 수행한다.

    이 모듈이 하는 일은 회차 관리다 — 기준선 재사용 판단, base/head 비교, 결과 기록.
    "이 정의에서 이 시나리오가 어떤 결과인가" 는 판정 규칙의 진실 원천이 있는 곳
    (실행 엔진)에 묻는다. 사본을 여기 두면 엔진이 규칙을 고칠 때 회귀 테스트만 옛 규칙으로
    통과시킨다.
    """
    from . import regression_client

    run_id = run["id"]
    resource_id = run["resource_id"]

    # 기준선은 같은 버전에서 이미 채점해 뒀으면 다시 계산하지 않는다. digest 를 알려면
    # 엔진에 물어야 하므로, 먼저 재사용 후보를 케이스 이름으로만 좁혀 두고 회차 응답의
    # digest 로 확정한다.
    reused: dict[str, dict] = {}
    try:
        probe = await regression_client.run(
            tenant_id=tenant_id, resource_type=resource_type, resource_id=resource_id,
            base_ref=run["base_ref"], head_ref=run["head_ref"], cases=cases,
        )
    except regression_client.RegressionUnavailable as e:
        fail_run(run_id, str(e))
        return

    if probe.get("error"):
        fail_run(run_id, probe.get("message") or probe["error"])
        return

    digests = {
        BASE_VARIANT: probe.get("base_digest") or "",
        HEAD_VARIANT: probe.get("head_digest") or "",
    }
    _sb().table("resource_eval_runs").update({
        "base_digest": digests[BASE_VARIANT],
        "head_digest": digests[HEAD_VARIANT],
    }).eq("id", run_id).execute()

    reused = await asyncio.to_thread(
        reusable_baseline_results,
        tenant_id=tenant_id,
        resource_id=resource_id,
        digest=digests[BASE_VARIANT],
        variant=BASE_VARIANT,
        cases=cases,
    )

    # 재사용할 기준선이 있으면 그 시나리오의 base 결과는 방금 받은 것 대신 저장된 것을 쓴다.
    # 재생은 순수 함수라 값은 같지만, 회차마다 같은 판정을 다시 계산하지 않는다는 규칙을
    # 여기서도 지킨다(스킬 회귀에서 같은 자리가 실제 누수였다).
    results = [
        r for r in (probe.get("results") or [])
        if not (r.get("variant") == BASE_VARIANT and r.get("eval_name") in reused)
    ]
    for row in results:
        row.setdefault("outputs_dir", None)
    results.extend(reused.values())

    undecided = probe.get("undecided") or []
    if not results:
        fail_run(run_id, "채점된 시나리오가 없습니다. 저장된 시나리오와 정의를 확인하세요.")
        return

    diff = diff_results(results)
    if undecided:
        # 판정하지 못한 시나리오를 조용히 두면 broken_count=0 이라 "이상 없음" 으로 보인다.
        diff["undecided"] = undecided
    diff["mode"] = probe.get("mode") or resource_type
    diff["llm_calls"] = probe.get("llm_calls", 0)
    diff["engine_instances"] = probe.get("engine_instances", 0)

    case_ids = {c["eval_name"]: c["id"] for c in cases if c.get("id")}
    insert_results(run_id=run_id, tenant_id=tenant_id, results=results, case_ids=case_ids)

    _finish(
        run_id,
        status="succeeded",
        pass_rate=_variant_pass_rate(results, (HEAD_VARIANT,)),
        base_pass_rate=_variant_pass_rate(results, (BASE_VARIANT,)),
        broken_count=diff["broken_count"],
        fixed_count=diff["fixed_count"],
        summary=diff,
    )
    logger.info(
        "pr-verify(%s): 완료 run=%s broken=%d fixed=%d 판정불가=%d 재사용=%d",
        label, run_id, diff["broken_count"], diff["fixed_count"], len(undecided), len(reused),
    )


async def execute_run(
    *,
    tenant_id: str,
    run: dict,
    cases: list[dict],
    suite_id: str,
) -> None:
    """검증 회차 하나를 끝까지 수행하고 결과를 기록한다.

    실패는 회차 상태로 남긴다 — 예외를 밖으로 던지지 않는다(백그라운드 태스크).
    """
    run_id = run["id"]
    resource_id = run["resource_id"]
    root = verify_root(run_id)

    try:
        resource_type = run.get("resource_type") or "skill"
        if resource_type in ("bpmn", "dmn"):
            await _run_via_engine(
                tenant_id=tenant_id, run=run, cases=cases,
                resource_type=resource_type,
                label="process" if resource_type == "bpmn" else "dmn",
            )
            return
        if resource_type != "skill":
            fail_run(run_id, f"'{resource_type}' 타입은 아직 검증 실행기가 없습니다.")
            return

        # 1) 두 버전을 워크스페이스에 펼친다.
        digests: dict[str, str] = {}
        for variant, ref in ((BASE_VARIANT, run["base_ref"]), (HEAD_VARIANT, run["head_ref"])):
            if not ref:
                fail_run(run_id, f"{variant} 브랜치 이름이 비어 있습니다.")
                return
            try:
                count, digest = await asyncio.to_thread(
                    _materialize_skill_version, tenant_id, resource_id, ref, root / "versions" / variant
                )
            except Exception as e:
                fail_run(run_id, f"{variant}({ref}) 버전을 가져오지 못했습니다: {e}")
                return
            digests[variant] = digest
            logger.info("pr-verify: %s(%s) 파일 %d개 준비 run=%s", variant, ref, count, run_id)

        if digests[BASE_VARIANT] and digests[BASE_VARIANT] == digests[HEAD_VARIANT]:
            # SKILL.md 가 동일해도 참조 파일이 바뀌었을 수 있으므로 중단하지는 않는다.
            logger.info("pr-verify: base 와 head 의 SKILL.md 가 동일 run=%s", run_id)

        _sb().table("resource_eval_runs").update({
            "base_digest": digests[BASE_VARIANT],
            "head_digest": digests[HEAD_VARIANT],
        }).eq("id", run_id).execute()

        # 2) 기준선은 같은 커밋에서 이미 채점해 뒀으면 다시 돌리지 않는다.
        #    기준선 재실행은 비용이 두 배로 드는 데다 실행마다 점수가 흔들려 비교를 망친다.
        reused = await asyncio.to_thread(
            reusable_baseline_results,
            tenant_id=tenant_id,
            resource_id=resource_id,
            digest=digests[BASE_VARIANT],
            variant=BASE_VARIANT,
            cases=cases,
        )
        if reused:
            logger.info(
                "pr-verify: 기준선 %d/%d건 재사용(digest=%s) run=%s",
                len(reused), len(cases), (digests[BASE_VARIANT] or "")[:12], run_id,
            )

        # 3) 시나리오 × 두 버전 실행 → 4) 같은 기준으로 채점
        backend = _sandbox(tenant_id)
        missing_inputs = await _run_all(
            backend, root, resource_id, cases, skip_base=set(reused)
        )

        # 5) 채점 결과 수집 — 재사용한 기준선은 이번 회차에 파일이 없으므로 합쳐 넣는다.
        results, missing = collect_gradings_at(
            root / "runs",
            cases,
            variants=_VARIANTS,
            baseline_variants=(BASE_VARIANT,),
            primary_variant=HEAD_VARIANT,
        )
        if reused:
            results = list(results) + list(reused.values())
            missing = [
                m for m in missing
                if not (m.get("variant") == BASE_VARIANT and m.get("eval_name") in reused)
            ]
        if not results:
            fail_run(run_id, "두 버전 모두 채점 결과가 만들어지지 않았습니다. 실행 로그를 확인하세요.")
            return

        # 5) 달라진 단계 비교
        diff = diff_results(results)
        diff["missing_runs"] = missing
        # 입력 문서를 못 받아온 시나리오는 두 버전 모두 "원문 없음" 으로 실패해 비교가
        # 성립하지 않는다. 조용히 "이상 없음" 으로 보이지 않도록 결과에 남긴다.
        if missing_inputs:
            diff["missing_inputs"] = missing_inputs
        if diff["no_signal"]:
            logger.warning(
                "pr-verify: 양쪽 모두 0점이라 신호가 없는 시나리오 run=%s: %s",
                run_id, diff["no_signal"],
            )

        case_ids = {c["eval_name"]: c["id"] for c in cases if c.get("id")}
        insert_results(run_id=run_id, tenant_id=tenant_id, results=results, case_ids=case_ids)

        _finish(
            run_id,
            status="succeeded",
            pass_rate=_variant_pass_rate(results, (HEAD_VARIANT,)),
            base_pass_rate=_variant_pass_rate(results, (BASE_VARIANT,)),
            broken_count=diff["broken_count"],
            fixed_count=diff["fixed_count"],
            summary=diff,
        )
        logger.info(
            "pr-verify: 완료 run=%s broken=%d fixed=%d",
            run_id, diff["broken_count"], diff["fixed_count"],
        )
    except Exception as e:  # 백그라운드 태스크 — 예외를 상태로 남긴다.
        logger.exception("pr-verify: 회차 처리 중 예외 run=%s", run_id)
        fail_run(run_id, f"검증 중 오류: {e}")
    finally:
        _cleanup(root)


def _sandbox(tenant_id: str):
    from pathlib import Path as _P

    from ..agents.agent import _BASE_DIR, _SKILLS_DIR
    from ..sandbox.docker_sandbox import get_or_create_sandbox

    return get_or_create_sandbox(
        tenant_id, workspace_host=_P(_BASE_DIR) / "workspace", skills_host=_SKILLS_DIR
    )


async def _run_all(
    backend,
    root: Path,
    skill_name: str,
    cases: list[dict],
    skip_base: set[str] | None = None,
) -> dict[str, list[str]]:
    """시나리오 × (base, head) 를 실행한 뒤 같은 조합을 채점한다.

    Args:
        skip_base: 기준선 결과를 재사용하는 시나리오 이름들. 이 시나리오의 base 는
            실행도 채점도 건너뛴다 — 같은 커밋을 다시 돌려 같은 답을 얻으려고 모델을
            두 번 더 부르는 셈이기 때문이다.

    Returns:
        {eval_name: [준비 실패한 storage 경로]} — 비어 있으면 입력이 모두 준비됐다는 뜻.
    """
    skip_base = skip_base or set()
    from ..agents.skill_creator_subagents import (
        _DEFAULT_SKILL_CREATOR_PATH,
        _TEST_RUNNER_SYSTEM,
        _build_evaluator_system,
    )

    # 문서 파서를 반드시 쥐어준다. read_file 로 .docx 를 읽으면 deepagents 가 그것을
    # 문서 첨부 블록으로 모델에 넘기고, 그 타입을 거부하는 모델(gpt-5.4 등)에서는
    # 실행이 400 으로 죽어 산출물이 0개가 된다 — 그러면 base/head 가 나란히 0점이 되어
    # "달라진 게 없다" 로 오해된다. 실제로 그렇게 나왔다.
    from ..skills.tools import extract_document_structure

    runner = _build_runner_agent(backend, _TEST_RUNNER_SYSTEM, tools=[extract_document_structure])
    grader = _build_runner_agent(backend, _build_evaluator_system(_DEFAULT_SKILL_CREATOR_PATH))

    async def _bounded(coros: list) -> None:
        for i in range(0, len(coros), _MAX_PARALLEL):
            await asyncio.gather(*coros[i:i + _MAX_PARALLEL])

    # 입력 문서를 먼저 워크스페이스에 놓는다(없으면 두 버전 모두 "원문 없음" 으로 실패한다).
    missing_inputs: dict[str, list[str]] = {}
    inputs_by_case: dict[str, list[str]] = {}
    for case in cases:
        resolved, failed = await asyncio.to_thread(_materialize_inputs, root, case)
        inputs_by_case[case["eval_name"]] = resolved
        if failed:
            missing_inputs[case["eval_name"]] = failed

    # 실행: 두 버전 모두(기준선을 재사용하는 시나리오는 head 만)
    run_jobs = []
    for case in cases:
        for variant in _VARIANTS:
            if variant == BASE_VARIANT and case["eval_name"] in skip_base:
                continue
            outputs = root / "runs" / case["eval_name"] / variant / "outputs"
            outputs.mkdir(parents=True, exist_ok=True)
            run_jobs.append(_invoke(
                runner,
                _runner_instruction(
                    skill_path=_container_path(root / "versions" / variant),
                    case=case,
                    outputs_dir=_container_path(outputs),
                    variant=variant,
                    input_paths=inputs_by_case[case["eval_name"]],
                ),
                label=f"run {case['eval_name']}/{variant}",
            ))
    await _bounded(run_jobs)

    # 채점: 두 버전 모두 같은 기준으로
    grade_jobs = []
    graded_in_code = 0
    for case in cases:
        for variant in _VARIANTS:
            if variant == BASE_VARIANT and case["eval_name"] in skip_base:
                continue
            vdir = root / "runs" / case["eval_name"] / variant
            # assertion 이 전부 기계 검사면 코드로 채점한다 — 문자열 포함 여부를
            # 확인하려고 채점 서브에이전트를 부를 이유가 없다.
            mechanical = grade_mechanically(
                vdir / "outputs", case.get("assertions") or [], case.get("checks") or []
            )
            if mechanical is not None:
                vdir.mkdir(parents=True, exist_ok=True)
                (vdir / "grading.json").write_text(
                    json.dumps(mechanical, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                graded_in_code += 1
                continue
            grade_jobs.append(_invoke(
                grader,
                _evaluator_instruction(
                    case=case,
                    outputs_dir=_container_path(vdir / "outputs"),
                    grading_path=_container_path(vdir / "grading.json"),
                    variant=variant,
                    skill_name=skill_name,
                ),
                label=f"grade {case['eval_name']}/{variant}",
            ))
    await _bounded(grade_jobs)
    if graded_in_code:
        logger.info(
            "pr-verify: 채점 %d건을 코드로 처리(모델 호출 %d건만 사용)",
            graded_in_code, len(grade_jobs),
        )
    return missing_inputs


def _cleanup(root: Path) -> None:
    """이번 회차 산출물은 남기고, 오래된 회차만 걷어낸다.

    "왜 깨졌는지" 를 확인할 자료는 실행 산출물뿐이라(결과 행에 outputs_dir 을 적어 둔다)
    회차가 끝났다고 지우면 리뷰어도 개발자도 근거를 다시 볼 수 없다. 대신 보존 기간이
    지난 회차 디렉터리를 정리해 무한정 쌓이지 않게 한다.
    """
    import os
    import time

    try:
        days = int(os.getenv("PR_VERIFY_RETENTION_DAYS", "7"))
    except ValueError:
        days = 7

    parent = root.parent
    if not parent.is_dir():
        return
    cutoff = time.time() - days * 86400
    for child in parent.iterdir():
        if child == root or not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logger.warning("pr-verify: 오래된 산출물 정리 실패 %s", child, exc_info=True)


# ---------------------------------------------------------------------------
# 조회
# ---------------------------------------------------------------------------

def latest_run_for_pr(tenant_id: str, pr_id: str) -> dict | None:
    rows = (
        _sb()
        .table("resource_eval_runs")
        .select(
            "id,status,error,trigger,base_ref,head_ref,base_digest,head_digest,"
            "pass_rate,base_pass_rate,broken_count,fixed_count,summary,started_at,finished_at"
        )
        .eq("tenant_id", tenant_id)
        .eq("pr_id", pr_id)
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    return (rows.data or [None])[0]


def run_results(tenant_id: str, run_id: str) -> list[dict]:
    rows = (
        _sb()
        .table("resource_eval_results")
        .select("eval_name,variant,passed,total,pass_rate,assertions")
        .eq("tenant_id", tenant_id)
        .eq("run_id", run_id)
        .execute()
    )
    return rows.data or []
