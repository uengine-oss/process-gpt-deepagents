"""스킬 도구가 상한 안에 결과를 돌려주고, 폴백 사실을 사용자에게 알리는지 검증한다.

DoD:
  - 도구 호출은 정해진 상한 안에 결과 또는 실패 사유를 돌려준다.
  - 상한을 넘기면 대체 경로(로컬 사본, git 없이 진행)로 폴백하고 그 사실을 알린다.
  - 병합 요청 기록에 실패하면 발행 결과에 그 사실이 표시된다.
"""

from __future__ import annotations

import time

import pytest

from core.skills import tools as skill_tools


class _FakeSandbox:
    """_git_net_blocked 등이 상태를 붙일 대상."""


@pytest.fixture
def sandbox():
    sb = _FakeSandbox()
    token = skill_tools._current_sandbox.set(sb)
    yield sb
    skill_tools._current_sandbox.reset(token)


@pytest.fixture
def tenant():
    token = skill_tools._current_tenant_id.set("acme")
    yield "acme"
    skill_tools._current_tenant_id.reset(token)


@pytest.fixture(autouse=True)
def _no_room():
    """다른 테스트가 남긴 대화방 컨텍스트에 체크아웃 경로가 흔들리지 않게 한다."""
    token = skill_tools._current_conversation_id.set("")
    yield
    skill_tools._current_conversation_id.reset(token)


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    skill_tools._git_provider_cache.clear()
    yield
    skill_tools._git_provider_cache.clear()


# ---------------------------------------------------------------------------
# 설정 조회
# ---------------------------------------------------------------------------

def test_git_config_lookup_is_bounded(tenant, monkeypatch):
    """설정(tenant_git_config) 조회가 매달리면 상한에서 끊고 git 없이 진행하게 한다."""
    from core.skills.git_providers import factory

    monkeypatch.setattr(factory, "get_provider", lambda tid: time.sleep(30))
    monkeypatch.setattr(skill_tools, "_GIT_CONFIG_TIMEOUT", 1)

    started = time.monotonic()
    with pytest.raises(ValueError) as excinfo:
        skill_tools._get_git_provider()
    assert time.monotonic() - started < 5
    assert "상한" in str(excinfo.value)


def test_git_config_lookup_is_cached(tenant, monkeypatch):
    """같은 턴에서 provider 를 여러 번 만들어도 설정 조회는 한 번만 나간다."""
    from core.skills.git_providers import factory

    calls: list[str] = []
    monkeypatch.setattr(factory, "get_provider", lambda tid: calls.append(tid) or object())

    first = skill_tools._get_git_provider()
    second = skill_tools._get_git_provider()
    assert first is second
    assert calls == ["acme"]


# ---------------------------------------------------------------------------
# 폴백 알림
# ---------------------------------------------------------------------------

def test_checkout_tells_user_it_fell_back_to_local_copy(sandbox, tenant, monkeypatch):
    """원격이 막혀 로컬 사본으로 폴백하면 경로와 함께 그 사실을 알린다."""
    monkeypatch.setattr(
        skill_tools, "_sandbox_exec",
        lambda cmd, **kw: (0, "/skills/acme/local/nda-review") if cmd.startswith("for d in ") else (0, ""),
    )
    skill_tools._mark_git_net_blocked("Recv failure: Connection reset by peer")

    result = skill_tools.checkout_to_workspace.func("nda-review")

    lines = result.splitlines()
    assert lines[0] == "/workspace/nda-review", "첫 줄은 그대로 쓸 수 있는 경로여야 한다"
    assert any(line.startswith("NOTE:") for line in lines[1:]), result
    assert "로컬 사본" in result


def test_successful_checkout_has_no_notes(sandbox, tenant, monkeypatch):
    """폴백이 없으면 예전처럼 경로만 돌려준다(반환값 오염 금지)."""
    monkeypatch.setattr(
        skill_tools, "_sandbox_exec",
        lambda cmd, **kw: (0, "/skills/acme/local/nda-review") if cmd.startswith("for d in ") else (0, ""),
    )
    monkeypatch.setattr(skill_tools, "_try_clone_skill_repo", lambda name, dest: None)

    assert skill_tools.checkout_to_workspace.func("nda-review") == "/workspace/nda-review"


# ---------------------------------------------------------------------------
# 병합 요청 기록 실패 보고
# ---------------------------------------------------------------------------

def _provider_with_pr():
    return type(
        "P", (), {
            "username": "o",
            "create_pull_request": lambda self, **kw: {
                "number": 7, "html_url": "https://h/o/s/pull/7"
            },
        },
    )()


def test_publish_reports_when_merge_request_record_fails(monkeypatch):
    """PR 은 만들어졌는데 기록만 실패한 경우, 결과 문자열에 그 사실이 남는다."""
    monkeypatch.setattr(skill_tools, "_get_git_provider", _provider_with_pr)
    monkeypatch.setattr(skill_tools, "_record_resource_pr", lambda **kw: None)

    result = skill_tools._create_pr_after_push(
        skill_name="s", branch="skill/s/1", base_branch="main",
        html_url="https://h/o/s", title="feat: update s", description="",
    )

    assert "https://h/o/s/pull/7" in result
    assert "기록하지 못했습니다" in result


def test_publish_stays_quiet_when_record_succeeds(monkeypatch):
    monkeypatch.setattr(skill_tools, "_get_git_provider", _provider_with_pr)
    monkeypatch.setattr(skill_tools, "_record_resource_pr", lambda **kw: {"id": "x"})

    result = skill_tools._create_pr_after_push(
        skill_name="s", branch="skill/s/1", base_branch="main",
        html_url="https://h/o/s", title="feat: update s", description="",
    )
    assert "기록하지 못했습니다" not in result


def test_record_failure_is_noted_for_the_user(monkeypatch, tenant):
    """DB 기록 실패는 로그로만 삼키지 않고 도구 응답에 실린다."""
    class _Boom:
        def table(self, *_a, **_kw):
            raise RuntimeError("db down")

    monkeypatch.setattr("core.storage.db.get_supabase", lambda: _Boom())

    with skill_tools._fallback_notices():
        assert skill_tools._record_resource_pr(
            resource_type="skill", resource_id="s", branch_name="b",
            base_branch="main", title="t", description="d",
        ) is None
        assert "병합 요청" in skill_tools._with_notes("결과")
