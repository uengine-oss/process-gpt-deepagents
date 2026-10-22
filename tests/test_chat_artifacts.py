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

from core.chat.artifacts import publish_chat_artifacts, publish_one  # noqa: E402
from core.chat.file_links import find_document_paths, find_room_documents  # noqa: E402


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


def publish(
    text: str,
    files: dict[str, bytes],
    store: FakeStore | None = None,
    extra_paths: list[str] | None = None,
):
    store = store or FakeStore()

    async def fetch(path: str):
        return files.get(path)

    body, records = asyncio.run(publish_chat_artifacts(
        tenant_id="uengine", conversation_id="room-1", text=text, fetch=fetch, store=store,
        extra_paths=extra_paths or [],
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


# ── 폼(worklist) 산출물 ────────────────────────────────────────
#
# 채팅만 산출물을 내는 것이 아니다. 예전에는 폼 산출물을 이 서버 메모리(또는 Redis)에
# 담고 한 시간짜리 토큰 주소를 폼값에 박았다 — 결재선을 타고 며칠 뒤에 열어 본 사람은
# 빈손이었다. 지금은 채팅과 같은 규약을 탄다.

def test_폼_산출물도_같은_규약으로_보관된다():
    store = FakeStore()
    record = asyncio.run(publish_one(
        tenant_id="uengine", conversation_id="inst-1",
        file_name="안내문.docx", content=b"docx-bytes", store=store,
    ))

    assert record["file_url"].startswith("https://signed.test/")
    assert record["file_id"] == "artifacts/안내문.docx"
    assert record["url_expires_at"] == "2026-01-01T00:00:00+00:00"
    assert len(store.put_calls) == 1


def test_폼_산출물의_형식_제한도_같다():
    """실행 파일·소스·로그는 결과물이 아니다. 그 판단은 SDK 가 한 군데서 한다."""
    store = FakeStore()
    record = asyncio.run(publish_one(
        tenant_id="uengine", conversation_id="inst-1",
        file_name="run.sh", content=b"#!/bin/sh", store=store,
    ))

    assert record is None
    assert store.put_calls == []


def test_빈_파일은_내주지_않는다():
    store = FakeStore()
    record = asyncio.run(publish_one(
        tenant_id="uengine", conversation_id="inst-1",
        file_name="빈.pdf", content=b"", store=store,
    ))

    assert record is None
    assert store.put_calls == []


# --- 본문이 말하지 않은 문서도 거둔다 -------------------------------------------------
#
# docx·pdf 는 바이너리라 write_file 스트림으로 화면에 뜨지 않는다. 본문에 경로가 적혔을
# 때만 거두면, 모델이 경로를 적지 않은 턴의 문서는 만들어졌다는 말만 남고 받을 길이 없다.


def test_본문에_없는_문서도_작업폴더_경로로_거둔다():
    path = "/workspace/.bpmn/room/보고서.docx"
    body, records, _ = publish("정리해 두었습니다.", {path: b"docx"}, extra_paths=[path])

    assert len(records) == 1
    assert records[0]["file_name"] == "보고서.docx"
    # 작업 폴더가 같은 파일을 알아볼 열쇠.
    assert records[0]["source_path"] == path
    # 본문에 없던 경로를 링크로 끼워 넣지는 않는다.
    assert body == "정리해 두었습니다."


def test_본문과_스캔에_같은_파일이_있어도_한_번만_올라간다():
    path = "/workspace/.bpmn/room/보고서.docx"
    _, records, store = publish(
        f"파일 경로: {path}", {path: b"docx"}, extra_paths=[path],
    )

    assert len(records) == 1
    assert len(store.put_calls) == 1


def test_이름이_같은_파일이_두_곳에_있으면_먼저_것만_낸다():
    first = "/workspace/.bpmn/room/a/보고서.docx"
    second = "/workspace/.bpmn/room/b/보고서.docx"
    _, records, store = publish(
        "완료", {first: b"first", second: b"second"}, extra_paths=[first, second],
    )

    assert len(records) == 1
    assert records[0]["source_path"] == first
    assert len(store.put_calls) == 1


def _touch(path: Path, content: bytes = b"x", mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime_ns is not None:
        import os

        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_작업폴더에서_이번_턴의_문서만_찾는다(tmp_path):
    room = tmp_path / "room"
    _touch(room / "이번턴.docx", mtime_ns=2_000_000_000_000_000_000)
    _touch(room / "지난턴.pdf", mtime_ns=1_000_000_000_000_000_000)
    # 문서가 아닌 것(텍스트 산출물)은 이미 write_file 스트림이 맡는다.
    _touch(room / "process-definition.json", mtime_ns=2_000_000_000_000_000_000)
    # 빈 파일은 받아도 열 수 없다.
    _touch(room / "빈.docx", content=b"", mtime_ns=2_000_000_000_000_000_000)
    # 스킬 평가 실행 산출물은 사용자 산출물이 아니다.
    _touch(room / ".skill-evals" / "run" / "내부.docx", mtime_ns=2_000_000_000_000_000_000)

    found = find_room_documents(
        room, "/workspace/.bpmn/room", since_ns=1_500_000_000_000_000_000,
    )

    assert found == ["/workspace/.bpmn/room/이번턴.docx"]


def test_하위_폴더의_문서도_컨테이너_경로로_찾는다(tmp_path):
    room = tmp_path / "room"
    _touch(room / "outputs" / "안내문.pdf", mtime_ns=2_000_000_000_000_000_000)

    found = find_room_documents(room, "/workspace/.bpmn/room", since_ns=0)

    assert found == ["/workspace/.bpmn/room/outputs/안내문.pdf"]


def test_없는_작업폴더는_빈_목록(tmp_path):
    assert find_room_documents(tmp_path / "없음", "/workspace/.bpmn/room") == []
