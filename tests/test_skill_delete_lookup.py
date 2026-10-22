"""등록된 스킬이 "목록에는 보이는데 삭제는 404" 가 되지 않는지 검증한다.

publish_skill 이 신규 레포를 만들면 파일은 {tenant}/{provider_config_id}/{skill} 로 들어간다
(_auto_register_tenant_skill). 그런데 프론트의 삭제는 항상 mode="local" 로 보내고, 그 경로는
_find_tenant_skill_dir 을 config_id 없이 호출한다. 예전에는 그때 {tenant}/{skill} 과
{tenant}/local/{skill} 만 봐서, provider-keyed 로 등록된 스킬은 목록(_scan_skills)에는
나오는데 삭제하면 404 가 났다(서버에서 실제로 재현됨).

아울러 원격 레포가 이미 지워진 뒤에도 삭제가 진행돼야 한다 — DELETE 는 멱등해야 하고,
404 로 예외를 던지면 로컬 정리까지 건너뛰어 스킬이 영영 남는다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.api import skills_router as sr
from core.skills.git_providers.gitea import GiteaProvider
from core.skills.git_providers.github import GitHubProvider
from core.skills.git_providers.gitlab import GitLabProvider

CONFIG_ID = "3f1c9a52-0b7e-4a11-9d2c-77e0c1a4b8de"


def _make_skill(root, tenant, collection, name):
    d = root / tenant / collection / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
    return d


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))
    return tmp_path


def test_finds_provider_keyed_skill_without_config_id(skills_root):
    """config_id 를 넘기지 않아도 provider-keyed 로 등록된 스킬을 찾는다."""
    expected = _make_skill(skills_root, "acme", CONFIG_ID, "vendor-risk-screener")

    found = sr._find_tenant_skill_dir("vendor-risk-screener", "acme", "")

    assert found == expected


def test_still_finds_local_upload(skills_root):
    expected = _make_skill(skills_root, "acme", "local", "uploaded-skill")
    assert sr._find_tenant_skill_dir("uploaded-skill", "acme", "") == expected


def test_does_not_reach_into_other_tenant(skills_root):
    """탐색 범위는 이 테넌트 디렉터리 안으로 한정된다."""
    _make_skill(skills_root, "other-tenant", CONFIG_ID, "secret-skill")
    (skills_root / "acme").mkdir(parents=True, exist_ok=True)

    assert sr._find_tenant_skill_dir("secret-skill", "acme", "") is None


def test_missing_skill_still_returns_none(skills_root):
    (skills_root / "acme" / "local").mkdir(parents=True)
    assert sr._find_tenant_skill_dir("nope", "acme", "") is None


# ---------------------------------------------------------------------------
# 원격 레포가 이미 없을 때
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


@pytest.mark.parametrize("provider_cls, kwargs", [
    (GitHubProvider, {"username": "o", "token": "t"}),
    (GiteaProvider, {"username": "o", "token": "t", "base_url": "https://g.example"}),
    (GitLabProvider, {"username": "o", "token": "t", "base_url": "https://l.example"}),
])
def test_delete_repo_treats_missing_repo_as_deleted(provider_cls, kwargs):
    """이미 지워진 레포(404)는 성공으로 다룬다 — 안 그러면 로컬 정리가 통째로 막힌다."""
    provider = provider_cls(**kwargs)
    with patch("httpx.delete", return_value=_Resp(404)):
        provider.delete_repo("o", "already-gone")  # 예외가 나면 실패


@pytest.mark.parametrize("provider_cls, kwargs", [
    (GitHubProvider, {"username": "o", "token": "t"}),
    (GiteaProvider, {"username": "o", "token": "t", "base_url": "https://g.example"}),
    (GitLabProvider, {"username": "o", "token": "t", "base_url": "https://l.example"}),
])
def test_delete_repo_still_raises_on_real_failure(provider_cls, kwargs):
    """권한 부족(403) 같은 진짜 실패는 종전대로 알린다."""
    provider = provider_cls(**kwargs)
    with patch("httpx.delete", return_value=_Resp(403)):
        with pytest.raises(RuntimeError):
            provider.delete_repo("o", "no-permission")
