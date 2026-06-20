"""Tests for /skills/upload and /skills/upload-from-git endpoints."""

from __future__ import annotations

import io
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from core.api.skills_router import routes


@pytest.fixture
def app():
    return Starlette(routes=routes)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_zip(files: dict[str, str]) -> bytes:
    """Build an in-memory zip from {path: content} dict."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _upload_request(client, zip_bytes: bytes, *, filename="my-skill.zip",
                    tenant_id="tenant1", skill_name=None):
    data = {"tenant_id": tenant_id}
    if skill_name:
        data["skill_name"] = skill_name
    return client.post(
        "/skills/upload",
        data=data,
        files={"file": (filename, zip_bytes, "application/zip")},
    )


# ── /skills/upload ────────────────────────────────────────────────────────────

class TestUploadSkill:
    def test_valid_zip_with_skill_md_at_root(self, client, tmp_path, monkeypatch):
        """6.1 — Valid zip with SKILL.md registers successfully (201)."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        zip_bytes = _make_zip({"SKILL.md": "name: my-skill\n", "run.py": "print('hi')"})
        resp = _upload_request(client, zip_bytes, skill_name="my-skill")

        assert resp.status_code == 201
        body = resp.json()
        assert body["registered"] is True
        assert body["skill_name"] == "my-skill"
        assert "SKILL.md" in body["files"]

        dest = tmp_path / "tenant1" / "my-skill"
        assert dest.is_dir()
        assert (dest / "SKILL.md").exists()

    def test_valid_skill_extension(self, client, tmp_path, monkeypatch):
        """6.1 — .skill extension treated as zip."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        zip_bytes = _make_zip({"SKILL.md": "name: my-skill\n"})
        resp = _upload_request(client, zip_bytes, filename="my-skill.skill", skill_name="my-skill")

        assert resp.status_code == 201

    def test_archive_with_single_subdir_root(self, client, tmp_path, monkeypatch):
        """6.1 — Archive with single root subdirectory uses that dir as skill root."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        zip_bytes = _make_zip({
            "my-skill/SKILL.md": "name: my-skill\n",
            "my-skill/run.py": "print('hello')",
        })
        resp = _upload_request(client, zip_bytes, filename="bundle.zip", tenant_id="tenant1")

        assert resp.status_code == 201
        body = resp.json()
        assert body["skill_name"] == "my-skill"

    def test_missing_skill_md_returns_422(self, client, tmp_path, monkeypatch):
        """6.2 — Archive without SKILL.md returns 422."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        zip_bytes = _make_zip({"run.py": "print('no skill md')"})
        resp = _upload_request(client, zip_bytes, skill_name="bad-skill")

        assert resp.status_code == 422
        assert "SKILL.md" in resp.json()["error"]

    def test_path_traversal_blocked(self, client, tmp_path, monkeypatch):
        """6.3 — Path traversal entries in archive are silently skipped."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", "name: safe-skill\n")
            zf.writestr("../../../evil.txt", "pwned")
        zip_bytes = buf.getvalue()

        resp = _upload_request(client, zip_bytes, skill_name="safe-skill")

        assert resp.status_code == 201
        evil = tmp_path.parent / "evil.txt"
        assert not evil.exists()

    def test_skill_name_conflict_returns_409(self, client, tmp_path, monkeypatch):
        """6.4 — Existing skill directory returns 409."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))
        existing = tmp_path / "tenant1" / "conflict-skill"
        existing.mkdir(parents=True)

        zip_bytes = _make_zip({"SKILL.md": "name: conflict-skill\n"})
        resp = _upload_request(client, zip_bytes, skill_name="conflict-skill")

        assert resp.status_code == 409

    def test_invalid_archive_returns_400(self, client, tmp_path, monkeypatch):
        """Invalid zip bytes return 400."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        resp = _upload_request(client, b"this is not a zip file", skill_name="bad")

        assert resp.status_code == 400

    def test_unsupported_extension_returns_400(self, client, tmp_path, monkeypatch):
        """Unsupported extension returns 400."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        zip_bytes = _make_zip({"SKILL.md": "name: x\n"})
        resp = _upload_request(client, zip_bytes, filename="skill.tar.gz")

        assert resp.status_code == 400

    def test_missing_tenant_id_returns_400(self, client):
        """Missing tenant_id returns 400."""
        zip_bytes = _make_zip({"SKILL.md": "name: x\n"})
        resp = client.post(
            "/skills/upload",
            data={},
            files={"file": ("x.zip", zip_bytes, "application/zip")},
        )
        assert resp.status_code == 400


# ── /skills/upload-from-git ───────────────────────────────────────────────────

class TestUploadSkillFromGit:
    def _clone_side_effect(self, dest: Path, skill_name: str):
        """Simulate a successful git clone by writing SKILL.md to dest."""
        def _do_clone(url, d, timeout=60):
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text("name: cloned-skill\n")
            (d / "run.py").write_text("print('cloned')")
        return _do_clone

    def test_valid_git_url_clones_successfully(self, client, tmp_path, monkeypatch):
        """6.5 — Valid repo URL results in 201 with registered skill."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        def fake_clone(url, dest, timeout=60):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text("name: cloned-skill\n")

        with patch("core.skills.upload_utils.clone_skill_repo", side_effect=fake_clone):
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/cloned-skill",
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["registered"] is True
        assert body["skill_name"] == "cloned-skill"
        assert "SKILL.md" in body["files"]

    def test_skill_name_derived_from_url(self, client, tmp_path, monkeypatch):
        """6.5 — skill_name is derived from the last URL segment."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        def fake_clone(url, dest, timeout=60):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "SKILL.md").write_text("name: my-repo\n")

        with patch("core.skills.upload_utils.clone_skill_repo", side_effect=fake_clone):
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/my-repo.git",
                },
            )

        assert resp.status_code == 201
        assert resp.json()["skill_name"] == "my-repo"

    def test_disallowed_url_scheme_returns_400(self, client):
        """6.6 — file:// scheme returns 400."""
        resp = client.post(
            "/skills/upload-from-git",
            json={
                "tenant_id": "tenant1",
                "repo_url": "file:///etc/passwd",
            },
        )
        assert resp.status_code == 400

    def test_ssh_scheme_returns_400(self, client):
        """6.6 — ssh:// scheme returns 400."""
        resp = client.post(
            "/skills/upload-from-git",
            json={
                "tenant_id": "tenant1",
                "repo_url": "ssh://git@github.com/example/skill",
            },
        )
        assert resp.status_code == 400

    def test_clone_failure_returns_502(self, client, tmp_path, monkeypatch):
        """6.7 — git clone RuntimeError returns 502."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        with patch(
            "core.skills.upload_utils.clone_skill_repo",
            side_effect=RuntimeError("git clone failed: repository not found"),
        ):
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/nonexistent-skill",
                },
            )

        assert resp.status_code == 502
        assert "git clone failed" in resp.json()["error"]

    def test_clone_timeout_returns_504(self, client, tmp_path, monkeypatch):
        """6.7 — TimeoutExpired returns 504."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        with patch(
            "core.skills.upload_utils.clone_skill_repo",
            side_effect=subprocess.TimeoutExpired(cmd="git clone", timeout=60),
        ):
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/slow-skill",
                },
            )

        assert resp.status_code == 504

    def test_conflict_returns_409(self, client, tmp_path, monkeypatch):
        """6.4 — Existing skill directory returns 409 before cloning."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))
        existing = tmp_path / "tenant1" / "existing-skill"
        existing.mkdir(parents=True)

        with patch("core.skills.upload_utils.clone_skill_repo") as mock_clone:
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/existing-skill",
                },
            )
            mock_clone.assert_not_called()

        assert resp.status_code == 409

    def test_missing_repo_url_returns_400(self, client):
        """Missing repo_url returns 400."""
        resp = client.post(
            "/skills/upload-from-git",
            json={"tenant_id": "tenant1"},
        )
        assert resp.status_code == 400

    def test_missing_tenant_id_returns_400(self, client):
        """Missing tenant_id returns 400."""
        resp = client.post(
            "/skills/upload-from-git",
            json={"repo_url": "https://github.com/example/skill"},
        )
        assert resp.status_code == 400

    def test_no_skill_md_in_cloned_repo_returns_422(self, client, tmp_path, monkeypatch):
        """Missing SKILL.md in cloned repo returns 422."""
        monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))

        def fake_clone(url, dest, timeout=60):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "README.md").write_text("just a readme")

        with patch("core.skills.upload_utils.clone_skill_repo", side_effect=fake_clone):
            resp = client.post(
                "/skills/upload-from-git",
                json={
                    "tenant_id": "tenant1",
                    "repo_url": "https://github.com/example/not-a-skill",
                },
            )

        assert resp.status_code == 422
        assert "SKILL.md" in resp.json()["error"]
