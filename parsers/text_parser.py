"""parsers/text_parser.py — TXT / Markdown 파서.

HTML 코멘트(<!-- ... -->)와 Markdown 관례 주석 패턴을 탐지한다.
공식 주석 구조가 없는 포맷이므로 최선 탐지(best-effort)다.
"""

from __future__ import annotations

import re

from parsers.protocol import AnnotationElement, StructuredDocument

# <!-- anchor: ... note: ... --> 형태 (커스텀 컨벤션 지원)
_HTML_COMMENT_WITH_ANCHOR = re.compile(
    r"<!--\s*anchor:\s*(.+?)\s*note:\s*(.+?)\s*-->",
    re.DOTALL,
)
# 단순 <!-- ... --> 코멘트
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)

# [text](url "note") Markdown 링크 타이틀을 주석으로 취급
_MD_LINK_WITH_TITLE = re.compile(r'\[([^\]]+)\]\([^\s)]+\s+"([^"]+)"\)')

# > [!NOTE] / > [!TIP] 등 GitHub Flavored Markdown callout
_GFM_CALLOUT = re.compile(r"> \[!(NOTE|TIP|WARNING|CAUTION|IMPORTANT)\]\s*\n((?:>.*\n?)*)", re.IGNORECASE)


def parse(file_path: str) -> StructuredDocument:
    fmt = "md" if file_path.lower().endswith((".md", ".markdown")) else "txt"
    try:
        text = open(file_path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        return StructuredDocument(
            file_path=file_path,
            format=fmt,
            body_text="",
            elements=[],
            parser_notes=f"read error: {e}",
        )

    elements: list[AnnotationElement] = []

    # 1) anchor+note 형식 HTML 코멘트
    for m in _HTML_COMMENT_WITH_ANCHOR.finditer(text):
        elements.append(AnnotationElement(
            type="html_comment",
            anchor_text=m.group(1).strip(),
            content=m.group(2).strip(),
        ))

    # 2) 단순 HTML 코멘트 — 앞 문장을 anchor로 근사
    for m in _HTML_COMMENT.finditer(text):
        # anchor+note 형식은 이미 처리했으므로 건너뜀
        comment_text = m.group(1).strip()
        if not comment_text or re.search(r"anchor:", comment_text):
            continue
        anchor = _preceding_sentence(text, m.start())
        elements.append(AnnotationElement(
            type="html_comment",
            anchor_text=anchor,
            content=comment_text,
        ))

    # 3) Markdown 링크 타이틀
    for m in _MD_LINK_WITH_TITLE.finditer(text):
        elements.append(AnnotationElement(
            type="md_link_title",
            anchor_text=m.group(1).strip(),
            content=m.group(2).strip(),
        ))

    # 4) GFM callout 블록
    for m in _GFM_CALLOUT.finditer(text):
        callout_type = m.group(1).upper()
        callout_body = re.sub(r"^> ?", "", m.group(2), flags=re.MULTILINE).strip()
        anchor = _preceding_sentence(text, m.start())
        elements.append(AnnotationElement(
            type=f"gfm_{callout_type.lower()}",
            anchor_text=anchor,
            content=callout_body,
        ))

    # 본문: HTML 코멘트 제거
    body_text = _HTML_COMMENT.sub("", text).strip()

    notes = "" if elements else "no known annotation patterns found in text/markdown"
    return StructuredDocument(
        file_path=file_path,
        format=fmt,
        body_text=body_text,
        elements=elements,
        parser_notes=notes,
    )


def _preceding_sentence(text: str, pos: int, max_chars: int = 120) -> str:
    """pos 직전 텍스트에서 마지막 문장(최대 max_chars 글자)을 반환한다."""
    snippet = text[max(0, pos - max_chars) : pos].strip()
    # 마지막 문장 경계
    for sep in (".", "!", "?", "\n"):
        idx = snippet.rfind(sep)
        if idx != -1:
            return snippet[idx + 1 :].strip()
    return snippet
