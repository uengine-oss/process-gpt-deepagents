"""core/skills/git_providers/gitea.py — Gitea API 구현 (Forgejo 호환)."""

from __future__ import annotations

from urllib.request import Request, urlopen

import httpx

from .base import GitProvider


class GiteaProvider(GitProvider):

    def __init__(self, username: str, token: str, base_url: str | None = None) -> None:
        if not base_url:
            raise ValueError("GiteaProvider requires base_url (e.g. https://gitea.example.com)")
        super().__init__(username, token, base_url)
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/api/v1"

    def _headers(self) -> dict:
        return {"Authorization": f"token {self.token}"}

    # ------------------------------------------------------------------

    def list_repos(self) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self._api}/repos/search",
                headers=self._headers(),
                params={"limit": 50, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            batch = (resp.json().get("data") or [])
            if not batch:
                break
            repos.extend(
                {
                    "name": r["name"],
                    "description": r.get("description") or "",
                    "html_url": r["html_url"],
                    "updated_at": r.get("updated", ""),
                    "default_branch": r.get("default_branch", "main"),
                }
                for r in batch
            )
            if len(batch) < 50:
                break
            page += 1
        return repos

    def repo_exists(self, name: str) -> dict | None:
        resp = httpx.get(
            f"{self._api}/repos/{self.username}/{name}",
            headers=self._headers(),
            timeout=10,
        )
        return resp.json() if resp.status_code == 200 else None

    def detect_default_branch(self, owner: str, repo: str) -> str:
        try:
            resp = httpx.get(
                f"{self._api}/repos/{owner}/{repo}",
                headers=self._headers(),
                timeout=10,
            )
            return resp.json().get("default_branch") or "main"
        except Exception:
            return "main"

    def create_repo(self, name: str) -> str:
        resp = httpx.post(
            f"{self._api}/user/repos",
            headers=self._headers(),
            json={"name": name, "private": False, "auto_init": False},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Gitea 레포 생성 실패: {resp.status_code} {resp.text[:200]}")
        return resp.json()["html_url"]

    def fetch_file(self, owner: str, repo: str, branch: str, path: str) -> bytes:
        url = f"{self._base}/{owner}/{repo}/raw/branch/{branch}/{path}"
        req = Request(url, headers={**self._headers(), "User-Agent": "process-gpt-deepagents/1.0"})
        with urlopen(req, timeout=60) as r:
            return r.read()

    def list_repo_tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        # 브랜치 → commit SHA 조회
        resp = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/branches/{branch}",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        sha = resp.json()["commit"]["id"]

        resp2 = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/git/trees/{sha}",
            headers=self._headers(),
            params={"recursive": True},
            timeout=30,
        )
        resp2.raise_for_status()
        return resp2.json().get("tree") or []

    def get_authenticated_clone_url(self, owner: str, repo_name: str) -> str:
        host = self._base.removeprefix("https://").removeprefix("http://")
        scheme = "https" if self._base.startswith("https") else "http"
        return f"{scheme}://{self.username}:{self.token}@{host}/{owner}/{repo_name}.git"

    def get_repo_html_url(self, owner: str, repo_name: str) -> str:
        return f"{self._base}/{owner}/{repo_name}"

    def list_branches(self, owner: str, repo: str) -> list[dict]:
        resp = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/branches",
            headers=self._headers(),
            params={"limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        return [{"name": b["name"], "sha": b["commit"]["id"]} for b in resp.json()]

    def list_commits(
        self, owner: str, repo: str, branch: str | None = None, limit: int = 20
    ) -> list[dict]:
        params: dict = {"limit": limit}
        if branch:
            params["sha"] = branch
        resp = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/commits",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return [
            {
                "sha": c["sha"],
                "message": c["commit"]["message"],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in resp.json()
        ]

    def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> dict:
        import base64

        encoded = base64.b64encode(content.encode()).decode()
        existing = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(),
            params={"ref": branch},
            timeout=10,
        )
        body: dict = {"message": message, "content": encoded, "branch": branch}
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        if author_name and author_email:
            body["author"] = {"name": author_name, "email": author_email}
        resp = httpx.put(
            f"{self._api}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"sha": data["content"]["sha"], "html_url": data["content"]["html_url"]}

    def delete_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        branch: str,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> dict:
        existing = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(),
            params={"ref": branch},
            timeout=10,
        )
        if existing.status_code != 200:
            raise FileNotFoundError(f"파일 '{path}'을 찾을 수 없습니다.")
        sha = existing.json()["sha"]
        body: dict = {"message": message, "sha": sha, "branch": branch}
        if author_name and author_email:
            body["author"] = {"name": author_name, "email": author_email}
        resp = httpx.request(
            "DELETE",
            f"{self._api}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        commit = data.get("commit", {})
        return {"sha": commit.get("sha", ""), "html_url": self.get_repo_html_url(owner, repo)}

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str) -> dict:
        resp = httpx.post(
            f"{self._api}/repos/{owner}/{repo}/branches",
            headers=self._headers(),
            json={"new_branch_name": new_branch, "old_branch_name": from_branch},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"name": data["name"], "sha": data["commit"]["id"]}

    def merge_pull_request(self, owner: str, repo: str, number: int, message: str = "") -> dict:
        resp = httpx.post(
            f"{self._api}/repos/{owner}/{repo}/pulls/{number}/merge",
            headers=self._headers(),
            json={"Do": "merge", "merge_message_field": message or f"Merge pull request #{number}"},
            timeout=15,
        )
        resp.raise_for_status()
        return {"sha": "", "merged": True}

    def create_pull_request(
        self, owner: str, repo: str, title: str, body: str, head: str, base: str
    ) -> dict:
        resp = httpx.post(
            f"{self._api}/repos/{owner}/{repo}/pulls",
            headers=self._headers(),
            json={"title": title, "body": body, "head": head, "base": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["number"], "html_url": data["html_url"], "state": data["state"]}

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        resp = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/pulls",
            headers=self._headers(),
            params={"state": state, "limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        return [
            {
                "number": p["number"],
                "title": p["title"],
                "html_url": p["html_url"],
                "state": p["state"],
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
            }
            for p in resp.json()
        ]

    def delete_repo(self, owner: str, repo: str) -> None:
        resp = httpx.delete(
            f"{self._api}/repos/{owner}/{repo}",
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code != 204:
            raise RuntimeError(f"Gitea 레포 삭제 실패: {resp.status_code} {resp.text[:200]}")

    def get_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict]:
        resp = httpx.get(
            f"{self._api}/repos/{owner}/{repo}/pulls/{number}/files",
            headers=self._headers(),
            params={"limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        return [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch": f.get("patch", ""),
            }
            for f in resp.json()
        ]
