"""선택한 자료 목록(list_documents)이 워드 양식의 file_id 를 알려 주는지.

배경: 에이전트에 office-mcp(generate_docx)를 붙이고 지식 베이스에서 가격표와 견적서 양식(.docx)을 골라
「양식을 채워 워드 파일로」를 시키면, 모델은 카탈로그에서 양식의 이름만 보고 「양식에 접근할 수 없다」며 멈췄다.
generate_docx 는 template_file_id 로 그 자료의 file_id(=스토리지 경로)를 받는데, 카탈로그 어디에도 없었다.
여기서는 (1) 카드에 file_id 가 실리는지, (2) 워드 자료만 양식 목록에 file_id 와 함께 나오는지 본다.
"""

from __future__ import annotations

import asyncio

from core.rag import memento
from core.rag import tools as rag_tools

PRICE = memento.DocCard(file_name="Hanbit-Office-Price-List.pdf",
                        file_id="knowledge/acme/Sales/1.pdf", mime_type="application/pdf")
TEMPLATE = memento.DocCard(file_name="Hanbit-Quote-Template.docx",
                           file_id="knowledge/acme/Templates/2.docx")


def test_card_markdown_carries_file_id():
    md = rag_tools._format_doc_card_md(TEMPLATE)
    assert "file_id: knowledge/acme/Templates/2.docx" in md


def test_list_documents_points_generate_docx_at_the_word_template(monkeypatch):
    async def fake_catalog(*, tenant_id, file_ids=None, file_names=None):
        return [PRICE, TEMPLATE]

    monkeypatch.setattr(memento, "list_catalog", fake_catalog)
    config = {"configurable": {"tenant_id": "acme", "knowledge_doc_ids": [PRICE.file_id, TEMPLATE.file_id]}}
    cmd = asyncio.run(rag_tools.list_documents.coroutine(state={"files": {}}, config=config, tool_call_id="t1"))
    text = cmd.update["messages"][0].content
    assert "generate_docx" in text and "template_file_id" in text
    assert "Hanbit-Quote-Template.docx → file_id: knowledge/acme/Templates/2.docx" in text
    assert "1.pdf" not in text.split("template_file_id", 1)[1]  # PDF 는 양식 목록에 넣지 않는다
