"""체크아웃이 대화방 단위로 격리되는지 검증한다.

샌드박스 컨테이너와 /workspace 볼륨은 테넌트 단위로 공유된다. 체크아웃을
/workspace/<skill-name> 에 두면 같은 테넌트의 두 사용자가 같은 스킬을 동시에 편집할 때
서로의 작업 디렉터리를 `rm -rf` 하고 덮어쓴다. 여기서는 (1) 방마다 다른 경로가 나오는지,
(2) 방 id 가 없는 호출부는 종전 경로를 그대로 쓰는지, (3) ls/read 스코프 가드가 남의 방
체크아웃을 막고 내 방 것은 열어 주는지를 본다.
"""

from __future__ import annotations

import pytest

from core.sandbox import docker_sandbox as ds
from core.skills import tools as skill_tools

ROOM_A = "11111111-2222-3333-4444-555555555555"
ROOM_B = "66666666-7777-8888-9999-000000000000"


class _FakeSandbox:
    pass


@pytest.fixture
def sandbox(monkeypatch):
    sb = _FakeSandbox()
    token = skill_tools._current_sandbox.set(sb)
    yield sb
    skill_tools._current_sandbox.reset(token)


@pytest.fixture
def exec_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        skill_tools, "_sandbox_exec",
        lambda cmd, **kw: (calls.append(cmd), (0, ""))[1],
    )
    return calls


def _in_room(room: str):
    return skill_tools._current_conversation_id.set(room)


def test_same_skill_in_two_rooms_gets_two_paths(sandbox, exec_calls):
    """같은 스킬이라도 방이 다르면 작업 디렉터리가 겹치지 않는다."""
    paths = []
    for room in (ROOM_A, ROOM_B):
        token = _in_room(room)
        try:
            paths.append(skill_tools._checkout_dest("nda-review"))
        finally:
            skill_tools._current_conversation_id.reset(token)

    assert paths[0] != paths[1]
    assert paths[0] == f"/workspace/.checkouts/{ROOM_A}/nda-review"
    assert paths[1] == f"/workspace/.checkouts/{ROOM_B}/nda-review"
    # 부모 디렉터리를 만들어 두지 않으면 cp/clone 이 실패한다.
    assert any("mkdir -p" in c for c in exec_calls), exec_calls


def test_checkout_returns_room_scoped_path(sandbox, monkeypatch):
    """checkout_to_workspace 가 방 전용 경로를 돌려준다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (0, ""))
    monkeypatch.setattr(skill_tools, "_try_clone_skill_repo", lambda name, dest: None)
    token = _in_room(ROOM_A)
    try:
        out = skill_tools.checkout_to_workspace.func("/skills/acme/local/nda-review")
    finally:
        skill_tools._current_conversation_id.reset(token)

    assert out == f"/workspace/.checkouts/{ROOM_A}/nda-review"


def test_without_room_id_keeps_legacy_path(sandbox, exec_calls):
    """방 id 가 없는 호출부(레거시 헬퍼 등)는 종전대로 /workspace 바로 아래를 쓴다."""
    token = _in_room("")
    try:
        assert skill_tools._checkout_dest("nda-review") == "/workspace/nda-review"
    finally:
        skill_tools._current_conversation_id.reset(token)
    assert not exec_calls, "격리가 없는 경로에서는 준비 작업도 하지 않는다"


# ---------------------------------------------------------------------------
# 읽기 스코프 가드
# ---------------------------------------------------------------------------

def test_checkout_tree_is_run_scoped():
    assert ds._is_run_scoped_path(f"/workspace/.checkouts/{ROOM_A}/nda-review/SKILL.md")
    # 방과 무관한 공용 영역은 여전히 제한 없음
    assert not ds._is_run_scoped_path("/workspace/uploads/contract.pdf")


def test_scope_allows_own_room_and_denies_other_room():
    """다른 방의 체크아웃을 절대경로로 찍어 읽는 것을 막는다."""
    backend = ds.DockerSandboxBackend.__new__(ds.DockerSandboxBackend)
    token = ds._workspace_scope.set((f"/workspace/.checkouts/{ROOM_A}",))
    try:
        assert backend._in_workspace_scope(f"/workspace/.checkouts/{ROOM_A}/nda-review/SKILL.md")
        assert not backend._in_workspace_scope(f"/workspace/.checkouts/{ROOM_B}/nda-review/SKILL.md")
    finally:
        ds._workspace_scope.reset(token)
