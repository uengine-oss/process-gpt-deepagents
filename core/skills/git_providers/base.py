"""core/skills/git_providers/base.py — Git provider 추상 인터페이스."""

from __future__ import annotations

from abc import ABC, abstractmethod


class GitProvider(ABC):
    """Git 서비스(GitHub/GitLab/Gitea 등)의 공통 인터페이스."""

    def __init__(self, username: str, token: str, base_url: str | None = None) -> None:
        self.username = username
        self.token = token
        self.base_url = base_url

    # ------------------------------------------------------------------
    # 레포지토리 조회
    # ------------------------------------------------------------------

    @abstractmethod
    def list_repos(self) -> list[dict]:
        """인증된 사용자가 소유한 레포 목록을 반환한다.

        반환 형식(각 항목):
          name, description, html_url, updated_at, default_branch
        """

    @abstractmethod
    def repo_exists(self, name: str) -> dict | None:
        """레포가 존재하면 레포 정보 dict를, 없으면 None을 반환한다."""

    @abstractmethod
    def detect_default_branch(self, owner: str, repo: str) -> str:
        """원격 레포의 default branch 이름을 반환한다."""

    # ------------------------------------------------------------------
    # 레포지토리 쓰기
    # ------------------------------------------------------------------

    @abstractmethod
    def create_repo(self, name: str) -> str:
        """새 레포를 생성하고 html_url을 반환한다."""

    # ------------------------------------------------------------------
    # 파일 접근
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_file(self, owner: str, repo: str, branch: str, path: str) -> bytes:
        """원격 레포의 파일 내용을 bytes로 반환한다."""

    @abstractmethod
    def list_repo_tree(self, owner: str, repo: str, branch: str) -> list[dict]:
        """레포 트리의 모든 항목을 반환한다.

        각 항목은 최소 {"path": str, "type": "blob"|"tree"} 를 포함한다.
        """

    # ------------------------------------------------------------------
    # URL 헬퍼
    # ------------------------------------------------------------------

    @abstractmethod
    def get_authenticated_clone_url(self, owner: str, repo_name: str) -> str:
        """인증 정보가 포함된 clone URL을 반환한다."""

    def get_repo_html_url(self, owner: str, repo_name: str) -> str:
        """레포의 웹 URL을 반환한다 (기본 구현 — 필요 시 오버라이드)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 브랜치 / 커밋 이력
    # ------------------------------------------------------------------

    @abstractmethod
    def list_branches(self, owner: str, repo: str) -> list[dict]:
        """브랜치 목록을 반환한다. 각 항목: {name, sha}"""

    @abstractmethod
    def list_commits(
        self, owner: str, repo: str, branch: str | None = None, limit: int = 20
    ) -> list[dict]:
        """커밋 이력을 반환한다. 각 항목: {sha, message, author, date}"""

    # ------------------------------------------------------------------
    # 파일 쓰기 (API 기반)
    # ------------------------------------------------------------------

    @abstractmethod
    def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> dict:
        """파일을 생성하거나 업데이트하고 {sha, html_url}을 반환한다."""

    @abstractmethod
    def delete_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        branch: str,
    ) -> dict:
        """파일을 삭제하고 커밋한다. {sha, html_url}을 반환한다."""

    # ------------------------------------------------------------------
    # 브랜치 쓰기
    # ------------------------------------------------------------------

    @abstractmethod
    def create_branch(self, owner: str, repo: str, new_branch: str, from_branch: str) -> dict:
        """브랜치를 생성하고 {name, sha}를 반환한다."""

    # ------------------------------------------------------------------
    # Pull Request / Merge Request
    # ------------------------------------------------------------------

    @abstractmethod
    def merge_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        message: str = "",
    ) -> dict:
        """PR/MR을 merge하고 {sha, merged}를 반환한다."""

    @abstractmethod
    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        """PR/MR을 생성하고 {number, html_url, state}를 반환한다."""

    @abstractmethod
    def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
    ) -> list[dict]:
        """PR/MR 목록을 반환한다. 각 항목: {number, title, html_url, state, head, base}"""

    # ------------------------------------------------------------------
    # 레포지토리 삭제
    # ------------------------------------------------------------------

    @abstractmethod
    def delete_repo(self, owner: str, repo: str) -> None:
        """레포지토리를 삭제한다."""
