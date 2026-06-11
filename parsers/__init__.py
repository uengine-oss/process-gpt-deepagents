"""parsers — 문서 구조화 추출 패키지.

포맷별 파서를 디스패치하고 StructuredDocument를 반환한다.
새 포맷 지원은 parsers/ 아래에 <format>_parser.py를 추가하고
_DISPATCH에 등록하면 된다.
"""

from __future__ import annotations

from pathlib import Path

from parsers import docx_parser, fallback, pdf_parser, text_parser
from parsers.protocol import StructuredDocument

_DISPATCH = {
    ".docx": docx_parser.parse,
    ".pdf": pdf_parser.parse,
    ".txt": text_parser.parse,
    ".md": text_parser.parse,
    ".markdown": text_parser.parse,
}


def extract(file_path: str) -> StructuredDocument:
    """파일 경로를 받아 포맷에 맞는 파서로 StructuredDocument를 반환한다."""
    suffix = Path(file_path).suffix.lower()
    parser_fn = _DISPATCH.get(suffix, fallback.parse)
    return parser_fn(file_path)


__all__ = ["extract", "StructuredDocument"]
