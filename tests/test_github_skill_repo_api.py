"""GitHub provider integration tests for skill repository API methods.

Requires GITHUB_TOKEN env var. Skipped automatically when not set.
A temporary repo is created at session start and deleted at teardown.
"""

import pytest
import uuid

pytestmark = pytest.mark.github


# ---------------------------------------------------------------------------
# 4.2 list_branches
# ---------------------------------------------------------------------------

def test_list_branches_returns_name_and_sha(github_provider, github_test_repo):
    branches = github_provider.list_branches(github_provider.username, github_test_repo)
    assert isinstance(branches, list)
    assert len(branches) >= 1
    for b in branches:
        assert "name" in b
        assert "sha" in b
        assert isinstance(b["name"], str)
        assert isinstance(b["sha"], str) and len(b["sha"]) == 40


def test_list_branches_includes_main(github_provider, github_test_repo):
    branches = github_provider.list_branches(github_provider.username, github_test_repo)
    names = [b["name"] for b in branches]
    assert "main" in names


# ---------------------------------------------------------------------------
# 4.3 create_branch
# ---------------------------------------------------------------------------

def test_create_branch_returns_name_and_sha(github_provider, github_test_repo):
    branch_name = f"test-branch-{uuid.uuid4().hex[:6]}"
    result = github_provider.create_branch(
        github_provider.username, github_test_repo, branch_name, "main"
    )
    assert result["name"] == branch_name
    assert isinstance(result["sha"], str) and len(result["sha"]) == 40

    # cleanup: delete the branch via GitHub API
    import httpx
    httpx.delete(
        f"https://api.github.com/repos/{github_provider.username}/{github_test_repo}/git/refs/heads/{branch_name}",
        headers={"Authorization": f"Bearer {github_provider.token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )


def test_create_branch_from_nonexistent_raises(github_provider, github_test_repo):
    with pytest.raises(Exception):
        github_provider.create_branch(
            github_provider.username, github_test_repo, "new-branch", "nonexistent-base"
        )


# ---------------------------------------------------------------------------
# 4.4 list_commits
# ---------------------------------------------------------------------------

def test_list_commits_returns_required_fields(github_provider, github_test_repo):
    commits = github_provider.list_commits(github_provider.username, github_test_repo)
    assert isinstance(commits, list)
    assert len(commits) >= 1
    for c in commits:
        assert "sha" in c
        assert "message" in c
        assert "author" in c
        assert "date" in c


def test_list_commits_limit_respected(github_provider, github_test_repo):
    # Seed a second commit so there are ≥2 commits
    github_provider.update_file(
        github_provider.username, github_test_repo,
        "seed.txt", "seed", "seed commit", "main",
    )
    commits = github_provider.list_commits(github_provider.username, github_test_repo, limit=1)
    assert len(commits) <= 1


def test_list_commits_branch_filter(github_provider, github_test_repo):
    commits = github_provider.list_commits(
        github_provider.username, github_test_repo, branch="main"
    )
    assert isinstance(commits, list)
    assert len(commits) >= 1


# ---------------------------------------------------------------------------
# 4.5 update_file
# ---------------------------------------------------------------------------

def test_update_file_new_file_returns_sha_and_url(github_provider, github_test_repo):
    path = f"test-files/file-{uuid.uuid4().hex[:6]}.txt"
    result = github_provider.update_file(
        github_provider.username, github_test_repo,
        path, "hello world", "test: add file", "main",
    )
    assert "sha" in result
    assert "html_url" in result
    assert isinstance(result["sha"], str) and result["sha"]
    assert "github.com" in result["html_url"]


def test_update_file_existing_file_updates(github_provider, github_test_repo):
    path = "updatable.txt"
    github_provider.update_file(
        github_provider.username, github_test_repo,
        path, "v1", "test: create updatable", "main",
    )
    result = github_provider.update_file(
        github_provider.username, github_test_repo,
        path, "v2", "test: update updatable", "main",
    )
    assert "sha" in result


# ---------------------------------------------------------------------------
# 4.6 create_pull_request + merge_pull_request
# ---------------------------------------------------------------------------

def test_create_and_merge_pull_request(github_provider, github_test_repo):
    branch_name = f"pr-branch-{uuid.uuid4().hex[:6]}"

    # Create feature branch
    github_provider.create_branch(
        github_provider.username, github_test_repo, branch_name, "main"
    )

    # Commit a file to the feature branch
    path = f"pr-file-{uuid.uuid4().hex[:6]}.txt"
    github_provider.update_file(
        github_provider.username, github_test_repo,
        path, "pr content", "test: pr file", branch_name,
    )

    # Create PR
    pr = github_provider.create_pull_request(
        github_provider.username, github_test_repo,
        title="Test PR",
        body="Integration test PR",
        head=branch_name,
        base="main",
    )
    assert "number" in pr
    assert "html_url" in pr
    assert "state" in pr
    assert pr["state"] == "open"

    # Merge PR
    merge_result = github_provider.merge_pull_request(
        github_provider.username, github_test_repo, pr["number"]
    )
    assert merge_result.get("merged") is True
    assert "sha" in merge_result


# ---------------------------------------------------------------------------
# 4.7 list_pull_requests
# ---------------------------------------------------------------------------

def test_list_pull_requests_open_state(github_provider, github_test_repo):
    prs = github_provider.list_pull_requests(github_provider.username, github_test_repo, state="open")
    assert isinstance(prs, list)
    for pr in prs:
        assert "number" in pr
        assert "title" in pr
        assert "html_url" in pr
        assert "state" in pr
        assert "head" in pr
        assert "base" in pr


def test_list_pull_requests_closed_state(github_provider, github_test_repo):
    prs = github_provider.list_pull_requests(github_provider.username, github_test_repo, state="closed")
    assert isinstance(prs, list)


def test_list_pull_requests_all_state(github_provider, github_test_repo):
    prs = github_provider.list_pull_requests(github_provider.username, github_test_repo, state="all")
    assert isinstance(prs, list)


def test_list_pull_requests_head_is_branch_name_not_label(github_provider, github_test_repo):
    """head/base must be branch names (e.g. 'main'), not 'owner:branch' labels."""
    prs = github_provider.list_pull_requests(github_provider.username, github_test_repo, state="all")
    for pr in prs:
        assert ":" not in pr["head"], f"head should be branch name, got: {pr['head']}"
        assert ":" not in pr["base"], f"base should be branch name, got: {pr['base']}"
