"""샌드박스 구간별 시간 상한과 폴백 동작을 검증한다.

배경: 툴 호출 하나가 언제 끝날지 알 수 없으면 대화가 통째로 멈춘다. 매달릴 수 있는
구간은 컨테이너 기동(dind 대기 재시도), 도구 설치(apt 미러 차단), 셸 실행(명령 자체),
설정 조회(git config) 네 곳이다. 여기서는 (1) 각 구간이 상한 안에 결과 또는 사유를
돌려주는지, (2) 자동 설치 재시도가 상한을 몇 배로 늘리지 않는지, (3) 상한을 넘겼을 때
대체 경로로 넘어가며 그 사실을 출력에 남기는지를 본다.
"""

from __future__ import annotations

import time

import docker.errors
import pytest

from core.sandbox import docker_sandbox as ds


class _HangingContainer:
    """exec_run 이 돌아오지 않는 컨테이너."""

    def exec_run(self, cmd, demux=False):
        time.sleep(30)
        return 0, b""


class _ScriptedContainer:
    """명령별로 정해진 응답을 주는 컨테이너. 'install' 응답은 매달리게 할 수 있다."""

    def __init__(self, *, missing: str = "pandoc", hang_install: bool = False):
        self.missing = missing
        self.hang_install = hang_install
        self.commands: list[str] = []

    def exec_run(self, cmd, demux=False):
        script = cmd[-1]
        self.commands.append(script)
        if "apt-get install" in script:
            if self.hang_install:
                time.sleep(30)
            return 0, b"done"
        return 127, f"bash: line 1: {self.missing}: command not found".encode()


@pytest.fixture
def backend():
    b = ds.DockerSandboxBackend.__new__(ds.DockerSandboxBackend)
    b._tenant_id = "acme"
    b._container = None
    b._client = None
    import threading

    b._lock = threading.Lock()
    return b


# ---------------------------------------------------------------------------
# 셸 실행
# ---------------------------------------------------------------------------

def test_shell_execution_returns_reason_within_limit(backend, monkeypatch):
    """끝나지 않는 명령도 상한 안에서 사유를 붙여 돌아온다."""
    monkeypatch.setattr(backend, "_ensure_container", lambda **kw: _HangingContainer())

    started = time.monotonic()
    resp = backend.execute("sleep 300", timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 10, f"상한(1초)을 한참 넘겨 돌아왔다: {elapsed:.1f}s"
    assert resp.exit_code == ds.TIMEOUT_EXIT_CODE
    assert "timed out after 1s" in resp.output


def test_unavailable_sandbox_is_reported_not_raised(backend, monkeypatch):
    """컨테이너를 못 얻으면 예외가 아니라 사유가 적힌 결과로 돌려준다."""

    def _boom(**kw):
        raise ds.SandboxUnavailable("기동 상한 초과")

    monkeypatch.setattr(backend, "_ensure_container", _boom)

    resp = backend.execute("echo hi")
    assert resp.exit_code == ds.UNAVAILABLE_EXIT_CODE
    assert "기동 상한 초과" in resp.output


# ---------------------------------------------------------------------------
# 도구 설치
# ---------------------------------------------------------------------------

def test_install_timeout_falls_back_and_says_so(backend, monkeypatch):
    """설치가 상한을 넘기면 설치를 포기하고, 그 사실을 출력에 남긴다."""
    container = _ScriptedContainer(hang_install=True)
    monkeypatch.setattr(backend, "_ensure_container", lambda **kw: container)

    started = time.monotonic()
    resp = backend.execute("pandoc --version", timeout=2)
    elapsed = time.monotonic() - started

    # 총 소요가 상한 근처에 머문다 — 재시도마다 상한을 새로 주면 몇 배로 늘어난다.
    assert elapsed < 10, f"자동 설치 재시도가 상한을 몇 배로 늘렸다: {elapsed:.1f}s"
    assert "자동 설치" in resp.output and "상한" in resp.output
    assert "command not found" in resp.output, "폴백 후 원래 결과를 그대로 돌려줘야 한다"


def test_install_success_retries_the_command(backend, monkeypatch):
    """설치가 상한 안에 끝나면 원래 명령을 한 번 더 실행한다(기존 동작 유지)."""
    container = _ScriptedContainer(hang_install=False)
    monkeypatch.setattr(backend, "_ensure_container", lambda **kw: container)

    backend.execute("pandoc --version", timeout=30)

    assert any("apt-get install" in c for c in container.commands)
    assert sum("pandoc --version" in c for c in container.commands) == 2


# ---------------------------------------------------------------------------
# 컨테이너 기동
# ---------------------------------------------------------------------------

def test_container_start_gives_up_at_deadline(backend, monkeypatch):
    """기동 재시도는 횟수가 아니라 남은 시간으로 끊는다."""

    def _never_ready():
        raise docker.errors.DockerException("dind not ready")

    monkeypatch.setattr(backend, "_start_container", _never_ready)
    monkeypatch.setattr(ds, "_get_docker_client", lambda: object())

    started = time.monotonic()
    with pytest.raises(ds.SandboxUnavailable):
        backend._ensure_container(deadline=time.monotonic() + 0.5)
    assert time.monotonic() - started < 5


def test_container_lock_wait_is_bounded(backend, monkeypatch):
    """다른 실행이 컨테이너를 잡고 있어도 상한 안에 포기한다."""
    backend._lock.acquire()
    try:
        with pytest.raises(ds.SandboxUnavailable):
            backend._ensure_container(deadline=time.monotonic() + 0.3)
    finally:
        backend._lock.release()


# ---------------------------------------------------------------------------
# 설정 조회
# ---------------------------------------------------------------------------

def test_git_config_step_is_bounded(monkeypatch):
    """설정 적용이 매달려도 기동 경로를 잡아 두지 않는다."""
    monkeypatch.setattr(ds, "SANDBOX_CONFIG_TIMEOUT", 1)

    started = time.monotonic()
    ds.DockerSandboxBackend._configure_git(_HangingContainer())
    assert time.monotonic() - started < 5
