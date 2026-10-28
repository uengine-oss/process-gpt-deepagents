"""memento 문서 검색·페이지 읽기가 지금의 memento 인자(path)도 보내는지."""
import asyncio

from core.rag import memento


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"hits": [], "total_matches": 0, "truncated": False, "file_name": "가격표.pdf", "pages": []}


class _Client:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or [])))
        return _Resp()


def test_grep_and_page_send_path(monkeypatch):
    client = _Client()
    monkeypatch.setattr(memento, "_get_client", lambda: client)
    asyncio.run(memento.grep_document(tenant_id="t1", file_name="가격표.pdf", pattern="의자"))
    asyncio.run(memento.read_pages(tenant_id="t1", file_name="가격표.pdf", pages="1-2"))
    (grep_url, grep_params), (page_url, page_params) = client.calls
    assert grep_url == "/document/grep" and grep_params["path"] == "가격표.pdf" and grep_params["file_name"] == "가격표.pdf"
    assert page_url == "/document/page" and page_params["path"] == "가격표.pdf" and page_params["pages"] == "1-2"
