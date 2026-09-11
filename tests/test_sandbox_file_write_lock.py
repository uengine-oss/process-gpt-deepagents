"""같은 파일에 대한 동시 편집·쓰기가 한 번에 하나씩만 실행되는지 검증한다.

배경: 모델이 한 턴에 같은 SKILL.md 로 edit_file 을 여러 개 내면, 샌드박스 편집 스크립트
("읽기 → 치환 → 'wb' 로 다시 쓰기")가 동시에 돌아 파일이 깨졌다. 깨진 파일은
"is not a text file" 로 편집이 막히고, read_file 이 `file` 블록을 돌려줘 모델 호출이
400 으로 실패하며 대화방을 더 쓸 수 없게 됐다.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.sandbox import docker_sandbox as ds


class _ConcurrencyProbe:
    """부모 클래스 edit/write 를 대신해 동시에 몇 개가 실행 중인지 기록한다."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def __call__(self, *args, **kwargs):
        with self._guard:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)  # 편집 스크립트가 컨테이너에서 도는 시간
        with self._guard:
            self.active -= 1
        return "ok"


@pytest.fixture
def backend():
    b = ds.DockerSandboxBackend.__new__(ds.DockerSandboxBackend)
    b._tenant_id = "acme"
    b._container = None
    b._client = None
    b._lock = threading.Lock()
    return b


def _run_in_threads(fn, count: int) -> None:
    threads = [threading.Thread(target=fn) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_same_file_edits_run_one_at_a_time(backend, monkeypatch):
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(ds.BaseSandbox, "edit", probe)
    path = "/workspace/.checkouts/room-1/office-supply-request-reviewer/SKILL.md"

    _run_in_threads(lambda: backend.edit(path, "old", "new"), 4)

    assert probe.calls == 4
    assert probe.max_active == 1, "같은 파일에 대한 편집이 동시에 실행됐다"


def test_edit_and_write_on_same_file_do_not_overlap(backend, monkeypatch):
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(ds.BaseSandbox, "edit", probe)
    monkeypatch.setattr(ds.BaseSandbox, "write", probe)
    path = "/workspace/.checkouts/room-1/skill/evals/evals.json"

    threads = [
        threading.Thread(target=lambda: backend.edit(path, "a", "b")),
        threading.Thread(target=lambda: backend.write(path, "{}")),
        threading.Thread(target=lambda: backend.edit(path, "c", "d")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert probe.calls == 3
    assert probe.max_active == 1


async def test_async_edits_from_middleware_are_serialized(backend, monkeypatch):
    """파일시스템 미들웨어는 aedit → asyncio.to_thread(self.edit) 로 부른다. 그 경로도 막혀야 한다."""
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(ds.BaseSandbox, "edit", probe)
    path = "/workspace/.checkouts/room-1/skill/SKILL.md"

    results = await asyncio.gather(*(backend.aedit(path, f"o{i}", f"n{i}") for i in range(4)))

    assert results == ["ok"] * 4
    assert probe.max_active == 1


def test_same_path_always_maps_to_same_lock(backend):
    path = "/workspace/.checkouts/room-1/skill/SKILL.md"
    assert backend._file_lock(path) is backend._file_lock(path)
    assert backend._file_lock(path) is backend._file_lock("/workspace/.checkouts/room-1/skill//SKILL.md")
