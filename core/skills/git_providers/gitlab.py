"""core/skills/git_providers/gitlab.py — GitLab API 구현."""

from __future__ import annotations

from urllib.parse import quote
from urllib.request import Request, urlopen

import httpx

from .base import GitProvider

_GITLAB_COM = "https://gitlab.com"


class GitLabProvider(GitProvider):

    def __init__(self, username: str, token: str, base_url: str | None = None) -> None:
        super().__init__(username, token, base_url)
        self._base = (base_url or _GITLAB_COM).rstrip("/")
        self._api = f"{self._base}/api/v4"
        # DB에 저장된 username이 그룹명 등으로 잘못 설정된 경우를 대비해
        # 토큰 소유자의 실제 username으로 덮어쓴다.
        self.username = self._resolve_username(username)

    def _resolve_username(self, stored: str) -> str:
        try:
            resp = httpx.get(f"{self._api}/user", headers=self._headers(), timeout=10)
            if resp.status_code == 200:
                actual = resp.json().get("username", "")
                return actual if actual else stored
        except Exception:
            pass
        return stored

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token}

    # ------------------------------------------------------------------

    def list_repos(self) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self._api}/projects",
                headers=self._headers(),
                params={"owned": True, "per_page": 100, "page": page, "order_by": "updated_at"},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend(
                {
                    "name": r["path"],
                    "description": r.get("description") or "",
                    "html_url": r["web_url"],
                    "updated_at": r.get("last_activity_at", ""),
                    "default_branch": r.get("default_branch", "main"),
                }
                for r in batch
            )
            if len(batch) < 100:
                break
            page += 1
        return repos

    def repo_exists(self, name: str) -> dict | None:
        encoded = quote(f"{self.username}/{name}", safe="")
        resp = httpx.get(f"{self._api}/projects/{encoded}", headers=self._headers(), timeout=10)
        return resp.json() if resp.status_code == 200 else None

    def detect_default_branch(self, owner: str, repo: str) -> str:
        try:
            encoded = quote(f"{owner}/{repo}", safe="")
            resp = httpx.get(f"{self._api}/projects/{encoded}", headers=self._headers(), timeout=10)
            return resp.json().get("default_branch") or "main"
        except Exception:
            return "main"

    def create_repo(self, name: str) -> str:
        resp = httpx.post(
            f"{self._api}/projects",
            headers=self._headers(),
            json={"name": name, "path": name, "visibility": "public"},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GitLab 프로젝트 생성 실패: {resp.status_code} {resp.text[:200]}")
        return resp.json()["web_url"]

    def fetch_file(self, owner: str, repo: str, branch: str, path: str) -> bytes:
        encoded_project = quote(f"{owner}/{repo}", safe="")
        encoded_path = quote(path, safe="")
        url = f"{self._api}/projects/{encoded_project}/repository/files/{encoded_path}/raw?ref={branch}"
        req = Request(url, headers={**self._headers(), "User-Agent": "process-gpt-deepagents/1.0"})
        with urlopen(req, timeout=60) as r:
            return r.read()

    def list_repo_tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        encoded = quote(f"{owner}/{repo}", safe="")
        items: list[dict] = []
        page = 1
        while True:
            resp = httpx.get(
                f"{self._api}/projects/{encoded}/repository/tree",
                headers=self._headers(),
                params={"recursive": True, "per_page": 100, "page": page, "ref": branch},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            items.extend(
                {"path": item["path"], "type": "blob" if item["type"] == "blob" else "tree"}
                for item in batch
            )
            if len(batch) < 100:
                break
            page += 1
        return items

    def get_authenticated_clone_url(self, owner: str, repo_name: str) -> str:
        host = self._base.removeprefix("https://").removeprefix("http://")
        scheme = "https" if self._base.startswith("https") else "http"
        return f"{scheme}://oauth2:{self.token}@{host}/{owner}/{repo_name}.git"

    def get_repo_html_url(self, owner: str, repo_name: str) -> str:
        return f"{self._base}/{owner}/{repo_name}"

    def list_branches(self, owner: str, repo: str) -> list[dict]:
        encoded = quote(f"{owner}/{repo}", safe="")
        resp = httpx.get(
            f"{self._api}/projects/{encoded}/repository/branches",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15,
        )
        # GitLab returns 404 for empty repos (no commits yet)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [{"name": b["name"], "sha": b["commit"]["id"]} for b in resp.json()]

    def list_commits(
        self, owner: str, repo: str, branch: str | None = None, limit: int = 20
    ) -> list[dict]:
        encoded = quote(f"{owner}/{repo}", safe="")
        params: dict = {"per_page": limit}
        if branch:
            params["ref_name"] = branch
        resp = httpx.get(
            f"{self._api}/projects/{encoded}/repository/commits",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return [
            {
                "sha": c["id"],
                "message": c["message"],
                "author": c["author_name"],
                "date": c["authored_date"],
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

        encoded_project = quote(f"{owner}/{repo}", safe="")
        encoded_path = quote(path, safe="")
        body: dict = {
            "branch": branch,
            "content": base64.b64encode(content.encode()).decode(),
            "commit_message": message,
            "encoding": "base64",
        }
        if author_name:
            body["author_name"] = author_name
        if author_email:
            body["author_email"] = author_email
        existing = httpx.get(
            f"{self._api}/projects/{encoded_project}/repository/files/{encoded_path}",
            headers=self._headers(),
            params={"ref": branch},
            timeout=10,
        )
        method = httpx.put if existing.status_code == 200 else httpx.post
        resp = method(
            f"{self._api}/projects/{encoded_project}/repository/files/{encoded_path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "sha": data.get("content_sha256", ""),
            "html_url": self.get_repo_html_url(owner, repo),
        }

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
        encoded_project = quote(f"{owner}/{repo}", safe="")
        encoded_path = quote(path, safe="")
        existing = httpx.get(
            f"{self._api}/projects/{encoded_project}/repository/files/{encoded_path}",
            headers=self._headers(),
            params={"ref": branch},
            timeout=10,
        )
        if existing.status_code != 200:
            raise FileNotFoundError(f"파일 '{path}'을 찾을 수 없습니다.")
        body: dict = {"branch": branch, "commit_message": message}
        if author_name:
            body["author_name"] = author_name
        if author_email:
            body["author_email"] = author_email
        resp = httpx.request(
            "DELETE",
            f"{self._api}/projects/{encoded_project}/repository/files/{encoded_path}",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return {"sha": "", "html_url": self.get_repo_html_url(owner, repo)}

    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str) -> dict:
        encoded = quote(f"{owner}/{repo}", safe="")
        resp = httpx.post(
            f"{self._api}/projects/{encoded}/repository/branches",
            headers=self._headers(),
            json={"branch": new_branch, "ref": from_branch},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"name": data["name"], "sha": data["commit"]["id"]}

    def merge_pull_request(self, owner: str, repo: str, number: int, message: str = "") -> dict:
        encoded = quote(f"{owner}/{repo}", safe="")
        body: dict = {}
        if message:
            body["merge_commit_message"] = message
        resp = httpx.put(
            f"{self._api}/projects/{encoded}/merge_requests/{number}/merge",
            headers=self._headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"sha": data.get("merge_commit_sha", ""), "merged": data.get("state") == "merged"}

    def create_pull_request(
        self, owner: str, repo: str, title: str, body: str, head: str, base: str
    ) -> dict:
        encoded = quote(f"{owner}/{repo}", safe="")
        resp = httpx.post(
            f"{self._api}/projects/{encoded}/merge_requests",
            headers=self._headers(),
            json={
                "title": title,
                "description": body,
                "source_branch": head,
                "target_branch": base,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"number": data["iid"], "html_url": data["web_url"], "state": data["state"]}

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[dict]:
        encoded = quote(f"{owner}/{repo}", safe="")
        _STATE_MAP = {"open": "opened", "closed": "closed", "all": None}
        gl_state = _STATE_MAP.get(state, "opened")
        params: dict = {"per_page": 100}
        if gl_state is not None:
            params["state"] = gl_state
        resp = httpx.get(
            f"{self._api}/projects/{encoded}/merge_requests",
            headers=self._headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return [
            {
                "number": mr["iid"],
                "title": mr["title"],
                "html_url": mr["web_url"],
                "state": mr["state"],
                "head": mr["source_branch"],
                "base": mr["target_branch"],
            }
            for mr in resp.json()
        ]

    def delete_repo(self, owner: str, repo: str) -> None:
        encoded = quote(f"{owner}/{repo}", safe="")
        resp = httpx.delete(
            f"{self._api}/projects/{encoded}",
            headers=self._headers(),
            timeout=15,
        )
        # 이미 없는 레포(404)는 '삭제 완료'와 같게 다룬다. DELETE 는 멱등해야 하고,
        # 여기서 예외를 던지면 호출부가 로컬 정리까지 건너뛰어 스킬이 영영 안 지워진다.
        if resp.status_code not in (202, 204, 404):
            raise RuntimeError(f"GitLab 프로젝트 삭제 실패: {resp.status_code} {resp.text[:200]}")

    def get_pull_request_files(self, owner: str, repo: str, number: int) -> list[dict]:
        encoded = quote(f"{owner}/{repo}", safe="")
        resp = httpx.get(
            f"{self._api}/projects/{encoded}/merge_requests/{number}/changes",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        changes = resp.json().get("changes") or []
        result = []
        for c in changes:
            if c.get("new_file"):
                status = "added"
            elif c.get("deleted_file"):
                status = "removed"
            elif c.get("renamed_file"):
                status = "renamed"
            else:
                status = "modified"
            filename = c.get("new_path") or c.get("old_path", "")
            patch = c.get("diff", "")
            additions = sum(1 for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in patch.splitlines() if line.startswith("-") and not line.startswith("---"))
            result.append({
                "filename": filename,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "patch": patch,
            })
        return result
