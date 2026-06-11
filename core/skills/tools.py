"""core/skills/tools.py — 딥 에이전트 및 skill-creator 서브에이전트 공용 툴.

extract_document_structure, list_github_repos, publish_skill을 제공한다.
셸 실행은 DockerSandboxBackend가 제공하는 execute() 툴을 통해 격리 컨테이너에서 수행된다.
"""

from __future__ import annotations

import contextvars
import os
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from parsers import extract as _parse_document

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent.parent / "workspace"))).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

# 현재 요청의 tenant_id — executor에서 set() 호출
_current_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_tenant_id", default=""
)
_current_user_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_name", default=""
)
_current_user_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_email", default=""
)


def _substitute_ws(s: str) -> str:
    """미치환 {ws} 플레이스홀더를 실제 workspace root로 교체한다."""
    return s.replace("{ws}", str(WORKSPACE_ROOT))


@tool
def extract_document_structure(file_path: str) -> str:
    """Parse a document and return its full structure, including all annotation elements.

    Supports: .docx (review comments, footnotes, endnotes, track changes),
              .pdf  (annotations, highlights, sticky notes),
              .txt / .md (HTML comments, GFM callouts, Markdown link titles).
    Unknown formats fall back to raw body text only.

    Returns a human-readable structured representation that the LLM can use
    to identify (anchor_text, annotation) pairs for knowledge extraction.
    """
    try:
        doc = _parse_document(_substitute_ws(file_path))
        return doc.to_llm_text()
    except Exception as e:
        return f"ERROR parsing {file_path}: {e}"


# ---------------------------------------------------------------------------
# GitHub 헬퍼
# ---------------------------------------------------------------------------

def _get_github_creds() -> tuple[str, str]:
    """tenant_oauth 테이블에서 현재 테넌트의 GitHub 자격증명을 반환한다."""
    from ..storage.db import get_supabase

    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        raise ValueError("tenant_id가 설정되지 않았습니다.")

    sb = get_supabase()
    resp = (
        sb.table("tenant_oauth")
        .select("github_username, github_token")
        .eq("tenant_id", tenant_id)
        .limit(1)
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    if not rows or not rows[0].get("github_username") or not rows[0].get("github_token"):
        raise ValueError(
            "GitHub 계정이 연결되지 않았습니다. 설정에서 GitHub 계정을 연결해 주세요."
        )
    return rows[0]["github_username"], rows[0]["github_token"]


def _github_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _push_skill(
    src: Path,
    skill_name: str,
    branch: str,
    username: str,
    token: str,
    *,
    existing_repo: bool = False,
) -> str:
    repo_url = f"https://{username}:{token}@github.com/{username}/{skill_name}.git"

    author_name = _current_user_name.get() or "Skill Creator Agent"
    author_email = _current_user_email.get() or "skill-creator@agent"

    if not (src / ".git").exists():
        _git(src, "init", "-b", "main")
        _git(src, "remote", "add", "origin", repo_url)
    else:
        _git(src, "remote", "set-url", "origin", repo_url)

    _git(src, "config", "user.email", author_email)
    _git(src, "config", "user.name", author_name)

    if existing_repo:
        fetch_code, _ = _git(src, "fetch", "origin", "main", "--depth=1")
        if fetch_code == 0:
            _git(src, "reset", "--soft", "FETCH_HEAD")

    _git(src, "add", ".")
    verb = "update" if branch == "main" else f"add branch {branch}"
    code, out = _git(src, "commit", "-m", f"feat: {verb} {skill_name}")
    if code != 0 and "nothing to commit" not in out:
        return f"ERROR git commit: {out}"

    if branch != "main":
        _git(src, "checkout", "-b", branch)

    push_args = ["push", "-u", "origin", branch]
    if not existing_repo:
        push_args.append("--force")
    code, out = _git(src, *push_args)
    if code != 0:
        return f"ERROR git push: {out}"

    return f"Published '{skill_name}' → https://github.com/{username}/{skill_name}/tree/{branch}"


@tool
def list_github_repos() -> str:
    """List all GitHub repositories owned by the configured tenant's GitHub account.

    Returns a JSON array of objects with name, description, html_url, updated_at,
    and default_branch. Use this to check whether a skill already exists before
    creating a new one.
    """
    import httpx
    import json

    try:
        username, token = _get_github_creds()
    except ValueError as e:
        return f"ERROR: {e}"

    headers = _github_headers(token)
    repos: list[dict] = []
    page = 1

    try:
        while True:
            resp = httpx.get(
                f"https://api.github.com/users/{username}/repos",
                headers=headers,
                params={"per_page": 100, "page": page, "sort": "updated"},
                timeout=15,
            )
            if resp.status_code != 200:
                return f"ERROR: {resp.status_code} {resp.text[:200]}"
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
    except httpx.RequestError as e:
        return f"ERROR (network): {e}"

    return json.dumps(repos, ensure_ascii=False, indent=2)


@tool
def publish_skill(skill_path: str, branch: str | None = None) -> str:
    """Publish a completed skill to its own GitHub repository.

    **Two-phase flow:**

    Phase 1 — call without `branch`:
      - If the GitHub repo does NOT exist: creates it and pushes to main.
        Returns the published URL.
      - If the GitHub repo ALREADY exists: returns a "CONFIRM:" message.
        You must then call `ask_user` to ask whether to update main or use a
        new branch, and call this tool again with the chosen branch.

    Phase 2 — call with `branch` after user confirmation:
      - Pushes to the specified branch (use "main" to overwrite, or e.g. "v2"
        to preserve the current version).

    Args:
        skill_path: Absolute path to the completed skill folder (in workspace).
        branch:     Target branch. Omit on first call; set after user confirms.
    """
    import httpx

    try:
        username, token = _get_github_creds()
    except ValueError as e:
        return f"ERROR: {e}"

    src = Path(skill_path).expanduser().resolve()
    if not src.is_dir():
        return f"ERROR: not a directory: {skill_path}"

    skill_name = src.name
    headers = _github_headers(token)

    try:
        check = httpx.get(
            f"https://api.github.com/repos/{username}/{skill_name}",
            headers=headers,
            timeout=10,
        )
    except httpx.RequestError as e:
        return f"ERROR (network): {e}"

    repo_exists = check.status_code == 200

    if branch is None:
        if repo_exists:
            url = check.json()["html_url"]
            return (
                f"CONFIRM: '{skill_name}' 레포가 이미 존재합니다 ({url}). "
                "ask_user를 호출해 main에 커밋으로 업데이트할지, "
                "새 브랜치명을 쓸지 확인한 뒤 "
                "branch 값을 지정해서 publish_skill을 다시 호출하세요."
            )
        resp = httpx.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json={"name": skill_name, "private": False, "auto_init": False},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return f"ERROR creating GitHub repo: {resp.status_code} {resp.text[:200]}"
        return _push_skill(src, skill_name, "main", username, token, existing_repo=False)
    elif check.status_code not in (200, 404):
        return f"ERROR checking repo: {check.status_code} {check.text[:200]}"

    return _push_skill(src, skill_name, branch, username, token, existing_repo=repo_exists)
