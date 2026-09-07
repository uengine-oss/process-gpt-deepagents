"""publish_skill 이 기존 레포에 "요청하지 않은 삭제"를 섞지 않는지 검증한다.

_push_skill_sandbox 는 기존 레포를 수정할 때 fetch + reset --soft 후 워킹트리를 스테이징한다.
워킹트리가 로컬 스킬 사본이면 레포에만 있는 파일(evals/ 등)이 없는데, 그때 `git add .` 는
그 파일들의 삭제까지 커밋해 병합 시 레포에서 사라진다. 기존 레포 경로는 삭제를 스테이징하지
않아야 한다(의도적 삭제는 delete_skill_file 경로를 쓴다).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.skills import tools as skill_tools


@pytest.fixture
def git_calls(monkeypatch):
    """_git_sb / _sandbox_exec 를 가로채 호출된 git 인자 목록을 모은다."""
    calls: list[tuple[str, ...]] = []

    def fake_git(cwd, *args):
        calls.append(tuple(args))
        if args[:1] == ("commit",):
            return 0, "1 file changed"
        if args[:1] == ("push",):
            return 0, "pushed"
        if args[:1] == ("fetch",):
            return 0, ""
        return 0, ""

    monkeypatch.setattr(skill_tools, "_git_sb", fake_git)
    # 원격을 타는 git(fetch/push)은 _git_net_sb 로 나간다 — 같은 fake 로 받아 기록한다.
    monkeypatch.setattr(skill_tools, "_git_net_sb", fake_git)
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (0, ""))
    return calls


def _add_args(calls):
    return [c for c in calls if c and c[0] == "add"]


def test_existing_repo_does_not_stage_deletions(git_calls):
    with patch.object(skill_tools, "_create_pr_after_push", return_value="PR created"):
        result = skill_tools._push_skill_sandbox(
            "/workspace/nda-review",
            "nda-review",
            "skill/nda-review/1",
            "https://token@host/o/nda-review.git",
            "https://host/o/nda-review",
            existing_repo=True,
        )

    assert result == "PR created"
    adds = _add_args(git_calls)
    assert adds, "git add 가 호출되지 않았다"
    # 삭제를 스테이징하지 않아야 한다.
    assert adds[0] == ("add", "--ignore-removal", "."), adds


def test_new_repo_stages_everything(git_calls):
    with patch.object(skill_tools, "_auto_register_tenant_skill", return_value="registered"):
        result = skill_tools._push_skill_sandbox(
            "/workspace/new-skill",
            "new-skill",
            "main",
            "https://token@host/o/new-skill.git",
            "https://host/o/new-skill",
            existing_repo=False,
        )

    assert result == "registered"
    adds = _add_args(git_calls)
    # 신규 레포는 초기 커밋이므로 종전대로 전체를 스테이징한다.
    assert adds[0] == ("add", "."), adds


# ---------------------------------------------------------------------------
# checkout_to_workspace: 레포가 있으면 로컬 사본이 아니라 레포를 가져와야 한다
# ---------------------------------------------------------------------------

def test_clone_preferred_when_repo_exists(monkeypatch):
    """레포가 있으면 클론 경로를 쓴다(로컬 사본으로 작업하면 PR diff 가 전체 교체가 된다)."""
    execs: list[str] = []
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (execs.append(cmd), (0, ""))[1])

    provider = type(
        "P", (), {
            "username": "o",
            "repo_exists": lambda self, n: {"name": n},
            "get_authenticated_clone_url": lambda self, o, n: f"https://t@h/{o}/{n}.git",
        },
    )()
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: provider)

    assert skill_tools._try_clone_skill_repo("nda-review", "/workspace/nda-review") == "/workspace/nda-review"
    assert any(" clone --depth" in c for c in execs), execs


def test_falls_back_to_local_copy_when_no_repo(monkeypatch):
    """레포가 없으면 None → 호출자가 로컬 사본으로 폴백한다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (0, ""))
    provider = type("P", (), {"username": "o", "repo_exists": lambda self, n: None})()
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: provider)

    assert skill_tools._try_clone_skill_repo("local-only", "/workspace/local-only") is None


def test_falls_back_when_git_not_configured(monkeypatch):
    """git 미연동 테넌트도 기존처럼 동작해야 한다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (0, ""))
    def no_git():
        raise ValueError("Git 계정이 연결되지 않았습니다")
    monkeypatch.setattr(skill_tools, "_get_git_provider", no_git)

    assert skill_tools._try_clone_skill_repo("any", "/workspace/any") is None


def test_falls_back_when_clone_fails(monkeypatch):
    """클론이 실패하면 작업을 막지 않고 로컬 사본으로 폴백한다."""
    monkeypatch.setattr(skill_tools, "_sandbox_exec", lambda cmd, **kw: (1, "fatal: could not read"))
    provider = type(
        "P", (), {
            "username": "o",
            "repo_exists": lambda self, n: {"name": n},
            "get_authenticated_clone_url": lambda self, o, n: f"https://t@h/{o}/{n}.git",
        },
    )()
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: provider)

    assert skill_tools._try_clone_skill_repo("nda-review", "/workspace/nda-review") is None
