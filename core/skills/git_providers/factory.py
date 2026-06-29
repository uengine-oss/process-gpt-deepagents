"""core/skills/git_providers/factory.py — tenant/URL 기반 GitProvider 팩토리."""

from __future__ import annotations

import os
import re

from .base import GitProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider
from .gitea import GiteaProvider

_GITHUB_HOST_RE = re.compile(r"github\.com", re.IGNORECASE)
_GITLAB_HOST_RE = re.compile(r"gitlab\.", re.IGNORECASE)


def get_provider(tenant_id: str) -> GitProvider:
    """DB tenant_git_config에서 해당 테넌트의 기본 Git provider를 반환한다."""
    from ...storage.db import get_supabase

    if not tenant_id:
        raise ValueError(
            "tenant_id가 설정되지 않았습니다. ask_user를 호출하지 말고 작업을 중단하세요."
        )

    sb = get_supabase()

    # is_default=true 우선, 없으면 첫 번째 레코드 사용.
    # tenant_git_config 테이블이 없는 환경(로컬/dev)에서는 supabase 가 APIError(PGRST205)를
    # 던지는데, 이는 '미연결'과 동일하게 다뤄 ValueError 로 변환한다(호출 도구들이 ValueError 를
    # 잡아 graceful 폴백 — 스킬은 워크스페이스에 생성되고 프론트가 저장하므로 git 없이도 동작).
    try:
        resp = (
            sb.table("tenant_git_config")
            .select("id, provider, base_url, username, token")
            .eq("tenant_id", tenant_id)
            .eq("is_default", True)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []

        if not rows:
            resp = (
                sb.table("tenant_git_config")
                .select("id, provider, base_url, username, token")
                .eq("tenant_id", tenant_id)
                .limit(1)
                .execute()
            )
            rows = getattr(resp, "data", None) or []
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(
            "Git 계정이 연결되지 않았습니다(tenant_git_config 미설정/조회 불가). "
            f"ask_user를 호출하지 말고 git 없이 작업을 계속하세요. (detail: {e})"
        ) from e

    if not rows:
        raise ValueError(
            "Git 계정이 연결되지 않았습니다. "
            "ask_user를 호출하지 말고 작업을 중단하세요. "
            "사용자가 설정 화면에서 직접 Git 계정을 연결해야 합니다."
        )

    cfg = rows[0]
    provider = _build_provider(
        provider=cfg["provider"],
        username=cfg["username"],
        token=cfg["token"],
        base_url=cfg.get("base_url"),
    )
    provider.config_id = str(cfg["id"])
    return provider


def get_provider_from_url(url: str) -> GitProvider:
    """URL과 환경변수 토큰으로 Git provider를 반환한다 (시스템 레포용).

    우선순위:
      1. github.com → GitHubProvider (GITHUB_TOKEN)
      2. gitlab.com 또는 hostname에 'gitlab' 포함 → GitLabProvider (GITLAB_TOKEN)
      3. GITEA_BASE_URL 환경변수와 일치 → GiteaProvider (GITEA_TOKEN)
      4. 나머지 → GitLabProvider로 폴백 (self-hosted GitLab 가정)
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    base = f"{parsed.scheme}://{parsed.netloc}"

    gitea_base = (os.getenv("GITEA_BASE_URL") or "").rstrip("/")

    if _GITHUB_HOST_RE.search(host):
        token = (os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN") or "").strip()
        return GitHubProvider(username="", token=token)

    if gitea_base and base.rstrip("/") == gitea_base:
        token = (os.getenv("GITEA_TOKEN") or "").strip()
        username = (os.getenv("GITEA_USERNAME") or "").strip()
        return GiteaProvider(username=username, token=token, base_url=gitea_base)

    if _GITLAB_HOST_RE.search(host) or "gitlab" in host:
        token = (os.getenv("GITLAB_TOKEN") or "").strip()
        username = (os.getenv("GITLAB_USERNAME") or "").strip()
        actual_base = base if host != "gitlab.com" else None
        return GitLabProvider(username=username, token=token, base_url=actual_base)

    # 폴백: self-hosted GitLab
    token = (os.getenv("GITLAB_TOKEN") or "").strip()
    username = (os.getenv("GITLAB_USERNAME") or "").strip()
    return GitLabProvider(username=username, token=token, base_url=base)


def _build_provider(
    provider: str,
    username: str,
    token: str,
    base_url: str | None,
) -> GitProvider:
    match provider:
        case "github":
            return GitHubProvider(username=username, token=token, base_url=base_url)
        case "gitlab":
            return GitLabProvider(username=username, token=token, base_url=base_url)
        case "gitea":
            return GiteaProvider(username=username, token=token, base_url=base_url)
        case _:
            raise ValueError(f"지원하지 않는 Git provider: {provider!r}")
