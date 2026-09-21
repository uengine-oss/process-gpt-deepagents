"""채팅 산출물이 받을 수 있는 것으로 나가는가.

한 시간짜리 토큰 주소를 쓰던 때는, 그 시간이 지나거나 파드가 바뀌면 "만들었습니다"라는
문장과 죽은 링크만 남았다. 지금은 비공개 버킷에 보관하고, 레코드에 `file_id` 를 함께
실어 화면이 새 주소를 받아 갈 수 있게 한다. 이 파일이 지키는 것은 그 약속이다.

경로를 링크로 바꾸는 규칙 자체는 SDK 가 갖는다(processgpt_agent_sdk/tests/test_artifacts.py).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processgpt_agent_sdk.artifacts import StoredArtifact  # noqa: E402

from core.chat.artifacts import publish_chat_artifacts  # noqa: E402
from core.chat.file_links import find_document_paths  # noqa: E402


class FakeStore:
    """보관소 노릇. 무엇이 올라갔는지만 기억한다."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    async def put(self, **kwargs) -> StoredArtifact:
        self.put_calls.append(kwargs)
        name = kwargs["path"].name
        return StoredArtifact(
            file_id=f"artifacts/{name}",
            url=f"https://signed.test/{name}?token=x",
            expires_at="2026-01-01T00:00:00+00:00",
        )

    async def url_for(self, *, tenant_id: str, file_id: str) -> StoredArtifact:
        return StoredArtifact(file_id=file_id, url=f"https://signed.test/reissued/{file_id}")


def publish(text: str, files: dict[str, bytes], store: FakeStore | None = None):
    store = store or FakeStore()

    async def fetch(path: str):
        return files.get(path)

    body, records = asyncio.run(publish_chat_artifacts(
        tenant_id="uengine", conversation_id="room-1", text=text, fetch=fetch, store=store,
    ))
    return body, records, store


def test_경로가_받을_수_있는_주소로_바뀐다():
    text = "작성했습니다.\n파일 경로: /workspace/.bpmn/room/Team Overview.docx"
    body, records, _ = publish(text, {"/workspace/.bpmn/room/Team Overview.docx": b"docx"})

    assert "[Team Overview.docx](https://signed.test/Team Overview.docx?token=x)" in body
    assert "/workspace/.bpmn" not in body
    assert len(records) == 1


def test_레코드가_다시_발급받을_열쇠를_싣는다():
    """주소는 한 시간이면 죽는다. 그때 화면이 집을 것이 레코드에 있어야 한다."""
    body, records, _ = publish(
        "보고서: /workspace/report.pdf", {"/workspace/report.pdf": b"%PDF-"},
    )

    assert records[0]["file_id"] == "artifacts/report.pdf"
    assert records[0]["url_expires_at"] == "2026-01-01T00:00:00+00:00"


def test_꺼내지_못한_파일로는_링크를_만들지_않는다():
    """지어낸 주소를 누르면 아무것도 안 나온다. 그건 경로가 보이는 것보다 나쁘다."""
    body, records, store = publish("파일: /workspace/없는것.docx", {})

    assert records == []
    assert store.put_calls == []
    assert "https://" not in body


def test_한글_문서는_이_갈래를_타지_않는다():
    """hwpx 는 미리보기·편집이 되는 별도 경로가 맡는다. 여기서도 내면 두 번 나온다."""
    assert find_document_paths("/workspace/안내문.hwpx") == []


def test_workspace_밖의_경로는_보지_않는다():
    assert find_document_paths("/etc/passwd.pdf 와 ./local/a.docx") == []


def test_같은_파일이_여러_번_나와도_한_번만_올린다():
    text = "/workspace/a.xlsx 를 만들었습니다. 다시: /workspace/a.xlsx"
    _, records, store = publish(text, {"/workspace/a.xlsx": b"xlsx"})

    assert len(store.put_calls) == 1
    assert len(records) == 1


def test_산출물이_없으면_손대지_않는다():
    text = "특별히 만든 파일은 없습니다."
    body, records, store = publish(text, {})

    assert body == text
    assert records == []
    assert store.put_calls == []
