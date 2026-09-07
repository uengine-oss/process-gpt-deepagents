"""Fetch Git repository skills and write them to disk at invoke time.

Flow
----
1. ``prefetch_git_skills(repo_urls)``
   Downloads SKILL.md for every skill found in every configured repo.
   Skills are written to ``{skills_root}/{repo-slug}/{skill-name}/SKILL.md``.
   The skill-name is the immediate parent directory of SKILL.md in the repo
   (handles repos where skills are nested under a subdirectory).

2. ``fetch_git_skill_file(skill_path)``
   Fetches any file belonging to a Git skill on demand, writes it to disk,
   and returns its content.

Directory layout on disk
------------------------
  {first SKILLS_DIRS entry}/
    {repo-slug}/                 e.g. anthropics-skills/
      {skill-name}/              e.g. algorithmic-art/
        SKILL.md
        run.py   ← fetched on demand

Environment variables
---------------------
SKILLS_DIRS        Comma-separated skill directories (first entry used as Git skills root)
SKILL_REPO_URLS    Comma-separated Git repo URLs to fetch skills from
GITHUB_TOKEN       GitHub API token
GITLAB_TOKEN       GitLab Personal Access Token
GITEA_TOKEN        Gitea token
GITEA_BASE_URL     Gitea instance URL (e.g. https://gitea.example.com)
GITEA_USERNAME     Gitea username
GITLAB_USERNAME    GitLab username
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_SKILL_REPOS: list[str] = [
    "https://github.com/anthropics/skills",
]

# slug → (owner, repo, branch, original_url)
_slug_registry: dict[str, tuple[str, str, str, str]] = {}
# slug → {skill_name: repo_dir_path}
_skill_paths: dict[str, dict[str, str]] = {}
# skill_name → slug (cross-repo 충돌 시 last-write-wins)
_skill_owner: dict[str, str] = {}

_tree_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _tree_lock
    if _tree_lock is None:
        _tree_lock = asyncio.Lock()
    return _tree_lock


def _resolve_skills_root() -> str:
    _base = Path(__file__).parent.parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_base / "skills"))
    first = raw.split(",")[0].strip() or str(_base / "skills")
    if not os.path.isabs(first):
        first = str((_base / first).resolve())
    return first.rstrip("/")


def _parse_repo_url(url: str) -> tuple[str, str]:
    """URL에서 (owner, repo)를 추출한다."""
    import re
    m = re.match(
        r"(?:https?://[^/]+/|git@[^:]+:)([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$",
        (url or "").strip(),
        re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Not a valid Git repo URL: {url!r}")
    return m.group(1), m.group(2)


def _repo_slug(owner: str, repo: str) -> str:
    return f"{owner}-{repo}".lower().replace("_", "-")


def _configured_repos() -> list[str]:
    env = [u.strip() for u in (os.environ.get("SKILL_REPO_URLS") or "").split(",") if u.strip()]
    return env or _DEFAULT_SKILL_REPOS


def _fetch_repo_skill_map(url: str) -> tuple[str, dict[str, str]]:
    """(branch, {skill_name: repo_dir_path}) 반환."""
    from .git_providers.factory import get_provider_from_url

    owner, repo = _parse_repo_url(url)
    provider = get_provider_from_url(url)
    branch = provider.detect_default_branch(owner, repo)
    tree = provider.list_repo_tree(owner, repo, branch)

    skill_map: dict[str, str] = {}
    for item in tree:
        if item.get("type") != "blob":
            continue
        path: str = item["path"]
        if not path.endswith("/SKILL.md"):
            continue
        repo_dir_path = path[: -len("/SKILL.md")]
        skill_name = repo_dir_path.rsplit("/", 1)[-1]
        if skill_name:
            skill_map[skill_name] = repo_dir_path

    return branch, skill_map


async def _ensure_repo_skill_map(url: str) -> tuple[str, dict[str, str]]:
    """(branch, skill_map) 반환. in-process 캐시 사용."""
    owner, repo = _parse_repo_url(url)
    slug = _repo_slug(owner, repo)

    if slug in _skill_paths:
        _owner, _repo, branch, _url = _slug_registry[slug]
        return branch, _skill_paths[slug]

    branch, skill_map = await asyncio.to_thread(_fetch_repo_skill_map, url)
    _slug_registry[slug] = (owner, repo, branch, url)
    _skill_paths[slug] = skill_map
    for skill_name in skill_map:
        _skill_owner[skill_name] = slug
    logger.info(
        "git-skills: indexed %s/%s@%s → %d skills: %s",
        owner, repo, branch, len(skill_map), sorted(skill_map),
    )
    return branch, skill_map


async def prefetch_git_skills(repo_urls: list[str] | None = None) -> int:
    """Download SKILL.md for every skill in every configured repo and write to disk.

    Skills are written to ``{skills_root}/{slug}/{skill_name}/SKILL.md``.
    Returns the number of SKILL.md files written.
    """
    root = _resolve_skills_root()
    urls = repo_urls or _configured_repos()

    written = 0
    async with _get_lock():
        index_tasks = [_ensure_repo_skill_map(u) for u in urls]
        results = await asyncio.gather(*index_tasks, return_exceptions=True)

    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            logger.exception("git-skills: failed to index %s: %s", url, result)
            continue

        owner, repo = _parse_repo_url(url)
        slug = _repo_slug(owner, repo)
        branch, skill_map = result

        from .git_providers.factory import get_provider_from_url
        provider = get_provider_from_url(url)

        # 이미 디스크에 있는 SKILL.md 는 다시 받지 않는다.
        # 이 함수는 **채팅 턴마다** 호출되므로, 매번 전부 내려받으면 턴마다 스킬 수만큼
        # 네트워크 요청이 나간다(익명 GitHub rate limit 소진 + 지연). 게다가 그 요청은
        # `asyncio.to_thread` 안의 `urlopen(timeout=60)` 이라 **취소되지 않는다** — 느려지면
        # 그 턴이 취소에도 반응하지 않는 좀비가 되어 이후 턴의 HITL 재개까지 망가진다.
        # 새 스킬이 추가된 경우만 받으면 되므로 존재 여부로 거른다(갱신은 재기동/삭제 후 재시작).
        fetch_tasks = []
        disk_paths: list[Path] = []
        for skill_name, repo_dir_path in skill_map.items():
            disk_path = Path(root) / slug / skill_name / "SKILL.md"
            if disk_path.is_file():
                continue
            fetch_tasks.append(
                asyncio.to_thread(
                    provider.fetch_file, owner, repo, branch, f"{repo_dir_path}/SKILL.md"
                )
            )
            disk_paths.append(disk_path)

        if not fetch_tasks:
            logger.debug("git-skills: %s 는 이미 최신(다운로드 생략)", slug)
            continue

        contents = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for disk_path, content in zip(disk_paths, contents):
            if isinstance(content, Exception):
                logger.warning("git-skills: could not fetch %s: %s", disk_path, content)
                continue
            try:
                disk_path.parent.mkdir(parents=True, exist_ok=True)
                disk_path.write_bytes(content)
                written += 1
            except Exception as exc:
                logger.warning("git-skills: could not write %s: %s", disk_path, exc)

    logger.info("git-skills: wrote %d SKILL.md files under %s", written, root)
    return written


def fetch_git_skill_file(skill_path: str) -> str:
    """Fetch a Git skill file on demand, write it to disk, and return its content.

    ``skill_path`` must be an absolute path under the skills root:
      {skills_root}/{slug}/{skill_name}/{filename}

    Raises ``ValueError`` for unknown slugs/skills and ``RuntimeError`` on fetch errors.
    """
    root = Path(_resolve_skills_root()).resolve()
    path = Path(skill_path).resolve()

    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValueError(f"skill_path {skill_path!r} is not under skills root {root!r}")

    parts = rel.parts
    if len(parts) < 3:
        raise ValueError(f"skill_path must include slug/skill_name/filename: {skill_path!r}")

    slug = parts[0]
    skill_name = parts[1]
    filename = "/".join(parts[2:])

    if slug not in _slug_registry:
        raise ValueError(
            f"Unknown repo slug {slug!r}. "
            "prefetch_git_skills() must be called before reading skill files."
        )
    if skill_name not in _skill_paths.get(slug, {}):
        raise ValueError(f"Unknown skill {skill_name!r} in repo {slug!r}.")

    owner, repo, branch, original_url = _slug_registry[slug]
    repo_dir_path = _skill_paths[slug][skill_name]

    from .git_providers.factory import get_provider_from_url
    provider = get_provider_from_url(original_url)

    try:
        raw = provider.fetch_file(owner, repo, branch, f"{repo_dir_path}/{filename}")
        content = raw.decode("utf-8", errors="replace")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except Exception as exc:
            logger.warning("git-skills: could not cache %s: %s", path, exc)
        return content
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch {slug}/{skill_name}/{filename}: {exc}") from exc
