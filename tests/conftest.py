"""Shared test fixtures for skill repository API integration tests."""

import os
import uuid

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "github: integration test requiring GITHUB_TOKEN env var"
    )
    config.addinivalue_line(
        "markers", "gitlab: integration test requiring GITLAB_TOKEN env var"
    )
    config.addinivalue_line(
        "markers", "gitea: integration test requiring GITEA_TOKEN env var"
    )
    config.addinivalue_line(
        "markers", "real_tenant_auth: run against the real /skills tenant guard (no identity stub)"
    )


# ---------------------------------------------------------------------------
# GitHub fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def github_token():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN not set — skipping GitHub integration tests")
    return token


@pytest.fixture(scope="session")
def github_username(github_token):
    from core.skills.git_providers.github import GitHubProvider
    provider = GitHubProvider(username="", token=github_token)
    repos = provider.list_repos()
    assert repos, "Could not resolve GitHub username"
    import httpx
    resp = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["login"]


@pytest.fixture(scope="session")
def github_provider(github_token, github_username):
    from core.skills.git_providers.github import GitHubProvider
    return GitHubProvider(username=github_username, token=github_token)


@pytest.fixture(scope="session")
def github_test_repo(github_provider):
    """Create a temporary test repo and tear it down after the session."""
    repo_name = f"test-skill-api-{uuid.uuid4().hex[:8]}"
    github_provider.create_repo(repo_name)
    # Seed with an initial file so branches/commits are queryable
    github_provider.update_file(
        github_provider.username, repo_name,
        "README.md", "# test", "init", "main",
    )
    yield repo_name
    try:
        github_provider.delete_repo(github_provider.username, repo_name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 스킬 API 테넌트 인증
# ---------------------------------------------------------------------------
# /skills* 는 이제 검증된 JWT 에서 테넌트를 얻는다(processgpt_agent_sdk.tenant_auth).
# 기존 라우터 테스트는 토큰을 보내지 않으므로, 기본적으로 테스트용 신원을 주입한다.
# 가드 자체를 검증하는 테스트는 `@pytest.mark.real_tenant_auth` 로 이 스텁을 끈다.

TEST_USER_ID = "00000000-0000-0000-0000-0000000000aa"
TEST_TENANT_IDS = frozenset({"t1", "tenant1"})


@pytest.fixture(autouse=True)
def stub_tenant_auth(request, monkeypatch):
    if request.node.get_closest_marker("real_tenant_auth"):
        yield
        return

    from processgpt_agent_sdk import tenant_auth

    async def _fake_resolve_identity(_request):
        return tenant_auth.RequestIdentity(
            user_id=TEST_USER_ID,
            email="test@example.com",
            tenant_ids=TEST_TENANT_IDS,
        )

    monkeypatch.setattr(tenant_auth, "resolve_identity", _fake_resolve_identity)
    tenant_auth.clear_caches()
    yield
    tenant_auth.clear_caches()
