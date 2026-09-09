"""스킬 변경(unified diff)을 사람이 읽는 한 줄로 옮긴다.

병합 요청 제목이 `feat: update <skill>` 이면 검토자는 제목만 보고는 열어 볼 이유를
알 수 없다 — 같은 스킬을 고친 요청 여러 건이 병합 요청함에 나란히 서면 브랜치 해시로만
갈린다. 그래서 제목 자리에 "무엇이 바뀌는가" 를 넣는다.

규칙은 화면(process-gpt-vue3 `usePrChanges.ts`)과 일부러 같게 맞췄다. 같은 변경을
두 곳에서 다르게 부르면 검토자가 같은 요청을 다른 것으로 읽는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 이만큼 손댔으면 한 줄 인용으로는 오해를 부른다. 통째로 다시 썼다고 말하는 편이 정직하다.
REWRITE_THRESHOLD = 50

_HEADING = re.compile(r"^#{1,6}\s+(.+)$")
_LIST_MARKER = re.compile(r"^([-*+]\s+|\d+[.)]\s+)")
_EMPHASIS = re.compile(r"\*\*|__|`")
_SEPARATOR = re.compile(r"^[-=_*\s]+$")
_TABLE_RULE = re.compile(r"^\|[\s|:-]*\|?$")
_JSON_SKELETON = re.compile(r"^[\[\]{},]+$")
_WORD = re.compile(r"[0-9A-Za-z가-힣]")
_DESCRIPTION_KEY = re.compile(r"^description\s*:", re.IGNORECASE)


@dataclass
class _FileChange:
    path: str
    status: str = "modified"  # added | removed | modified
    additions: int = 0
    deletions: int = 0
    added: list[tuple[str, str]] = field(default_factory=list)  # (본문, 문단 제목)
    removed: list[tuple[str, str]] = field(default_factory=list)


def _clean_line(raw: str) -> str:
    """인용할 수 있게 목록 기호·강조·따옴표를 걷어낸다."""
    text = raw.strip()
    text = _LIST_MARKER.sub("", text)
    text = re.sub(r"^#{1,6}\s+", "", text)
    text = _EMPHASIS.sub("", text)
    text = text.strip("\"'“”‘’ ,")
    return re.sub(r"\s+", " ", text).strip()


def _is_noise(text: str) -> bool:
    """내용이 없는 줄. 인용해 봐야 아무것도 알려주지 못한다."""
    if not text:
        return True
    if _SEPARATOR.match(text) or _TABLE_RULE.match(text) or _JSON_SKELETON.match(text):
        return True
    return len(_WORD.findall(text)) < 2


def _truncate(text: str, limit: int) -> str:
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _file_label(path: str) -> str:
    """경로를 검토자가 쓰는 말로 바꾼다. 경로는 파일 위치일 뿐 내용이 아니다."""
    if re.fullmatch(r"SKILL\.md", path, re.IGNORECASE):
        return "스킬 본문"
    if re.search(r"(^|/)evals?/", path, re.IGNORECASE) or re.search(r"evals?\.json$", path, re.IGNORECASE):
        return "평가 시나리오"
    if re.search(r"(^|/)references?/", path, re.IGNORECASE):
        return "참고 문서"
    return path


def parse_unified_diff(patch: str) -> list[_FileChange]:
    """`git diff` 출력을 파일별 변경으로 쪼갠다.

    문맥 줄에 나오는 제목(`## …`)을 따라가며 각 줄이 어느 문단에서 바뀌었는지도 남긴다 —
    "무엇이 바뀌었나" 는 대개 "어디가 바뀌었나" 와 함께여야 뜻이 통한다.
    """
    files: list[_FileChange] = []
    current: _FileChange | None = None
    section = ""

    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            path = parts[1].strip() if len(parts) == 2 else line.split()[-1].lstrip("b/")
            current = _FileChange(path=path)
            files.append(current)
            section = ""
            continue
        if current is None:
            continue

        if line.startswith("new file mode"):
            current.status = "added"
            continue
        if line.startswith("deleted file mode"):
            current.status = "removed"
            continue
        if line.startswith("@@"):
            section = ""
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("index "):
            continue

        if not line:
            continue
        marker, body = line[0], line[1:]
        heading = _HEADING.match(body.strip())

        if marker == " ":
            if heading:
                section = _clean_line(heading.group(1))
            continue
        if marker not in "+-":
            continue

        if marker == "+":
            current.additions += 1
        else:
            current.deletions += 1

        text = _clean_line(body)
        if _is_noise(text):
            if marker == "+" and heading:
                section = text
            continue
        (current.added if marker == "+" else current.removed).append((text, section))
        if marker == "+" and heading:
            section = text

    return files


def _describe_file(change: _FileChange) -> str:
    """파일 한 건이 무엇을 바꾸는지 한 조각으로 옮긴다."""
    label = _file_label(change.path)

    if change.status == "added":
        # 새 문서는 제목이 곧 내용 요약이다.
        title = next((text for text, sec in change.added if sec and sec == text), None)
        if title is None and change.added:
            title = change.added[0][0]
        return f"'{_truncate(title, 34)}' 문서 추가" if title else f"{label} 추가"

    if change.status == "removed":
        return f"{label} 삭제"

    if change.additions + change.deletions >= REWRITE_THRESHOLD:
        return f"{label} 전면 개편"

    # 스킬 설명(description)은 언제 이 스킬이 불릴지를 정하는 문장이라 따로 짚어 준다.
    is_desc = lambda pair: bool(_DESCRIPTION_KEY.match(pair[0]))  # noqa: E731
    if any(map(is_desc, change.added)) or any(map(is_desc, change.removed)):
        rest = [pair for pair in change.added if not is_desc(pair)]
        return f"호출 조건 문구 수정 외 {len(rest)}줄" if rest else "호출 조건 문구 수정"

    quoted = (change.added or change.removed or [None])[0]
    if quoted is None:
        return f"{label} 변경"

    text, section = quoted
    verb = "추가" if change.added else "삭제"
    rest = (len(change.added) or len(change.removed)) - 1
    where = f"{_truncate(section, 16)} · " if section else ""
    tail = f" 외 {rest}줄" if rest > 0 else ""
    return f'{where}"{_truncate(text, 40)}"{tail} {verb}'


def summarize_diff(patch: str, *, max_chars: int = 0) -> str:
    """스킬 변경이 실제로 무엇을 바꾸는지 한 줄로 옮긴다.

    `max_chars` 를 주면 길이로 자르고 남은 건수는 `외 N건` 으로 접는다.
    개수로 자르면 "새 문서가 생겼다" 처럼 정작 알아야 할 조각이
    "파일 1개 변경" 같은 맹탕에 밀려난다.
    """
    files = parse_unified_diff(patch)
    if not files:
        return ""

    removed = [f for f in files if f.status == "removed"]
    rest = [f for f in files if f.status != "removed"]
    # 내용이 실린 파일부터 말한다. 삭제는 건수로 묶어 뒤에 붙인다.
    rest.sort(key=lambda f: f.additions + f.deletions, reverse=True)

    notes = [_describe_file(f) for f in rest]
    if removed:
        notes.append(_describe_file(removed[0]) if len(removed) == 1 else f"파일 {len(removed)}개 삭제")

    if not notes:
        return ""
    if max_chars <= 0:
        return " · ".join(notes)

    kept: list[str] = []
    width = 0
    for note in notes:
        nxt = width + len(note) + (3 if kept else 0)
        if kept and nxt > max_chars:
            break
        kept.append(note)
        width = nxt
    hidden = len(notes) - len(kept)
    if hidden:
        kept.append(f"외 {hidden}건")
    return " · ".join(kept)
