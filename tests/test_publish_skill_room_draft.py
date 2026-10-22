"""같은 대화방에서 스킬을 만들고 이어서 고칠 때 publish_skill 이 병합 요청 경로를 타는지.

관측된 문제: 새 대화방에서 고치면 PR 이 열리는데, 스킬을 만든 그 방에서 이어 고치면
열리지 않았다. 새 방에서는 checkout_to_workspace 가 돌려준 `.checkouts/<room>/<skill>`
(origin 이 붙은 클론) 위에서 고치지만, 같은 방에서는 생성 때 쓴 초안이
`.bpmn/<room>/<skill>/` 에 그대로 남아 있어 에이전트가 그 자리를 고치기 때문이다.
초안에는 git 이력이 없어 그대로 발행하면 diff 기준이 없고, 결국 main 통째 커밋으로 빠져
이후 PR 을 열 변경분이 0 이 된다.

레포가 있는 스킬을 방 초안 경로로 발행하면 그 레포를 클론한 위에 초안 파일을 얹어야 한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.skills import tools as skill_tools

ROOM = "513ea852-f8be-4dbd-a4da-b75e249292a5"
SKILL = "meeting-room-booking-reviewer"
DRAFT = f"/workspace/.bpmn/{ROOM}/{SKILL}"
CHECKOUT = f"/workspace/.checkouts/{ROOM}/{SKILL}"
HTML_URL = f"https://example.invalid/owner/{SKILL}"


def _provider(*, repo_exists=True, branches=("main",)):
    p = MagicMock()
    p.username = "owner"
    p.repo_exists.return_value = {"name": SKILL} if repo_exists else None
    p.list_branches.return_value = list(branches)
    p.get_authenticated_clone_url.return_value = f"https://token@example.invalid/owner/{SKILL}.git"
    p.get_repo_html_url.return_value = HTML_URL
    return p


@pytest.fixture
def sandbox(monkeypatch):
    """샌드박스 명령을 가로채고 호출 로그를 돌려준다."""
    sb_token = skill_tools._current_sandbox.set(object())
    conv_token = skill_tools._current_conversation_id.set(ROOM)

    calls: list[str] = []

    def fake_exec(cmd, *, timeout=None):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(skill_tools, "_sandbox_exec", fake_exec)
    yield calls
    skill_tools._current_sandbox.reset(sb_token)
    skill_tools._current_conversation_id.reset(conv_token)


def test_room_draft_is_published_from_a_fresh_clone(sandbox, monkeypatch):
    """방 초안 경로로 발행하면 레포를 클론한 위에 초안을 얹어 그 경로로 push 한다."""
    clones: list[tuple[str, str]] = []

    def fake_clone(auth_url, dest):
        clones.append((auth_url, dest))
        return 0, ""

    pushed: dict = {}

    def fake_push(cwd, skill_name, branch, clone_url, html_url, *, existing_repo=False):
        pushed.update(cwd=cwd, branch=branch, existing_repo=existing_repo)
        return f"PR created for '{skill_name}'"

    monkeypatch.setattr(skill_tools, "_clone_sb", fake_clone)
    monkeypatch.setattr(skill_tools, "_push_skill_sandbox", fake_push)
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: _provider())

    result = skill_tools._publish_skill_impl(DRAFT)

    assert "PR created" in result, result
    # 초안이 아니라 체크아웃 경로에서 발행해야 한다.
    assert pushed["cwd"] == CHECKOUT, pushed
    assert pushed["existing_repo"] is True
    assert pushed["branch"] != "main"
    # 그 체크아웃은 원격을 클론해 만든 것이어야 한다(= diff 기준이 있는 상태).
    assert clones and clones[0][1] == CHECKOUT, clones
    # 초안 파일은 .git 을 건드리지 않고 얹는다.
    assert any("! -name .git" in c and CHECKOUT in c for c in sandbox), sandbox


def test_clone_failure_is_reported_instead_of_overwriting_main(sandbox, monkeypatch):
    """클론하지 못하면 초안 그대로 발행하지 않고 실패로 알린다."""
    monkeypatch.setattr(skill_tools, "_clone_sb", lambda *a: (1, "network unreachable"))

    def fail_push(*a, **k):  # pragma: no cover - 호출되면 테스트 실패
        raise AssertionError("클론 실패 시 push 로 진행하면 안 된다")

    monkeypatch.setattr(skill_tools, "_push_skill_sandbox", fail_push)
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: _provider())

    result = skill_tools._publish_skill_impl(DRAFT)

    assert result.startswith("ERROR"), result
    assert "network unreachable" in result
    assert "checkout_to_workspace" in result


def test_checkout_path_is_published_as_is(sandbox, monkeypatch):
    """이미 체크아웃 경로면(새 방에서 고친 경우) 다시 클론하지 않는다."""
    clones: list = []
    monkeypatch.setattr(skill_tools, "_clone_sb", lambda a, d: (clones.append(d), (0, ""))[1])

    pushed: dict = {}

    def fake_push(cwd, skill_name, branch, clone_url, html_url, *, existing_repo=False):
        pushed.update(cwd=cwd, existing_repo=existing_repo)
        return "PR created"

    monkeypatch.setattr(skill_tools, "_push_skill_sandbox", fake_push)
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: _provider())

    skill_tools._publish_skill_impl(CHECKOUT)

    assert pushed["cwd"] == CHECKOUT
    assert pushed["existing_repo"] is True
    assert not clones, "체크아웃 경로를 다시 클론하면 방금 한 수정이 사라진다"


def test_new_skill_from_room_draft_still_creates_repo(sandbox, monkeypatch):
    """레포가 없는 신규 스킬은 초안 경로 그대로 발행한다(클론할 대상이 없다)."""
    monkeypatch.setattr(skill_tools, "_clone_sb", lambda *a: (0, ""))

    pushed: dict = {}

    def fake_push(cwd, skill_name, branch, clone_url, html_url, *, existing_repo=False):
        pushed.update(cwd=cwd, branch=branch, existing_repo=existing_repo)
        return "Published"

    provider = _provider(repo_exists=False)
    provider.create_repo.return_value = HTML_URL
    monkeypatch.setattr(skill_tools, "_push_skill_sandbox", fake_push)
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: provider)

    skill_tools._publish_skill_impl(DRAFT)

    assert pushed["cwd"] == DRAFT
    assert pushed["existing_repo"] is False
    assert pushed["branch"] == "main"
    provider.create_repo.assert_called_once_with(SKILL)
