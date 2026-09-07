"""core/skills/git_providers/github.py — GitHub API 구현."""

from __future__ import annotations

from urllib.request import Request, urlopen

import httpx

from .base import GitProvider

_API = "https://api.github.com"


class GitHubProvider(GitProvider):

    def _headers(self) -> dict:
        # 토큰이 비어 있으면 Authorization 헤더 자체를 넣지 않는다. `Bearer ` 처럼 값이 빈
        # 헤더는 httpx/urllib 이 "Illegal header value" 로 거절해, 공개 레포(anthropics/skills)
        # 인덱싱조차 매번 실패한다(→ skill-creator 등 번들 스킬이 안 받아짐).
        # 익명 호출은 rate limit 만 낮을 뿐 공개 레포 읽기에는 문제가 없다.
        headers = {"Accept": "application/vnd.github.v3+json"}
        token = str(self.token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ------------------------------------------------------------------

    def list_repos(self) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{_API}/users/{self.username}/repos",
                headers=self._headers(),
                params={"per_page": 100, "page": page, "sort": "updated"},
                timeout=15,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
            batch = resp.json()
            if not batch:
                break
            repos.extend(
                {
                    "name": r["name"],
                    "description": r.get("description") or "",
                    "html_url": r["html_url"],
                    "updated_at": r.get("updated_at", ""),
                    "default_branch": r.get("default_branch", "main"),
                }
                for r in batch
            )
            if len(batch) < 100:
                break
            page += 1
        return repos

    def repo_exists(self, name: str) -> dict | None:
        resp = httpx.get(
            f"{_API}/repos/{self.username}/{name}",
            headers=self._headers(),
            timeout=10,
        )
        return resp.json() if resp.status_code == 200 else None

    def detect_default_branch(self, owner: str, repo: str) -> str:
        try:
            resp = httpx.get(f"{_API}/repos/{owner}/{repo}", headers=self._headers(), timeout=10)
            return resp.json().get("default_branch") or "main"
        except Exception:
            return "main"

    def create_repo(self, name: str) -> str:
        resp = httpx.post(
            f"{_API}/user/repos",
            headers=self._headers(),
            json={"name": name, "private": False, "auto_init": False},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GitHub repo 생성 실패: {resp.status_code} {resp.text[:200]}")
        return resp.json()["html_url"]

    def fetch_file(self, owner: str, repo: str, branch: str, path: str) -> bytes:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        raw_headers = {"User-Agent": "process-gpt-deepagents/1.0"}
        token = str(self.token or "").strip()
        if token:
            raw_headers["Authorization"] = f"Bearer {token}"
        req = Request(url, headers=raw_headers)
        with urlopen(req, timeout=60) as r:
            return r.read()

    def list_repo_tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("tree") or []

    def get_authenticated_clone_url(self, owner: str, repo_name: str) -> str:
        return f"https://{self.username}:{self.token}@github.com/{owner}/{repo_name}.git"

    def get_repo_html_url(self, owner: str, repo_name: str) -> str:
        return f"https://github.com/{owner}/{repo_name}"

    def list_branches(self, owner: str, repo: str) -> list[dict]:
        resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/branches",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        return [{"name": b["name"], "sha": b["commit"]["sha"]} for b in resp.json()]

    def list_commits(
        self, owner: str, repo: str, branch: str | None = None, limit: int = 20
    ) -> list[dict]:
        params: dict = {"per_page": limit}
        if branch:
            params["sha"] = branch
        resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/commits",
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
            f"{_API}/repos/{owner}/{repo}/contents/{path}",
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
            f"{_API}/repos/{owner}/{repo}/contents/{path}",
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
            f"{_API}/repos/{owner}/{repo}/contents/{path}",
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
            f"{_API}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        commit = data.get("commit", {})
        return {"sha": commit.get("sha", ""), "html_url": commit.get("html_url", "")}

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str) -> dict:
        ref_resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
            headers=self._headers(),
            timeout=10,
        )
        ref_resp.raise_for_status()
        sha = ref_resp.json()["object"]["sha"]
        resp = httpx.post(
            f"{_API}/repos/{owner}/{repo}/git/refs",
            headers=self._headers(),
            json={"ref": f"refs/heads/{new_branch}", "sha": sha},
            timeout=15,
        )
        resp.raise_for_status()
        return {"name": new_branch, "sha": sha}

    def merge_pull_request(self, owner: str, repo: str, number: int, message: str = "") -> dict:
        resp = httpx.put(
            f"{_API}/repos/{owner}/{repo}/pulls/{number}/merge",
            headers=self._headers(),
            json={"commit_message": message or f"Merge pull request #{number}", "merge_method": "merge"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"sha": data.get("sha", ""), "merged": data.get("merged", True)}

    def create_pull_request(
        self, owner: str, repo: str, title: str, body: str, head: str, base: str
    ) -> dict:
        resp = httpx.post(
            f"{_API}/repos/{owner}/{repo}/pulls",
            headers=self._headers(),
            json={"title": title, "body": body, "head": head, "base": base},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["number"], "html_url": data["html_url"], "state": data["state"]}

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/pulls",
            headers=self._headers(),
            params={"state": state, "per_page": 100},
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
            f"{_API}/repos/{owner}/{repo}",
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code != 204:
            raise RuntimeError(f"GitHub repo 삭제 실패: {resp.status_code} {resp.text[:200]}")

    def get_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict]:
        resp = httpx.get(
            f"{_API}/repos/{owner}/{repo}/pulls/{number}/files",
            headers=self._headers(),
            params={"per_page": 100},
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
