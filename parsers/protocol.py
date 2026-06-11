"""parsers/protocol.py — 문서 구조화 데이터 모델."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AnnotationElement:
    """본문의 특정 문구에 연결된 주석/부가설명 단위."""

    type: str
    """주석 종류.

    알려진 값:
      review_comment   — 리뷰 코멘트 (말풍선)
      footnote         — 각주
      endnote          — 미주
      track_deletion   — 트랙 변경: 삭제된 텍스트
      track_insertion  — 트랙 변경: 삽입된 텍스트
      pdf_note         — PDF 스티키노트 / 어노테이션
      unknown          — 파서가 분류하지 못한 요소
    """

    anchor_text: str
    """이 주석이 붙은 문구.

    파서가 정확히 특정할 수 있으면 해당 문구, 없으면 주변 컨텍스트 문장.
    """

    content: str
    """주석 내용."""

    author: str | None = None
    date: str | None = None
    extra: dict = field(default_factory=dict)
    """포맷별 추가 메타데이터 (예: comment_id, footnote_number 등)."""


@dataclass
class StructuredDocument:
    """파서가 반환하는 문서 구조화 결과."""

    file_path: str
    format: str
    """파일 포맷: 'docx' | 'pdf' | 'txt' | 'md' | 'unknown'."""

    body_text: str
    """어노테이션을 제외한 본문 텍스트."""

    elements: list[AnnotationElement]
    """발견된 모든 주석 요소 목록."""

    parser_notes: str = ""
    """파서가 추출할 수 없었던 것, 특이사항 등."""

    def to_llm_text(self) -> str:
        """LLM이 읽기 쉬운 텍스트 표현으로 변환한다."""
        lines: list[str] = []

        lines.append(f"=== Document: {self.file_path} ===")
        lines.append(f"Format: {self.format}")
        if self.parser_notes:
            lines.append(f"Parser notes: {self.parser_notes}")
        lines.append("")

        lines.append("--- Body Text ---")
        lines.append(self.body_text.strip() or "(empty)")
        lines.append("")

        if self.elements:
            lines.append(f"--- Annotation Elements ({len(self.elements)} found) ---")
            for i, el in enumerate(self.elements, 1):
                lines.append(f"[{i}] type={el.type}")
                lines.append(f"    anchor : {el.anchor_text!r}")
                lines.append(f"    content: {el.content!r}")
                if el.author:
                    lines.append(f"    author : {el.author}")
                if el.date:
                    lines.append(f"    date   : {el.date}")
                if el.extra:
                    for k, v in el.extra.items():
                        lines.append(f"    {k}: {v}")
        else:
            lines.append("--- Annotation Elements: none found ---")

        return "\n".join(lines)
