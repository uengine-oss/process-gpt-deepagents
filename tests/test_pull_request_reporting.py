"""PR 을 올린 턴의 응답을 기본 안내 문구로 덮어쓰지 않는지 검증한다.

executor 는 스킬 산출물이 생긴 턴의 응답을 "스킬을 생성했어요. 확인 후 저장 버튼을
눌러주세요." 로 대체한다. 그런데 병합 요청(PR)을 올린 턴은 사용자가 저장 버튼을 누를 일이
없고, 덮어쓰면 PR 링크와 병합 위치 안내가 사라진다.

executor.py 는 무거운 의존성을 끌어오므로, 판정 함수만 소스에서 추출해 테스트한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_EXECUTOR = Path(__file__).parent.parent / "executor.py"


def _load_reporter():
    """executor.py 에서 _PULL_REQUEST_MARKERS / _reports_pull_request 만 추출해 실행한다."""
    tree = ast.parse(_EXECUTOR.read_text(encoding="utf-8"))
    wanted: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PULL_REQUEST_MARKERS" for t in node.targets
        ):
            wanted.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_reports_pull_request":
            wanted.append(node)
    assert len(wanted) == 2, f"추출 실패: {[getattr(n, 'name', type(n).__name__) for n in wanted]}"

    ns: dict = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(_EXECUTOR), "exec"), ns)
    return ns["_reports_pull_request"]


@pytest.fixture(scope="module")
def reports_pr():
    return _load_reporter()


@pytest.mark.parametrize(
    "text",
    [
        "PR created for 'nda-review' → https://github.com/ohsy818/nda-review/pull/1",
        "https://github.com/o/r/pull/12 로 올렸습니다.",
        "https://gitlab.com/o/r/-/merge_requests/3 확인해 주세요.",
        "PR #1: feat: update nda-review",
        "기본 브랜치에 직접 커밋하지 않고 PR을 생성했습니다. 리뷰 후 병합해 주세요.",
        "변경사항을 병합 요청으로 올렸습니다.",
    ],
)
def test_detects_pull_request_reports(reports_pr, text):
    assert reports_pr(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.",
        "출장 경비 정산 스킬을 만들었습니다.",
        "",
        None,
    ],
)
def test_plain_replies_are_not_pull_requests(reports_pr, text):
    assert reports_pr(text) is False


def test_override_condition_matches_executor_usage(reports_pr):
    """executor 의 실제 조건식(둘 다 아닐 때만 덮어쓴다)을 그대로 재현한다."""

    def should_override(raw_text: str | None) -> bool:
        return "확인 후 저장" not in (raw_text or "") and not reports_pr(raw_text)

    # PR 보고 → 덮어쓰지 않는다(핵심 회귀 케이스)
    assert should_override("PR created → https://github.com/o/r/pull/1") is False
    # 이미 안내 문구가 있으면 덮어쓰지 않는다(기존 동작)
    assert should_override("스킬을 생성했어요. 확인 후 저장 버튼을 눌러주세요.") is False
    # 그 외 평범한 응답은 기본 문구로 대체한다(기존 동작)
    assert should_override("스킬 파일을 만들었습니다.") is True
