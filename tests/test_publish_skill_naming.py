"""publish_skill 이 대화방 디렉터리를 스킬명으로 삼지 않는지 검증한다.

에이전트가 단독 스킬을 `<base>/skills/<name>/SKILL.md` 가 아니라 `<base>/SKILL.md` 로
떨어뜨리는 일이 있다. 그때 디렉터리명을 그대로 쓰면 대화방 UUID 가 레포명·스킬명이 되어
`ohsy818/ef9ae263-371d-45c3-adff-6c6acbe71129` 같은 저장소가 실제로 만들어진다
(서버 검증 중 재현됨). SKILL.md frontmatter 의 name 으로 되돌리고, 그것도 없으면
아무것도 만들지 않아야 한다.
"""

from __future__ import annotations

import pytest

from core.skills import tools as skill_tools

ROOM = "ef9ae263-371d-45c3-adff-6c6acbe71129"

SKILL_MD = """---
name: vendor-risk-screener
description: |
  신규 거래처 등록 요청을 검토한다.
---

# 본문
"""


class _FakeSandbox:
    pass


class _FakeProvider:
    username = "o"
    config_id = "cfg"

    def __init__(self):
        self.created: list[str] = []

    def repo_exists(self, name):
        return None

    def create_repo(self, name):
        self.created.append(name)
        return f"https://h/o/{name}"

    def get_authenticated_clone_url(self, owner, repo):
        return f"https://t@h/{owner}/{repo}.git"


@pytest.fixture
def env(monkeypatch):
    sb = _FakeSandbox()
    token = skill_tools._current_sandbox.set(sb)
    provider = _FakeProvider()
    monkeypatch.setattr(skill_tools, "_get_git_provider", lambda: provider)

    pushed: list[tuple] = []
    monkeypatch.setattr(
        skill_tools, "_push_skill_sandbox",
        lambda cwd, name, branch, clone_url, html_url, **kw: (
            pushed.append((cwd, name)), f"Published '{name}'"
        )[1],
    )
    yield provider, pushed
    skill_tools._current_sandbox.reset(token)


def _exec_stub(skill_md: str | None):
    def _exec(cmd, **kw):
        if cmd.startswith("sed -n"):
            return (0, skill_md) if skill_md is not None else (1, "")
        return 0, ""
    return _exec


def test_room_dir_uses_frontmatter_name(env, monkeypatch):
    """방 디렉터리를 넘겨도 frontmatter 의 name 으로 발행한다."""
    provider, pushed = env
    monkeypatch.setattr(skill_tools, "_sandbox_exec", _exec_stub(SKILL_MD))

    result = skill_tools.publish_skill.func(f"/workspace/.bpmn/{ROOM}")

    assert "vendor-risk-screener" in result
    assert provider.created == ["vendor-risk-screener"], provider.created
    assert pushed and pushed[0][1] == "vendor-risk-screener"
    assert ROOM not in provider.created[0]


def test_room_dir_without_frontmatter_creates_nothing(env, monkeypatch):
    """이름을 알 수 없으면 레포를 만들지 않고 무엇을 고쳐야 하는지 알린다."""
    provider, pushed = env
    monkeypatch.setattr(skill_tools, "_sandbox_exec", _exec_stub("본문만 있고 frontmatter 없음"))

    result = skill_tools.publish_skill.func(f"/workspace/.bpmn/{ROOM}")

    assert result.startswith("ERROR:")
    assert "SKILL.md" in result
    assert provider.created == [], "이름을 모르는 채로 레포를 만들었다"
    assert pushed == []


def test_ordinary_skill_dir_keeps_directory_name(env, monkeypatch):
    """일반 스킬 디렉터리는 종전대로 디렉터리명을 쓴다.

    frontmatter 를 무조건 우선하면 레포명과 name 이 다른 기존 스킬
    (예: 레포 skill-medical-nda-review / name: nda-review)이 엉뚱한 레포로 발행된다.
    """
    provider, pushed = env
    monkeypatch.setattr(skill_tools, "_sandbox_exec", _exec_stub(SKILL_MD))

    result = skill_tools.publish_skill.func(f"/workspace/.checkouts/{ROOM}/skill-medical-nda-review")

    assert provider.created == ["skill-medical-nda-review"], provider.created
    assert pushed[0][1] == "skill-medical-nda-review"
