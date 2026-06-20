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
