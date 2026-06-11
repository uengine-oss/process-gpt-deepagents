"""parsers/fallback.py — 미지원 포맷 폴백 파서.

원시 텍스트만 반환하고 parser_notes에 지원 불가 사유를 기록한다.
LLM이 이 출력을 보고 페어를 스스로 판단해야 한다.
"""

from __future__ import annotations

from pathlib import Path

from parsers.protocol import AnnotationElement, StructuredDocument


def parse(file_path: str) -> StructuredDocument:
    suffix = Path(file_path).suffix.lower()
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        text = ""
        note = f"read error: {e}"
    else:
        note = (
            f"format '{suffix}' has no dedicated parser; "
            "annotation extraction unavailable — body text only. "
            "LLM should identify annotation pairs from context."
        )

    return StructuredDocument(
        file_path=file_path,
        format="unknown",
        body_text=text.strip(),
        elements=[],
        parser_notes=note,
    )
