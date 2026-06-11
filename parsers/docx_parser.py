"""parsers/docx_parser.py — DOCX 파서.

추출 대상:
  - 리뷰 코멘트 (commentRangeStart/End로 anchor_text 정확히 특정)
  - 각주 (footnoteRef 위치로 anchor_text 특정)
  - 미주 (endnoteRef 위치로 anchor_text 특정)
  - 트랙 변경 (del/ins 요소로 삭제/삽입 텍스트 추출)
"""

from __future__ import annotations

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from parsers.protocol import AnnotationElement, StructuredDocument


def parse(file_path: str) -> StructuredDocument:
    doc = Document(file_path)
    notes: list[str] = []

    comments = _extract_comments(doc, notes)
    footnotes = _extract_footnotes(doc, notes)
    endnotes = _extract_endnotes(doc, notes)
    track_changes = _extract_track_changes(doc)
    body_text = _extract_body_text(doc)

    elements = comments + footnotes + endnotes + track_changes
    return StructuredDocument(
        file_path=file_path,
        format="docx",
        body_text=body_text,
        elements=elements,
        parser_notes="; ".join(notes) if notes else "",
    )


# ---------------------------------------------------------------------------
# 본문 텍스트 추출
# ---------------------------------------------------------------------------

def _extract_body_text(doc: Document) -> str:
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


# ---------------------------------------------------------------------------
# 리뷰 코멘트
# ---------------------------------------------------------------------------

def _extract_comments(doc: Document, notes: list[str]) -> list[AnnotationElement]:
    """commentRangeStart/End 쌍으로 anchor_text를 정확히 추출."""
    elements: list[AnnotationElement] = []

    # 1) 코멘트 내용 수집 (id → {author, date, text})
    comment_data: dict[str, dict] = {}
    try:
        comments_part = doc.part.comments
        if comments_part is None:
            return []
        # python-docx의 Comments 객체는 .element 대신 ._comments_elm 사용
        comments_elm = comments_part._comments_elm
        if comments_elm is None:
            return []
        for comment_el in comments_elm.findall(qn("w:comment")):
            cid = comment_el.get(qn("w:id"))
            author = comment_el.get(qn("w:author"), "")
            date = comment_el.get(qn("w:date"), "")
            text = "".join(
                node.text or ""
                for node in comment_el.iter()
                if node.text
            )
            comment_data[cid] = {"author": author, "date": date, "text": text.strip()}
    except Exception as e:
        notes.append(f"comment extraction error: {e}")
        return []

    if not comment_data:
        return []

    # 2) 본문 XML을 순회하며 commentRangeStart~End 사이 텍스트를 anchor로 수집
    body_el = doc.element.body
    open_ranges: dict[str, list[str]] = {}  # id → 수집 중인 텍스트 조각

    for node in body_el.iter():
        tag = node.tag

        if tag == qn("w:commentRangeStart"):
            cid = node.get(qn("w:id"))
            if cid in comment_data:
                open_ranges[cid] = []

        elif tag == qn("w:t") and node.text:
            for cid in open_ranges:
                open_ranges[cid].append(node.text)

        elif tag == qn("w:commentRangeEnd"):
            cid = node.get(qn("w:id"))
            if cid in open_ranges:
                anchor = "".join(open_ranges.pop(cid)).strip()
                data = comment_data[cid]
                elements.append(AnnotationElement(
                    type="review_comment",
                    anchor_text=anchor or "(범위 특정 불가)",
                    content=data["text"],
                    author=data["author"] or None,
                    date=data["date"] or None,
                    extra={"comment_id": cid},
                ))

    return elements


# ---------------------------------------------------------------------------
# 각주
# ---------------------------------------------------------------------------

