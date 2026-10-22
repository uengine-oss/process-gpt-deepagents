"""HTTP endpoint tests for skill repository API routes.

Uses Starlette TestClient with a mocked Git provider to verify:
- Request parsing and parameter validation (400 cases)
- Response shape for normal cases
- Local disk sync on branch=main commit (task 5.3)

These tests do NOT call real Git APIs — that's covered by test_github_skill_repo_api.py.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient  # requires httpx2
from starlette.applications import Starlette

from core.api.skills_router import routes


@pytest.fixture
def app():
    return Starlette(routes=routes)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)


def _mock_provider(
    branches=None,
    commits=None,
    update_file_result=None,
    create_branch_result=None,
    pull_requests=None,
    create_pr_result=None,
    merge_pr_result=None,
):
    """Build a MagicMock that mimics GitProvider for the given outputs."""
    p = MagicMock()
    p.username = "test-owner"
    p.list_branches.return_value = branches or [{"name": "main", "sha": "a" * 40}]
    # 라우트가 기본 브랜치를 함께 조회한다. MagicMock 기본 반환값은 JSON 직렬화가 안 돼
    # 응답 생성에서 터지므로 문자열로 고정한다.
    p.detect_default_branch.return_value = "main"
    p.list_commits.return_value = commits or [
        {"sha": "b" * 40, "message": "init", "author": "Alice", "date": "2024-01-01T00:00:00Z"}
    ]
    p.update_file.return_value = update_file_result or {"sha": "c" * 40, "html_url": "https://github.com/x/y"}
    p.create_branch.return_value = create_branch_result or {"name": "feat", "sha": "d" * 40}
    p.list_pull_requests.return_value = pull_requests or []
    p.create_pull_request.return_value = create_pr_result or {"number": 1, "html_url": "https://github.com/x/y/pull/1", "state": "open"}
    p.merge_pull_request.return_value = merge_pr_result or {"sha": "e" * 40, "merged": True}
    return p


# ---------------------------------------------------------------------------
# GET /skills/{skill_name}/branches
# ---------------------------------------------------------------------------

class TestListSkillBranches:
    def test_missing_tenant_id_returns_400(self, client):
        resp = client.get("/skills/my-skill/branches")
        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id is required"

    def test_returns_branches(self, client):
        provider = _mock_provider()
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.get("/skills/my-skill/branches?tenant_id=t1")
        assert resp.status_code == 200
        data = resp.json()
        assert "branches" in data
        assert data["branches"][0]["name"] == "main"
        assert len(data["branches"][0]["sha"]) == 40

    def test_missing_repo_is_not_reported_as_error(self, client):
        """레포가 아직 없으면(404) 오류가 아니라 빈 목록으로 알린다.

        500 으로 돌려주면 화면이 "레포 생성" 안내 대신 오류를 띄우고, 반대로 인증 실패까지
        빈 목록으로 뭉뚱그리면 이미 있는 레포를 "없다"고 말하게 된다.
        """
        import httpx

        provider = _mock_provider()
        response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
        provider.list_branches.side_effect = httpx.HTTPStatusError(
            "404", request=response.request, response=response
        )
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.get("/skills/my-skill/branches?tenant_id=t1")
        assert resp.status_code == 200
        assert resp.json() == {"branches": [], "default_branch": "main", "repo_missing": True}

    def test_auth_failure_is_reported_as_error(self, client):
        """401 은 "레포 없음" 으로 뭉개지 않고 사유와 함께 실패로 알린다."""
        import httpx

        provider = _mock_provider()
        response = httpx.Response(401, request=httpx.Request("GET", "https://api.github.com/x"))
        provider.list_branches.side_effect = httpx.HTTPStatusError(
            "401", request=response.request, response=response
        )
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.get("/skills/my-skill/branches?tenant_id=t1")
        assert resp.status_code == 500
        assert resp.json()["error"]


# ---------------------------------------------------------------------------
# POST /skills/{skill_name}/branches
# ---------------------------------------------------------------------------

class TestCreateSkillBranch:
    def test_missing_tenant_id_returns_400(self, client):
        resp = client.post("/skills/my-skill/branches", json={"branch": "feat"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id is required"

    def test_missing_branch_returns_400(self, client):
        resp = client.post("/skills/my-skill/branches", json={"tenant_id": "t1"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "branch is required"

    def test_creates_branch(self, client):
        provider = _mock_provider(create_branch_result={"name": "feat", "sha": "d" * 40})
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.post(
                "/skills/my-skill/branches",
                json={"tenant_id": "t1", "branch": "feat", "from": "main"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] is True
        assert data["name"] == "feat"
        assert len(data["sha"]) == 40


# ---------------------------------------------------------------------------
# GET /skills/{skill_name}/commits
# ---------------------------------------------------------------------------

class TestListSkillCommits:
    def test_missing_tenant_id_returns_400(self, client):
        resp = client.get("/skills/my-skill/commits")
        assert resp.status_code == 400

    def test_returns_commits_with_required_fields(self, client):
        provider = _mock_provider()
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.get("/skills/my-skill/commits?tenant_id=t1")
        assert resp.status_code == 200
        commits = resp.json()["commits"]
        assert len(commits) >= 1
        assert all(k in commits[0] for k in ("sha", "message", "author", "date"))

    def test_limit_capped_at_100(self, client):
        provider = _mock_provider()
        with patch("core.api.skills_router._get_provider", return_value=provider):
            client.get("/skills/my-skill/commits?tenant_id=t1&limit=200")
        _, kwargs = provider.list_commits.call_args
        assert kwargs.get("limit", provider.list_commits.call_args[0][3] if len(provider.list_commits.call_args[0]) > 3 else 20) <= 100


# ---------------------------------------------------------------------------
# POST /skills/{skill_name}/commit
# ---------------------------------------------------------------------------

class TestCommitSkillFile:
    def test_missing_tenant_id_returns_400(self, client):
        resp = client.post("/skills/my-skill/commit", json={"file_path": "f.py", "content": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id is required"

    def test_missing_file_path_returns_400(self, client):
        resp = client.post("/skills/my-skill/commit", json={"tenant_id": "t1", "content": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "file_path and content are required"

    def test_missing_content_returns_400(self, client):
        resp = client.post("/skills/my-skill/commit", json={"tenant_id": "t1", "file_path": "f.py"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "file_path and content are required"

    def test_commit_returns_sha_and_url(self, client):
        provider = _mock_provider(update_file_result={"sha": "c" * 40, "html_url": "https://github.com/x/y"})
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.post(
                "/skills/my-skill/commit",
                json={"tenant_id": "t1", "file_path": "README.md", "content": "hi", "branch": "feat"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["committed"] is True
        assert len(data["sha"]) == 40
        assert "html_url" in data

    def test_commit_to_main_updates_local_disk(self, client):
        """branch=main should write the file to the local skill directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir()

            provider = _mock_provider(update_file_result={"sha": "c" * 40, "html_url": "https://github.com/x/y"})
            with (
                patch("core.api.skills_router._get_provider", return_value=provider),
                patch("core.api.skills_router._find_skill_dir", return_value=skill_dir),
            ):
                resp = client.post(
                    "/skills/my-skill/commit",
                    json={
                        "tenant_id": "t1",
                        "file_path": "SKILL.md",
                        "content": "# my skill",
                        "branch": "main",
                    },
                )
            assert resp.status_code == 200
            assert resp.json()["committed"] is True
            local_file = skill_dir / "SKILL.md"
            assert local_file.exists(), "local file should be written for branch=main"
            assert local_file.read_text() == "# my skill"

    def test_commit_to_feature_branch_does_not_update_local_disk(self, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my-skill"
            skill_dir.mkdir()

            provider = _mock_provider(update_file_result={"sha": "c" * 40, "html_url": "https://github.com/x/y"})
            with (
                patch("core.api.skills_router._get_provider", return_value=provider),
                patch("core.api.skills_router._find_skill_dir", return_value=skill_dir),
            ):
                resp = client.post(
                    "/skills/my-skill/commit",
                    json={
                        "tenant_id": "t1",
                        "file_path": "SKILL.md",
                        "content": "# my skill",
                        "branch": "feat-branch",
                    },
                )
            assert resp.status_code == 200
            local_file = skill_dir / "SKILL.md"
            assert not local_file.exists(), "local file must NOT be written for feature branches"


# ---------------------------------------------------------------------------
# GET /skills/{skill_name}/pull-requests
# ---------------------------------------------------------------------------

class TestListSkillPullRequests:
    def test_missing_tenant_id_returns_400(self, client):
        resp = client.get("/skills/my-skill/pull-requests")
        assert resp.status_code == 400

    def test_invalid_state_returns_400(self, client):
        resp = client.get("/skills/my-skill/pull-requests?tenant_id=t1&state=unknown")
        assert resp.status_code == 400
        assert "state" in resp.json()["error"]

    def test_returns_pull_requests_and_total(self, client):
        pr = {"number": 1, "title": "fix", "html_url": "https://github.com/x/y/pull/1",
              "state": "open", "head": "feat", "base": "main"}
        provider = _mock_provider(pull_requests=[pr])
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.get("/skills/my-skill/pull-requests?tenant_id=t1")
        assert resp.status_code == 200
        data = resp.json()
        assert "pull_requests" in data
        assert "total" in data
        assert data["total"] == 1


# ---------------------------------------------------------------------------
# POST /skills/{skill_name}/pull-requests
# ---------------------------------------------------------------------------

class TestCreateSkillPullRequest:
    def test_missing_title_or_head_returns_400(self, client):
        resp = client.post("/skills/my-skill/pull-requests", json={"tenant_id": "t1", "title": "fix"})
        assert resp.status_code == 400
        assert resp.json()["error"] == "title and head are required"

    def test_creates_pull_request(self, client):
        provider = _mock_provider(create_pr_result={"number": 2, "html_url": "https://github.com/x/y/pull/2", "state": "open"})
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = client.post(
                "/skills/my-skill/pull-requests",
                json={"tenant_id": "t1", "title": "My PR", "head": "feat", "base": "main"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] is True
        assert data["number"] == 2
        assert "html_url" in data
        assert data["state"] == "open"


# ---------------------------------------------------------------------------
# POST /skills/{skill_name}/pull-requests/{number}/merge
# ---------------------------------------------------------------------------

class TestMergeSkillPullRequest:
    def test_invalid_pr_number_returns_400(self, client):
        resp = client.post("/skills/my-skill/pull-requests/abc/merge", json={"tenant_id": "t1"})
        assert resp.status_code == 400 or resp.status_code == 404

    def test_missing_tenant_id_returns_400(self, client):
        resp = client.post("/skills/my-skill/pull-requests/1/merge", json={})
        assert resp.status_code == 400
        assert resp.json()["error"] == "tenant_id is required"

    def test_merges_and_returns_result(self, client):
        provider = _mock_provider(merge_pr_result={"sha": "e" * 40, "merged": True})
        with (
            patch("core.api.skills_router._get_provider", return_value=provider),
            patch("core.api.skills_router._find_skill_dir", return_value=None),
        ):
            resp = client.post(
                "/skills/my-skill/pull-requests/1/merge",
                json={"tenant_id": "t1", "auto_sync": False},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["merged"] is True
        assert data["number"] == 1
        assert len(data["sha"]) == 40

    def test_auto_sync_false_gives_empty_sync(self, client):
        provider = _mock_provider(merge_pr_result={"sha": "e" * 40, "merged": True})
        with (
            patch("core.api.skills_router._get_provider", return_value=provider),
            patch("core.api.skills_router._find_skill_dir", return_value=None),
        ):
            resp = client.post(
                "/skills/my-skill/pull-requests/1/merge",
                json={"tenant_id": "t1", "auto_sync": False},
            )
        assert resp.json()["sync"] == {}


# ---------------------------------------------------------------------------
# POST /skills/{skill_name}/repo
# ---------------------------------------------------------------------------

class TestCreateSkillRepo:
    """레포 생성 직후 초기 커밋 push 가 실패하면 '레포는 있는데 브랜치가 없는' 상태로 남는다.
    그때 409 로 막으면 변경 이력·PR 화면이 영구히 '레포 없음' 으로 남으므로,
    빈 레포에 한해 초기 커밋을 다시 밀어 넣을 수 있어야 한다."""

    def _post(self, client, skill="my-skill"):
        return client.post(
            f"/skills/{skill}/repo",
            json={"tenant_id": "t1", "initial_content": "# hi", "file_path": "SKILL.md"},
        )

    def test_creates_repo_when_missing(self, client):
        provider = _mock_provider()
        provider.repo_exists.return_value = None
        provider.create_repo.return_value = "https://git.example.com/test-owner/my-skill"
        provider.config_id = "local"
        with (
            patch("core.api.skills_router._get_provider", return_value=provider),
            patch("core.api.skills_router._find_skill_dir", return_value=None),
        ):
            resp = self._post(client)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pushed"] == ["SKILL.md"]
        provider.create_repo.assert_called_once()

    def test_conflicts_when_repo_has_branches(self, client):
        provider = _mock_provider(branches=[{"name": "main", "sha": "a" * 40}])
        provider.repo_exists.return_value = {"name": "my-skill"}
        provider.config_id = "local"
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = self._post(client)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]
        provider.create_repo.assert_not_called()

    def test_retries_initial_commit_when_repo_is_empty(self, client):
        provider = _mock_provider()
        provider.list_branches.return_value = []  # 커밋이 없는 빈 레포
        provider.repo_exists.return_value = {"name": "my-skill"}
        provider.get_repo_html_url.return_value = "https://git.example.com/test-owner/my-skill"
        provider.config_id = "local"
        with (
            patch("core.api.skills_router._get_provider", return_value=provider),
            patch("core.api.skills_router._find_skill_dir", return_value=None),
        ):
            resp = self._post(client)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pushed"] == ["SKILL.md"]
        # 이미 있는 레포이므로 다시 만들지 않고, 초기 커밋만 다시 올린다.
        provider.create_repo.assert_not_called()
        provider.update_file.assert_called_once()

    def test_treats_branch_lookup_failure_as_existing(self, client):
        provider = _mock_provider()
        provider.repo_exists.return_value = {"name": "my-skill"}
        provider.list_branches.side_effect = RuntimeError("api down")
        provider.config_id = "local"
        with patch("core.api.skills_router._get_provider", return_value=provider):
            resp = self._post(client)
        assert resp.status_code == 409
