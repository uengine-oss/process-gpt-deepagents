"""parsers/pdf_parser.py — PDF 파서.

pymupdf(fitz)로 어노테이션 레이어를 추출한다.
anchor_text는 어노테이션이 덮는 영역의 텍스트를 근사 추출한다.
"""

from __future__ import annotations

from parsers.protocol import AnnotationElement, StructuredDocument

# PDF 어노테이션 타입 코드 → 사람이 읽을 수 있는 이름
_ANNOT_TYPE_MAP: dict[int, str] = {
    0: "text_note",       # sticky note
    1: "link",
    4: "free_text",       # 텍스트 상자
    8: "highlight",
    9: "underline",
    10: "squiggly",
    11: "strikethrough",
    12: "redact",
}


def parse(file_path: str) -> StructuredDocument:
    try:
        import fitz  # pymupdf
    except ImportError:
        return StructuredDocument(
            file_path=file_path,
            format="pdf",
            body_text="",
            elements=[],
            parser_notes="pymupdf not installed; PDF annotation extraction unavailable",
        )

    doc = fitz.open(file_path)
    body_parts: list[str] = []
    elements: list[AnnotationElement] = []
    notes: list[str] = []

    for page in doc:
        body_parts.append(page.get_text("text"))

        for annot in page.annots():
            annot_type_code = annot.type[0]
            annot_type_name = _ANNOT_TYPE_MAP.get(annot_type_code, f"type_{annot_type_code}")

            # 어노테이션 내용
            content = (annot.info.get("content") or annot.info.get("title") or "").strip()
            if not content:
                continue  # 내용 없는 어노테이션은 건너뜀

            # anchor_text: 어노테이션 rect 영역의 텍스트를 추출
            anchor_text = _extract_anchor_text(page, annot)

            author = annot.info.get("title") or None  # PDF에서 author는 title 필드
            if author == content:
                author = None  # title이 content와 같으면 실제 author가 아님

            elements.append(AnnotationElement(
                type=f"pdf_{annot_type_name}",
                anchor_text=anchor_text or "(위치 특정 불가)",
                content=content,
                author=author,
                date=annot.info.get("creationDate") or annot.info.get("modDate") or None,
                extra={
                    "page": page.number + 1,
                    "rect": list(annot.rect),
                },
            ))

    doc.close()

    if not elements:
        notes.append("no annotatable elements found in PDF")

    return StructuredDocument(
        file_path=file_path,
        format="pdf",
        body_text="\n".join(body_parts).strip(),
        elements=elements,
        parser_notes="; ".join(notes) if notes else "",
    )


def _extract_anchor_text(page, annot) -> str:
    """어노테이션 rect 또는 quad_points 영역의 텍스트를 추출한다."""
    try:
        # highlight/underline 등은 quad_points로 정확한 범위를 가짐
        quads = annot.vertices
        if quads:
            import fitz
            quad_list = [fitz.Quad(quads[i:i+4]) for i in range(0, len(quads), 4)]
            texts = [page.get_textbox(q.rect).strip() for q in quad_list]
            combined = " ".join(t for t in texts if t)
            if combined:
                return combined

        # 없으면 rect 영역 전체 텍스트
        text = page.get_textbox(annot.rect).strip()
        if text:
            return text
    except Exception:
        pass
    return ""
