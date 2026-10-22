"""샌드박스 → 원격 Git 경로가 막힌 환경에서 툴이 매달리지 않는지 검증한다.

배경: 샌드박스 컨테이너의 MTU 가 경로 MTU 보다 크면 DNS·TCP 핸드셰이크는 되는데 큰
응답(TLS 인증서, git pack)만 드롭돼, git 이 TCP 리셋까지 150초 넘게 조용히 매달린다.
그 결과 checkout_to_workspace 는 로컬 사본으로 폴백하기까지 매번 2~3분을, publish_skill
은 fetch+push 로 5분을 날렸다. 여기서는 (1) 원격 git 명령에 상한이 걸리는지,
(2) 한 번 차단으로 판정되면 이후 호출이 원격을 건너뛰는지, (3) 단순 실패는 차단으로
오판하지 않는지, (4) 이름만으로 부른 체크아웃이 로컬 스킬을 제대로 찾는지를 본다.
"""

from __future__ import annotations

import pytest

from core.skills import tools as skill_tools


class _FakeSandbox:
    """_git_net_blocked/_mark_git_net_blocked 가 상태를 붙일 대상."""


@pytest.fixture
def sandbox(monkeypatch):
    sb = _FakeSandbox()
    token = skill_tools._current_sandbox.set(sb)
    yield sb
    skill_tools._current_sandbox.reset(token)


def _provider(exists=True):
    return type(
        "P", (), {
            "username": "o",
            "repo_exists": lambda self, n: ({"name": n} if exists else None),
            "get_authenticated_clone_url": lambda self, o, n: f"https://t@h/{o}/{n}.git",
        },
    )()


def test_clone_runs_with_timeout_and_low_speed_abort(sandbox, monkeypatch):
    """클론은 시간 상한과 저속 중단 옵션을 걸고 나가야 한다."""
    seen: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        skill_tools, "_sandbox_exec",
        lambda cmd, **kw: (seen.append((cmd, kw.get("timeout"))), (0, ""))[1],
    )

    assert skill_tools._clone_sb("https://t@h/o/s.git", "/workspace/s") == (0, "")
    cmd, timeout = seen[0]
    assert "http.lowSpeedLimit" in cmd and "http.lowSpeedTime" in cmd, cmd
    assert timeout == skill_tools._GIT_NET_TIMEOUT


def test_network_failure_short_circuits_later_remote_calls(sandbox, monkeypatch):
    """한 번 접속 불가로 판정되면 이후 클론은 샌드박스를 아예 건드리지 않는다."""
    calls: list[str] = []

    def exec_(cmd, **kw):
        calls.append(cmd)
        return 1, "fatal: unable to access 'https://h/o/s.git/': Recv failure: Connection reset by peer"

    monkeypatch.setattr(skill_tools, "_sandbox_exec", exec_)
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: _provider())

    assert skill_tools._try_clone_skill_repo("s", "/workspace/s") is None
    assert len(calls) == 1

    # 두 번째 호출은 원격 조회도 클론도 하지 않고 즉시 폴백한다.
    assert skill_tools._try_clone_skill_repo("s", "/workspace/s") is None
    assert len(calls) == 1, calls


@pytest.mark.parametrize("stderr", [
    "fatal: repository not found",
    # git 은 권한 오류에도 "unable to access" 를 붙인다 — 이걸 전송 실패로 오인하면
    # 토큰이 틀렸을 뿐인데 원격 작업 전체가 몇 분간 막힌다.
    "fatal: unable to access 'https://h/o/s.git/': The requested URL returned error: 403",
])
def test_ordinary_clone_failure_does_not_block_remote(sandbox, monkeypatch, stderr):
    """네트워크 장애가 아닌 실패(권한/없는 레포)는 차단으로 판정하지 않는다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (1, stderr))
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: _provider())

    assert skill_tools._try_clone_skill_repo("s", "/workspace/s") is None
    assert not skill_tools._git_net_blocked()


def test_push_aborts_immediately_when_remote_unreachable(sandbox, monkeypatch):
    """원격이 막힌 상태의 publish 는 push 까지 가지 않고 바로 알린다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (0, ""))
    monkeypatch.setattr(skill_tools, "_git_sb", lambda cwd, *a: (0, ""))
    skill_tools._mark_git_net_blocked("Recv failure: Connection reset by peer")

    pushed: list[tuple] = []
    real_net_sb = skill_tools._git_net_sb

    def net_sb(cwd, *args):
        if args[:1] == ("push",):
            pushed.append(args)
        return real_net_sb(cwd, *args)

    monkeypatch.setattr(skill_tools, "_git_net_sb", net_sb)

    result = skill_tools._push_skill_sandbox(
        "/workspace/s", "s", "main", "https://t@h/o/s.git", "https://h/o/s",
        existing_repo=True,
    )
    assert result.startswith("ERROR git fetch:")
    assert not pushed, "원격이 막혔는데 push 까지 진행했다"


# ---------------------------------------------------------------------------
# 이름만 준 체크아웃이 로컬 스킬을 찾는가
# ---------------------------------------------------------------------------

def test_bare_name_finds_skill_under_provider_subdir(sandbox, monkeypatch):
    """스킬은 {tenant}/{provider_config_id|local}/{name} 에 있다 — 한 단계 아래까지 훑는다."""
    found_path = "/skills/acme/local/nda-review"
    calls: list[str] = []

    def exec_(cmd, **kw):
        calls.append(cmd)
        if cmd.startswith("for d in "):
            return 0, found_path
        return 0, ""

    monkeypatch.setattr(skill_tools, "_sandbox_exec", exec_)
    monkeypatch.setattr(skill_tools, "_try_clone_skill_repo", lambda name, dest: None)
    token = skill_tools._current_tenant_id.set("acme")
    try:
        assert skill_tools.checkout_to_workspace.func("nda-review") == "/workspace/nda-review"
    finally:
        skill_tools._current_tenant_id.reset(token)

    probe = calls[0]
    assert "/skills/acme/*/nda-review" in probe, probe
    assert any(found_path in c and "cp -r" in c for c in calls), calls
