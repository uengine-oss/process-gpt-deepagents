"""문서 산출물이 작업 폴더 목록까지 가는가.

docx·pdf 는 바이너리라 `write_file` 스트림(텍스트)을 타지 못한다. 그래서 비공개 버킷에
보관까지 해 두고도 화면의 작업 폴더에서는 통째로 빠져 있었다 — 사용자는 답변 본문의
링크를 찾아내야만 받을 수 있었다. 여기서 지키는 것은 "거둔 문서는 목록에 뜬다" 다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from executor import _document_artifact_payload, _emit_document_artifacts  # noqa: E402


RECORD = {
    "artifact_type": "docx",
    "file_name": "보고서.docx",
    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "file_url": "https://signed.test/보고서.docx?token=x",
    "file_id": "artifacts/abc.docx",
    "url_expires_at": "2026-01-01T00:00:00+00:00",
    "size_bytes": 1234,
    "source_path": "/workspace/.bpmn/room/보고서.docx",
}


class FakeStreamer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def test_작업폴더가_읽는_모양으로_나간다():
    payload = _document_artifact_payload(RECORD)

    assert payload["type"] == "file_artifact"
    assert payload["path"] == "/workspace/.bpmn/room/보고서.docx"
    assert payload["name"] == "보고서.docx"
    assert payload["ext"] == ".docx"
    # 바이너리는 미리보기 내용이 없다 — 주소로 받는다.
    assert payload["content"] == ""
    assert payload["url"] == RECORD["file_url"]
    # 주소는 만료된다. 화면이 새 주소를 받아 갈 열쇠를 함께 싣는다.
    assert payload["file_id"] == "artifacts/abc.docx"
    assert payload["url_expires_at"] == "2026-01-01T00:00:00+00:00"


def test_주소가_없으면_띄우지_않는다():
    assert _document_artifact_payload({**RECORD, "file_url": ""}) is None


def test_거둔_문서를_모두_보낸다():
    streamer = FakeStreamer()
    asyncio.run(_emit_document_artifacts(streamer, [RECORD, {**RECORD, "file_url": ""}]))

    assert [p["name"] for p in streamer.sent] == ["보고서.docx"]


def test_스트리머가_없으면_조용히_넘어간다():
    asyncio.run(_emit_document_artifacts(None, [RECORD]))
