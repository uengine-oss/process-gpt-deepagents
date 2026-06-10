"""Fetch GitHub repository skills and write them to disk at invoke time.

Flow
----
1. ``prefetch_github_skills(repo_urls)``
   Downloads SKILL.md for every skill found in every configured repo.
   Skills are written to ``{skills_root}/{repo-slug}/{skill-name}/SKILL.md``.
   The skill-name is the immediate parent directory of SKILL.md in the repo
   (handles repos where skills are nested under a subdirectory).

2. ``fetch_github_skill_file(skill_path)``
   Fetches any file belonging to a GitHub skill on demand, writes it to disk,
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
SKILLS_DIRS        Comma-separated skill directories (first entry used as GitHub skills root)
SKILL_REPO_URLS    Comma-separated GitHub repo URLs to fetch skills from
GITHUB_TOKEN       GitHub API token (optional, increases rate limits)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

_DEFAULT_SKILL_REPOS: list[str] = [
    "https://github.com/anthropics/skills",
]

# slug → (owner, repo, branch)
_slug_registry: dict[str, tuple[str, str, str]] = {}
# slug → {skill_name: repo_dir_path}
# repo_dir_path is the path *within the repo* that contains SKILL.md
# e.g. "template" or "skills/algorithmic-art"
_skill_paths: dict[str, dict[str, str]] = {}
# skill_name → slug (last-write-wins for cross-repo conflicts)
_skill_owner: dict[str, str] = {}

_tree_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _tree_lock
    if _tree_lock is None:
        _tree_lock = asyncio.Lock()
    return _tree_lock


def _resolve_skills_root() -> str:
    _base = Path(__file__).parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_base / "skills"))
    first = raw.split(",")[0].strip() or str(_base / "skills")
    if not os.path.isabs(first):
        first = str((_base / first).resolve())
    return first.rstrip("/")


def _parse_github_url(url: str) -> tuple[str, str]:
    import re
    m = re.match(
        r"https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$",
        (url or "").strip(),
        re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Not a GitHub repo URL: {url!r}")
    return m.group(1), m.group(2)


def _repo_slug(owner: str, repo: str) -> str:
    return f"{owner}-{repo}".lower().replace("_", "-")


def _configured_repos() -> list[str]:
    env = [u.strip() for u in (os.environ.get("SKILL_REPO_URLS") or "").split(",") if u.strip()]
    return env or _DEFAULT_SKILL_REPOS


def _github_api_get(url: str, token: str | None = None) -> dict:
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "process-gpt-deepagents/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _fetch_raw_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "process-gpt-deepagents/1.0"})
    with urlopen(req, timeout=60) as resp:
        return resp.read()


def _fetch_repo_skill_map(
    owner: str, repo: str, branch: str, token: str | None
) -> dict[str, str]:
    """Return {skill_name: repo_dir_path} by scanning for SKILL.md at any depth.

    repo_dir_path is the path within the repo that contains SKILL.md,
    e.g. "template" or "skills/algorithmic-art".
    skill_name is the last path component (immediate parent of SKILL.md).
    """
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    data = _github_api_get(api_url, token)
    tree_items = data.get("tree") or []

    skill_map: dict[str, str] = {}
    for item in tree_items:
        if item.get("type") != "blob":
            continue
        path: str = item["path"]
        if not path.endswith("/SKILL.md"):
            continue
        # repo_dir_path = everything before /SKILL.md
        repo_dir_path = path[: -len("/SKILL.md")]
        # skill_name = immediate parent directory
        skill_name = repo_dir_path.rsplit("/", 1)[-1]
        if skill_name:
            skill_map[skill_name] = repo_dir_path

    return skill_map


def _detect_default_branch(owner: str, repo: str, token: str | None) -> str:
    try:
        data = _github_api_get(f"https://api.github.com/repos/{owner}/{repo}", token)
        return data.get("default_branch") or "main"
    except Exception:
        return "main"


async def _ensure_repo_skill_map(
    owner: str, repo: str, token: str | None
) -> tuple[str, dict[str, str]]:
    """Return (branch, skill_map) for a repo, using in-process cache."""
    slug = _repo_slug(owner, repo)
    if slug in _skill_paths:
        branch = _slug_registry[slug][2]
        return branch, _skill_paths[slug]

    branch = await asyncio.to_thread(_detect_default_branch, owner, repo, token)
    skill_map = await asyncio.to_thread(_fetch_repo_skill_map, owner, repo, branch, token)
    _slug_registry[slug] = (owner, repo, branch)
    _skill_paths[slug] = skill_map
    for skill_name in skill_map:
        _skill_owner[skill_name] = slug
    logger.info(
        "github-skills: indexed %s/%s@%s → %d skills: %s",
        owner, repo, branch, len(skill_map), sorted(skill_map),
    )
    return branch, skill_map


async def prefetch_github_skills(repo_urls: list[str] | None = None) -> int:
    """Download SKILL.md for every skill in every configured repo and write to disk.

    Skills are written to ``{skills_root}/{slug}/{skill_name}/SKILL.md``.
    The skills_root is derived from the first SKILLS_DIRS entry.
    Returns the number of SKILL.md files written.
    """
    root = _resolve_skills_root()
    urls = repo_urls or _configured_repos()
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_TOKEN") or "").strip() or None

    written = 0
    async with _get_lock():
        index_tasks = [_ensure_repo_skill_map(*_parse_github_url(u), token) for u in urls]
        results = await asyncio.gather(*index_tasks, return_exceptions=True)

    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            logger.exception("github-skills: failed to index %s: %s", url, result)
            continue

        owner, repo = _parse_github_url(url)
        slug = _repo_slug(owner, repo)
        branch, skill_map = result
        raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"

        fetch_tasks = []
        disk_paths: list[Path] = []
        for skill_name, repo_dir_path in skill_map.items():
            raw_url = f"{raw_base}/{repo_dir_path}/SKILL.md"
            disk_path = Path(root) / slug / skill_name / "SKILL.md"
            fetch_tasks.append(asyncio.to_thread(_fetch_raw_bytes, raw_url))
            disk_paths.append(disk_path)

        contents = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for disk_path, content in zip(disk_paths, contents):
            if isinstance(content, Exception):
                logger.warning("github-skills: could not fetch %s: %s", disk_path, content)
                continue
            try:
                disk_path.parent.mkdir(parents=True, exist_ok=True)
                disk_path.write_bytes(content)
                written += 1
            except Exception as exc:
                logger.warning("github-skills: could not write %s: %s", disk_path, exc)

    logger.info("github-skills: wrote %d SKILL.md files under %s", written, root)
    return written


def fetch_github_skill_file(skill_path: str) -> str:
    """Fetch a GitHub skill file on demand, write it to disk, and return its content.

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
            "prefetch_github_skills() must be called before reading skill files."
        )
    if skill_name not in _skill_paths.get(slug, {}):
        raise ValueError(f"Unknown skill {skill_name!r} in repo {slug!r}.")

    owner, repo, branch = _slug_registry[slug]
    repo_dir_path = _skill_paths[slug][skill_name]
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{repo_dir_path}/{filename}"

    try:
        raw = _fetch_raw_bytes(raw_url)
        content = raw.decode("utf-8", errors="replace")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except Exception as exc:
            logger.warning("github-skills: could not cache %s: %s", path, exc)
        return content
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch {raw_url}: {exc}") from exc
