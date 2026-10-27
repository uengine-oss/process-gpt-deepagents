"""인계 산출물 워크트리 — 에이전트 작업 폴더 펼치기·턴 끝 커밋(2026-10-26).

받은 방 에이전트는 방 브랜치 head 를 작업 폴더로 펼쳐 일하고, 턴이 끝나면 바뀐 경로만
커밋한다. 바뀐 것이 없으면 커밋하지 않고, head 가 그새 바뀌었으면 최신 head 위에 다시 얹는다.
브랜치가 없는 방은 None 을 돌려 기존 방 폴더 동기화를 쓰게 한다.
"""
import hashlib

import pytest

from core.storage import worktree


def _entry(data: bytes, path: str = "x.txt") -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "content_type": worktree._content_type(path)}


class FakeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Result:
    def __init__(self, data):
        self.data = data


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters = db, table, {}

    def select(self, *_):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def limit(self, _):
        return self

    def execute(self):
        rows = self.db.tables[self.table]
        return Result([r for r in rows if all(r.get(k) == v for k, v in self.filters.items())])


class Bucket:
    def __init__(self, db):
        self.db = db

    def upload(self, path, file, file_options=None):
        if path in self.db.objects:
            raise Exception("409 Duplicate: The resource already exists")
        self.db.objects[path] = file
        self.db.uploads.append(path)

    def download(self, key):
        return self.db.objects[key]

    def list(self, prefix):
        names = {}
        for key, data in self.db.objects.items():
            if not key.startswith(prefix + "/"):
                continue
            rest = key[len(prefix) + 1:]
            head, _, tail = rest.partition("/")
            names[head] = {"name": head, "id": None} if tail else {"name": head, "id": "1", "updated_at": self.db.folder_times.get(key)}
        return list(names.values())


class Storage:
    def __init__(self, db):
        self.db = db

    def from_(self, _):
        return Bucket(self.db)


class Rpc:
    def __init__(self, db, name, args):
        self.db, self.name, self.args = db, name, args

    def execute(self):
        self.db.rpcs.append(self.args)
        if self.db.move_head_once:
            # 같은 방에서 사람이 그새 커밋했다.
            self.db.move_head_once = False
            self.db.add_commit("h2", {**self.db.head_tree(), "사람.txt": _entry(b"human")})
            raise FakeError("40001", "artifact: head 가 바뀌었습니다")
        branch = self.db.tables["artifact_branches"][0]
        if branch["head_commit_id"] != self.args["p_expected_head"]:
            raise FakeError("40001", "artifact: head 가 바뀌었습니다")
        if self.db.head_tree() == self.args["p_tree"]:
            return Result({"commit_id": branch["head_commit_id"], "created": False})
        self.db.add_commit(f"c{len(self.db.rpcs)}", self.args["p_tree"])
        return Result({"commit_id": branch["head_commit_id"], "created": True})


class FakeDb:
    def __init__(self, tree: dict | None, contents: dict[str, bytes]):
        self.objects = {worktree.object_key("t", hashlib.sha256(d).hexdigest()): d for d in contents.values()}
        self.uploads: list[str] = []
        self.rpcs: list[dict] = []
        self.folder_times: dict[str, str] = {}
        self.move_head_once = False
        self.tables = {"artifact_branches": [], "artifact_commits": []}
        self.storage = Storage(self)
        if tree is not None:
            self.tables["artifact_branches"].append({"room_id": "room", "head_commit_id": "h1"})
            self.tables["artifact_commits"].append({"id": "h1", "tree": tree, "created_at": "2026-10-26T01:00:00+00:00"})

    def head_tree(self):
        head = self.tables["artifact_branches"][0]["head_commit_id"]
        return next(c["tree"] for c in self.tables["artifact_commits"] if c["id"] == head)

    def add_commit(self, cid, tree):
        self.tables["artifact_commits"].append({"id": cid, "tree": tree, "created_at": "2026-10-26T02:00:00+00:00"})
        self.tables["artifact_branches"][0]["head_commit_id"] = cid

    def table(self, name):
        return Query(self, name)

    def rpc(self, name, args):
        return Rpc(self, name, args)


@pytest.fixture
def handed_over():
    est, cost = "견적서 v1".encode(), b"cost v1"
    tree = {"견적서.md": _entry(est, "견적서.md"), "data/원가표.csv": _entry(cost, "data/원가표.csv")}
    return FakeDb(tree, {"est": est, "cost": cost}), tree


def test_room_without_branch_uses_old_path(tmp_path):
    assert worktree.materialize("t", "room", tmp_path, client=FakeDb(None, {})) is None


def test_materialize_puts_handed_over_files_in_the_work_folder(tmp_path, handed_over):
    db, tree = handed_over
    (tmp_path / "남은파일.txt").write_text("이전 파드 잔여")
    state = worktree.materialize("t", "room", tmp_path, client=db)
    assert state.head_commit_id == "h1" and state.tree == tree
    assert (tmp_path / "견적서.md").read_text() == "견적서 v1"
    assert (tmp_path / "data" / "원가표.csv").read_bytes() == b"cost v1"
    assert not (tmp_path / "남은파일.txt").exists(), "head 에 없는 파일은 지운다"