def _extract_footnotes(doc: Document, notes: list[str]) -> list[AnnotationElement]:
    elements: list[AnnotationElement] = []

    # doc.part.footnotes 속성이 없는 버전 대응 — related_parts에서 partname으로 탐색
    footnotes_xml = _get_part_xml(doc, "footnotes.xml")
    if footnotes_xml is None:
        return []

    # 각주 내용 수집 (id → text)
    footnote_texts: dict[str, str] = {}
    for fn in footnotes_xml.findall(qn("w:footnote")):
        fid = fn.get(qn("w:id"))
        # id 0, -1은 구분자/연속 각주 마커 — 건너뜀
        if fid in ("0", "-1"):
            continue
        text = "".join(node.text or "" for node in fn.iter() if node.text).strip()
        footnote_texts[fid] = text

    if not footnote_texts:
        return []

    # 본문에서 footnoteReference가 있는 run의 앞 텍스트를 anchor로 사용
    for para in doc.paragraphs:
        para_runs = para.runs
        for run_idx, run in enumerate(para_runs):
            fn_refs = run.element.findall(f".//{qn('w:footnoteReference')}")
            for ref in fn_refs:
                fid = ref.get(qn("w:id"))
                if fid not in footnote_texts:
                    continue
                # anchor: 이 run 이전 단어(마지막 non-empty run 텍스트)
                anchor = _prev_run_text(para_runs, run_idx) or para.text.strip()
                elements.append(AnnotationElement(
                    type="footnote",
                    anchor_text=anchor,
                    content=footnote_texts[fid],
                    extra={"footnote_id": fid},
                ))

    return elements


# ---------------------------------------------------------------------------
# 미주
# ---------------------------------------------------------------------------

def _extract_endnotes(doc: Document, notes: list[str]) -> list[AnnotationElement]:
    elements: list[AnnotationElement] = []

    endnotes_xml = _get_part_xml(doc, "endnotes.xml")
    if endnotes_xml is None:
        return []

    endnote_texts: dict[str, str] = {}
    for en in endnotes_xml.findall(qn("w:endnote")):
        eid = en.get(qn("w:id"))
        if eid in ("0", "-1"):
            continue
        text = "".join(node.text or "" for node in en.iter() if node.text).strip()
        endnote_texts[eid] = text

    if not endnote_texts:
        return []

    for para in doc.paragraphs:
        para_runs = para.runs
        for run_idx, run in enumerate(para_runs):
            en_refs = run.element.findall(f".//{qn('w:endnoteReference')}")
            for ref in en_refs:
                eid = ref.get(qn("w:id"))
                if eid not in endnote_texts:
                    continue
                anchor = _prev_run_text(para_runs, run_idx) or para.text.strip()
                elements.append(AnnotationElement(
                    type="endnote",
                    anchor_text=anchor,
                    content=endnote_texts[eid],
                    extra={"endnote_id": eid},
                ))

    return elements


# ---------------------------------------------------------------------------
# 트랙 변경
# ---------------------------------------------------------------------------

def _extract_track_changes(doc: Document) -> list[AnnotationElement]:
    elements: list[AnnotationElement] = []
    body_el = doc.element.body

    for para_el in body_el.iter(qn("w:p")):
        for del_el in para_el.findall(f".//{qn('w:del')}"):
            deleted = "".join(
                node.text or ""
                for node in del_el.iter(qn("w:delText"))
            ).strip()
            if not deleted:
                continue
            # 삽입 쌍 탐색: 같은 단락 내 같은 author/rsidDel의 ins
            author = del_el.get(qn("w:author"), "")
            date = del_el.get(qn("w:date"), "")
            inserted = _find_paired_insertion(para_el, author)

            elements.append(AnnotationElement(
                type="track_deletion",
                anchor_text=deleted,
                content=inserted or "(삽입 텍스트 없음)",
                author=author or None,
                date=date or None,
            ))

    return elements


def _find_paired_insertion(para_el, author: str) -> str:
    """같은 단락 내 같은 author의 w:ins 텍스트를 반환한다."""
    for ins_el in para_el.findall(f".//{qn('w:ins')}"):
        if ins_el.get(qn("w:author"), "") == author:
            text = "".join(
                node.text or ""
                for node in ins_el.iter(qn("w:t"))
            ).strip()
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def _get_part_xml(doc: Document, filename: str):
    """related_parts에서 partname 끝이 filename인 파트의 XML 루트를 반환한다."""
    for part in doc.part.related_parts.values():
        if str(part.partname).endswith(filename):
            return etree.fromstring(part.blob)
    return None


def _prev_run_text(runs, current_idx: int) -> str:
    """current_idx 이전 run 중 마지막 non-empty 텍스트를 반환한다."""
    for run in reversed(runs[:current_idx]):
        text = run.text.strip()
        if text:
            return text
    return ""
