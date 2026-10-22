"""병합 요청 제목이 "무엇이 바뀌는가" 를 말하는지 고정한다.

관측된 문제: 스킬 병합 요청 제목이 전부 `feat: update <skill>` 이라, 병합 요청함에
같은 스킬을 고친 요청 세 건이 나란히 서면 브랜치 해시로만 갈렸다. 검토자는 제목만
보고는 어느 것을 먼저 볼지 고를 수 없었다.

여기서는 diff → 한 줄 요약 규칙을 실제 스킬 변경 모양으로 고정한다.
문구는 화면(process-gpt-vue3 `usePrChanges.ts`)과 같아야 한다 — 같은 변경을 두 곳에서
다르게 부르면 검토자가 같은 요청을 다른 것으로 읽는다.
"""

from __future__ import annotations

from core.skills.diff_summary import summarize_diff

TITLE_MAX = 72


def _diff(*body: str) -> str:
    return "\n".join(body) + "\n"


def test_한_줄_추가는_문단_제목과_함께_그_문장을_인용한다():
    patch = _diff(
        "diff --git a/SKILL.md b/SKILL.md",
        "index 111..222 100644",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -72,6 +72,7 @@ description: Review meeting room booking",
        " ",
        " ## 작성 원칙",
        " ",
        "+- 판정 사유는 한 문장으로 간결하게 쓴다.",
        " - 규칙에 없는 임의 기준을 추가하지 않는다.",
    )
    assert summarize_diff(patch) == '작성 원칙 · "판정 사유는 한 문장으로 간결하게 쓴다." 추가'


def test_여러_줄_추가는_첫_줄만_인용하고_나머지는_건수로_접는다():
    patch = _diff(
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -10,3 +10,6 @@",
        " ## 판정 규칙",
        "+- 예약 불가 판정에는 재신청 안내를 반드시 넣는다",
        "+- 접두어는 재신청 안내: 를 그대로 사용한다",
        "+- 비슷한 표현으로 바꾸지 않는다",
    )
    summary = summarize_diff(patch)
    assert summary.startswith('판정 규칙 · "예약 불가 판정에는 재신청 안내를 반드시 넣는다"')
    assert "외 2줄 추가" in summary


def test_새_문서는_파일명이_아니라_문서_제목으로_부른다():
    patch = _diff(
        "diff --git a/references/nda-review-checklist.md b/references/nda-review-checklist.md",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/references/nda-review-checklist.md",
        "@@ -0,0 +1,3 @@",
        "+# NDA 검토 체크리스트 (상세 기준)",
        "+",
        "+이 문서는 비밀유지계약서 검토 시 점검 항목을 정리한 것이다.",
    )
    assert summarize_diff(patch) == "'NDA 검토 체크리스트 (상세 기준)' 문서 추가"


def test_크게_손댄_파일은_한_줄_인용_대신_전면_개편이라고_말한다():
    """줄 하나를 인용하면 나머지 수백 줄이 그 한 줄인 것처럼 읽힌다."""
    body = [
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -1,60 +1,1 @@",
    ]
    body += [f"-오래된 본문 줄 {i}" for i in range(60)]
    body += ["+새로 쓴 본문 한 줄"]
    assert summarize_diff(_diff(*body)) == "스킬 본문 전면 개편"


def test_description_변경은_호출_조건이_바뀐다고_짚어_준다():
    """description 은 언제 이 스킬이 불릴지를 정하는 문장이라 다른 줄과 무게가 다르다."""
    patch = _diff(
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -1,4 +1,4 @@",
        " ---",
        " name: room-check",
        "-description: 예전 설명 문구",
        "+description: 회의실 예약 요청을 검토하는 전용 스킬",
        " ---",
    )
    assert summarize_diff(patch) == "호출 조건 문구 수정"


def test_삭제된_파일은_건수로_묶어_뒤에_붙인다():
    patch = _diff(
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -5,2 +5,3 @@",
        " ## 참고",
        "+- 첨부 문서는 더 이상 쓰지 않는다",
        "diff --git a/evals/a.docx b/evals/a.docx",
        "deleted file mode 100644",
        "--- a/evals/a.docx",
        "+++ /dev/null",
        "diff --git a/evals/b.docx b/evals/b.docx",
        "deleted file mode 100644",
        "--- a/evals/b.docx",
        "+++ /dev/null",
    )
    assert summarize_diff(patch).endswith("파일 2개 삭제")


def test_구분선과_표_구분행은_인용하지_않는다():
    """`---` 나 `|---|` 를 인용해 봐야 무엇이 바뀌었는지 알 수 없다."""
    patch = _diff(
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -1,2 +1,5 @@",
        " ## 표",
        "+|------|------|",
        "+---",
        "+| 유형 | 검토 포인트 |",
    )
    assert summarize_diff(patch) == '표 · "| 유형 | 검토 포인트 |" 추가'


def test_제목_길이를_넘기면_남은_조각은_외_N건으로_접는다():
    """개수로 자르면 '새 문서가 생겼다' 가 '파일 1개 변경' 같은 맹탕에 밀려난다."""
    body = [
        "diff --git a/SKILL.md b/SKILL.md",
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -1,60 +1,1 @@",
    ]
    body += [f"-오래된 본문 줄 {i}" for i in range(60)]
    body += ["+새로 쓴 본문"]
    body += [
        "diff --git a/references/checklist.md b/references/checklist.md",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/references/checklist.md",
        "@@ -0,0 +1,1 @@",
        "+# 아주 긴 제목을 가진 참고 문서 하나 두울 세엣 네엣 다섯 여섯 일곱",
        "diff --git a/evals/evals.json b/evals/evals.json",
        "--- a/evals/evals.json",
        "+++ b/evals/evals.json",
        "@@ -1,2 +1,3 @@",
        '+        "새 평가 기준이 하나 늘었다",',
    ]
    title = summarize_diff(_diff(*body), max_chars=TITLE_MAX)
    assert len(title) <= TITLE_MAX + len(" · 외 1건")
    assert title.startswith("스킬 본문 전면 개편")
    assert "외 1건" in title


def test_diff_가_비면_빈_문자열을_돌려준다():
    """요약을 못 만들었다고 스킬 발행을 막아서는 안 된다."""
    assert summarize_diff("") == ""
    assert summarize_diff("아무 diff 도 아닌 문자열") == ""