def test_no_change_makes_no_commit(tmp_path, handed_over):
    db, _ = handed_over
    state = worktree.materialize("t", "room", tmp_path, client=db)
    res = worktree.commit_turn("t", state, tmp_path, client=db)
    assert res == {"commit_id": "h1", "created": False}
    assert db.rpcs == [] and db.uploads == []


def test_changed_file_becomes_a_new_commit_with_only_new_objects(tmp_path, handed_over):
    db, tree = handed_over
    state = worktree.materialize("t", "room", tmp_path, client=db)
    (tmp_path / "견적서.md").write_text("견적서 v2")
    (tmp_path / "견적서.docx").write_bytes(b"docx")
    res = worktree.commit_turn("t", state, tmp_path, client=db)
    assert res["created"] is True
    new_tree = db.head_tree()
    assert new_tree["견적서.md"]["sha256"] == hashlib.sha256("견적서 v2".encode()).hexdigest()
    assert new_tree["data/원가표.csv"] == tree["data/원가표.csv"]
    assert "견적서.docx" in new_tree
    assert len(db.uploads) == 2, "바뀐 파일만 올린다"


def test_moved_head_retries_on_top_of_latest(tmp_path, handed_over):
    db, _ = handed_over
    state = worktree.materialize("t", "room", tmp_path, client=db)
    (tmp_path / "견적서.md").write_text("에이전트 고침")
    db.move_head_once = True
    res = worktree.commit_turn("t", state, tmp_path, client=db)
    assert res["created"] is True
    assert len(db.rpcs) == 2 and db.rpcs[1]["p_expected_head"] == "h2"
    final = db.head_tree()
    assert "사람.txt" in final, "그새 들어온 사람 커밋을 잃지 않는다"
    assert final["견적서.md"]["sha256"] == hashlib.sha256("에이전트 고침".encode()).hexdigest()


def test_files_uploaded_to_room_folder_after_head_are_laid_on_top(tmp_path, handed_over):
    db, _ = handed_over
    from core.storage.storage import encode_key_path

    newer = f"skills/t/room/{encode_key_path('사람이올림.txt')}"
    older = "skills/t/room/old.txt"
    db.objects[newer], db.objects[older] = "사람 파일".encode(), b"legacy"
    db.folder_times[newer], db.folder_times[older] = "2026-10-26T03:00:00Z", "2026-10-25T00:00:00Z"
    state = worktree.materialize("t", "room", tmp_path, client=db)
    assert (tmp_path / "사람이올림.txt").read_text() == "사람 파일"
    assert not (tmp_path / "old.txt").exists(), "head 이전 방 폴더 파일은 이미 반영된 것이다"
    res = worktree.commit_turn("t", state, tmp_path, client=db)
    assert res["created"] is True and "사람이올림.txt" in db.head_tree()


def test_agent_cannot_write_into_an_earlier_rooms_folder(monkeypatch):
    """인계받은 방 에이전트가 앞선 대화에 남은 앞 방 경로에 쓰면 그 결과는 어느 브랜치에도 남지 않는다.

    2026-10-26 e2e: 이팀장 방 에이전트가 /workspace/.bpmn/<홍길동 방>/견적서.md 를 고쳐
    '고쳤다'고 답했지만 이팀장 브랜치에는 커밋되지 않았다. 다른 방 폴더 쓰기는 막고 이번 방 폴더를 알려 준다.
    """
    import threading

    from core.sandbox import docker_sandbox as ds

    b = ds.DockerSandboxBackend.__new__(ds.DockerSandboxBackend)
    b._tenant_id, b._container, b._client, b._lock = "acme", None, None, threading.Lock()
    monkeypatch.setattr(ds.BaseSandbox, "write", lambda self, p, c: ds.WriteResult(path=p))
    monkeypatch.setattr(ds.BaseSandbox, "edit", lambda self, p, o, n, r=False: ds.EditResult(path=p, occurrences=1))
    ds.set_workspace_scope("/workspace/.bpmn/lee-room", "/workspace/.checkouts/lee-room")
    try:
        denied = b.edit("/workspace/.bpmn/hong-room/견적서.md", "3,000", "2,700")
        assert denied.error and "/workspace/.bpmn/lee-room" in denied.error
        assert b.write("/workspace/.bpmn/hong-room/new.md", "x").error
        assert b.edit("/workspace/.bpmn/lee-room/견적서.md", "3,000", "2,700").error is None
        assert b.write("/workspace/.checkouts/lee-room/skill/SKILL.md", "x").error is None
        assert b.write("/workspace/uploads/memo.md", "x").error is None, "방과 무관한 공용 영역은 그대로"
    finally:
        ds.set_workspace_scope()
