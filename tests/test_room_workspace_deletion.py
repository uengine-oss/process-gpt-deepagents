"""턴에 지운 작업 폴더 파일이 storage 에서도 지워진다(2026-11-04).

운영 Lite 공유 폴더(자료/Docs)에서 문서 하나 수정, 하나 삭제를 시키면 에이전트는 "삭제했어요"라고
답했지만 변경안에는 수정만 들어갔다. 턴 끝 동기화가 있는 파일만 올려, 지운 파일이 storage 에 남아
Lite 가 결과를 거둘 때 그대로 있는 파일로 보였다.
"""
import asyncio

from core.storage import storage


class _Bucket:
    def __init__(self, log):
        self.log = log

    def remove(self, keys):
        self.log.append(("remove", list(keys)))
        return []

    def upload(self, path, file, file_options):
        self.log.append(("upload", path))

    def get_public_url(self, path):
        return path


def _client(log):
    bucket = _Bucket(log)

    class Client:
        class storage:  # noqa: N801
            @staticmethod
            def from_(_):
                return bucket

    return Client()


def _patch(monkeypatch, log):
    monkeypatch.setattr(storage, "_require_tenant_id", lambda: "ohsy")
    monkeypatch.setattr(storage, "_validate_workspace_path", lambda *a: None)
    monkeypatch.setattr(storage, "get_supabase", lambda: _client(log))


def _lite_seed_key(room: str, checkout: str, rel: str) -> str:
    """Lite server/lib/roomStore.js pushRoomDir 가 넣는 키 그대로:
    roomPrefix('/workspace/.bpmn/<room>/<checkout>') + '/' + seedKey(rel),
    seedKey 는 하위 폴더 파일이면 '__u8__' + base64url(rel 전체)."""
    import base64

    raw = base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")
    return f"skills/ohsy/{room}/{checkout}/__u8__{raw}"


def test_removes_the_key_lite_seeded_and_the_key_upload_made(monkeypatch):
    log = []
    _patch(monkeypatch, log)
    rel = "folders/folder-2f3d35/자료/Docs/삭제대상.md"
    storage.remove_workspace_files("room-1", {rel})
    [(op, keys)] = log
    assert op == "remove"
    # 턴 끝 업로드가 만든 키
    assert f"skills/ohsy/room-1/{storage.encode_key_path(rel)}" in keys
    # Lite 가 체크아웃 아래에 넣어 둔 키 — 남으면 변경안에서 삭제가 빠진다
    assert _lite_seed_key("room-1", "folders/folder-2f3d35", "자료/Docs/삭제대상.md") in keys
    assert all(storage.decode_key_path(k.split("/", 3)[3]) == rel for k in keys)


def test_nothing_to_remove_calls_nothing(monkeypatch):
    log = []
    _patch(monkeypatch, log)
    assert storage.remove_workspace_files("room-1", set()).startswith("Removed 0")
    assert log == []


def test_turn_sync_removes_what_the_turn_deleted_before_upload(monkeypatch, tmp_path):
    import executor

    room = tmp_path / "room"
    (room / "folders/docs/자료/Docs").mkdir(parents=True)
    (room / "folders/docs/자료/Docs/견적 조건.md").write_text("검토 메모 추가")
    before = storage.workspace_file_set(room) | {"folders/docs/자료/Docs/지울 문서.md"}
    log = []
    _patch(monkeypatch, log)
    monkeypatch.setattr(executor, "_bpmn_work_dir", lambda: room)
    asyncio.run(executor._sync_room_workspace("ohsy", "room-1", None, before))

    assert log[0][0] == "remove", "지우기가 올리기보다 먼저다"
    removed = [storage.decode_key_path(k.split("/", 3)[3]) for k in log[0][1]]
    assert set(removed) == {"folders/docs/자료/Docs/지울 문서.md"}
    assert [op for op, _ in log[1:]] == ["upload"]


def test_files_not_seen_at_turn_start_are_left_alone(monkeypatch, tmp_path):
    """다른 파드가 만든 파일(이 파드의 턴 시작 목록에 없음)은 지우지 않는다."""
    import executor

    room = tmp_path / "room"
    room.mkdir()
    (room / "a.md").write_text("a")
    log = []
    _patch(monkeypatch, log)
    monkeypatch.setattr(executor, "_bpmn_work_dir", lambda: room)
    asyncio.run(executor._sync_room_workspace("ohsy", "room-1", None, {"a.md"}))
    assert [op for op, _ in log] == ["upload"]
