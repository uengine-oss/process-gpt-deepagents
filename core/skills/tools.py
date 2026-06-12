"""core/skills/tools.py — 딥 에이전트 및 skill-creator 서브에이전트 공용 툴.

extract_document_structure, list_github_repos, publish_skill을 제공한다.

보안 원칙:
  - /skills/ 하위 파일 읽기는 호스트에서 허용한다.
  - 스킬 생성·수정·GitHub 배포 등 모든 쓰기 작업은 Docker sandbox 내부에서만 수행한다.
  - sandbox가 없으면 쓰기 작업은 거부된다.
  - /skills/ 내 스킬을 수정할 경우 sandbox의 /workspace/ 로 복사한 뒤 작업한다.
"""

from __future__ import annotations

import contextvars
import os
import shlex
from pathlib import Path

from langchain_core.tools import tool

from parsers import extract as _parse_document

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent.parent / "workspace"))).resolve()
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

SKILLS_ROOT = Path(os.environ.get("SKILLS_ROOT", "/skills")).resolve()
ANTHROPIC_SKILLS_DIR = SKILLS_ROOT / "anthropics-skills"

# 현재 요청의 컨텍스트 값 — executor/agent에서 set() 호출
_current_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_tenant_id", default=""
)
_current_user_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_name", default=""
)
_current_user_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_email", default=""
)
# Docker sandbox 백엔드 — agent.py에서 set() 호출
_current_sandbox: contextvars.ContextVar = contextvars.ContextVar(
    "tools_sandbox", default=None
)

_CONTAINER_WORKSPACE = Path("/workspace")
_CONTAINER_SKILLS = Path("/skills")


def _substitute_ws(s: str) -> str:
    """미치환 {ws} 플레이스홀더를 실제 workspace root로 교체한다."""
    return s.replace("{ws}", str(WORKSPACE_ROOT))


# ---------------------------------------------------------------------------
# Sandbox 실행 헬퍼 (쓰기 작업 전용)
# ---------------------------------------------------------------------------

def _require_sandbox():
    """sandbox가 없으면 RuntimeError를 발생시킨다."""
    sandbox = _current_sandbox.get()
    if sandbox is None:
        raise RuntimeError(
            "sandbox not available. 스킬 쓰기 작업은 Docker sandbox 환경에서만 가능합니다."
        )
    return sandbox


def _sandbox_exec(cmd: str) -> tuple[int, str]:
    """sandbox 컨테이너 내부에서만 실행한다. sandbox가 없으면 에러를 반환한다."""
    sandbox = _current_sandbox.get()
    if sandbox is None:
        return 1, "ERROR: sandbox not available"
    resp = sandbox.execute(cmd)
    return resp.exit_code or 0, resp.output.strip()


def _git_sb(cwd: str, *args: str) -> tuple[int, str]:
    """sandbox 컨테이너 내부에서 git 명령을 실행한다."""
    parts = " ".join(shlex.quote(a) for a in args)
    return _sandbox_exec(f"cd {shlex.quote(cwd)} && git {parts}")


# ---------------------------------------------------------------------------
# 문서 파싱 (읽기 전용 — 호스트 허용)
# ---------------------------------------------------------------------------

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
        raise ValueError(
            "tenant_id가 설정되지 않았습니다. ask_user를 호출하지 말고 작업을 중단하세요."
        )

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
            "GitHub 계정이 연결되지 않았습니다. "
            "ask_user를 호출하지 말고 작업을 중단하세요. "
            "사용자가 설정 화면에서 직접 GitHub 계정을 연결해야 합니다."
        )
    return rows[0]["github_username"], rows[0]["github_token"]


def _github_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}


def _push_skill_sandbox(
    cwd: str,
    skill_name: str,
    branch: str,
    username: str,
    token: str,
    *,
    existing_repo: bool = False,
) -> str:
    """sandbox 컨테이너 내부에서 git init/commit/push를 수행한다."""
    repo_url = f"https://{username}:{token}@github.com/{username}/{skill_name}.git"

    author_name = _current_user_name.get() or "Skill Creator Agent"
    author_email = _current_user_email.get() or "skill-creator@agent"

    rc, _ = _sandbox_exec(f"test -d {shlex.quote(cwd + '/.git')}")
    if rc != 0:
        _git_sb(cwd, "init", "-b", "main")
        _git_sb(cwd, "remote", "add", "origin", repo_url)
    else:
        _git_sb(cwd, "remote", "set-url", "origin", repo_url)

    _git_sb(cwd, "config", "user.email", author_email)
    _git_sb(cwd, "config", "user.name", author_name)

    if existing_repo:
        fetch_code, _ = _git_sb(cwd, "fetch", "origin", "main", "--depth=1")
        if fetch_code == 0:
            _git_sb(cwd, "reset", "--soft", "FETCH_HEAD")

    _git_sb(cwd, "add", ".")
    verb = "update" if branch == "main" else f"add branch {branch}"
    code, out = _git_sb(cwd, "commit", "-m", f"feat: {verb} {skill_name}")
    if code != 0 and "nothing to commit" not in out:
        return f"ERROR git commit: {out}"

    if branch != "main":
        _git_sb(cwd, "checkout", "-b", branch)

    push_args = ["push", "-u", "origin", branch]
    if not existing_repo:
        push_args.append("--force")
    code, out = _git_sb(cwd, *push_args)
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
def checkout_to_workspace(source: str) -> str:
    """Bring a skill or GitHub repository into /workspace/ for editing.

    Call this FIRST whenever you need to read, modify, build, or run code from
    a skill or any GitHub repository. Never operate on /skills/ paths directly.

    **Source resolution order:**
    1. GitHub URL (https://github.com/…)  → git clone into /workspace/<repo-name>
    2. Absolute /skills/ path             → cp -r to /workspace/<skill-name>
    3. Bare skill/repo name               → search /skills/<tenant-id>/<name> first;
                                            if not found, look up the GitHub repo by
                                            exact name and clone it.

    Returns the absolute container path of the checked-out directory, e.g.:
      /workspace/my-skill
    On failure returns an ERROR: … string.

    **Decision rule:** Make this call autonomously based on user context.
    Do NOT ask the user which path to use — resolve it yourself using the
    priority above and proceed.

    Args:
        source: GitHub URL, absolute /skills/ path, or bare skill/repo name.
    """
    import re

    if _current_sandbox.get() is None:
        return "ERROR: sandbox not available."

    _github_url_re = re.compile(
        r"https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$",
        re.IGNORECASE,
    )

    # Case 1: GitHub URL
    m = _github_url_re.match(source.strip())
    if m:
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
        try:
            username, token = _get_github_creds()
            auth_url = f"https://{username}:{token}@github.com/{owner}/{repo}.git"
        except ValueError:
            auth_url = source.strip()
        dest = f"/workspace/{repo}"
        rc, out = _sandbox_exec(
            f"rm -rf {shlex.quote(dest)} && git clone --depth=50 {shlex.quote(auth_url)} {shlex.quote(dest)}"
        )
        if rc != 0:
            return f"ERROR: git clone 실패: {out}"
        return dest

    # Case 2: absolute /skills/ path
    if source.startswith("/skills/"):
        p = Path(source)
        dest = f"/workspace/{p.name}"
        rc, out = _sandbox_exec(
            f"rm -rf {shlex.quote(dest)} && cp -r {shlex.quote(source)} {shlex.quote(dest)}"
        )
        if rc != 0:
            return f"ERROR: 복사 실패: {out}"
        return dest

    # Case 3: bare name
    name = source.strip().strip("/")
    tenant_id = _current_tenant_id.get()

    # 3a: local /skills/{tenant_id}/{name}
    if tenant_id:
        local_path = f"/skills/{tenant_id}/{name}"
        rc, _ = _sandbox_exec(f"test -d {shlex.quote(local_path)}")
        if rc == 0:
            dest = f"/workspace/{name}"
            rc2, out = _sandbox_exec(
                f"rm -rf {shlex.quote(dest)} && cp -r {shlex.quote(local_path)} {shlex.quote(dest)}"
            )
            if rc2 != 0:
                return f"ERROR: 로컬 스킬 복사 실패: {out}"
            return dest

    # 3b: GitHub repo by exact name
    try:
        username, token = _get_github_creds()
    except ValueError as e:
        return f"ERROR: 로컬 스킬을 찾을 수 없고 GitHub 계정도 없습니다: {e}"

    import httpx

    headers = _github_headers(token)
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{username}/{name}",
            headers=headers,
            timeout=10,
        )
    except httpx.RequestError as e:
        return f"ERROR (network): {e}"

    if resp.status_code != 200:
        return (
            f"ERROR: '{name}' 스킬을 /skills/ 에서도 GitHub 레포에서도 찾을 수 없습니다. "
            "정확한 스킬명 또는 GitHub URL을 확인하세요."
        )

    repo_name = resp.json()["name"]
    auth_url = f"https://{username}:{token}@github.com/{username}/{repo_name}.git"
    dest = f"/workspace/{repo_name}"
    rc, out = _sandbox_exec(
        f"rm -rf {shlex.quote(dest)} && git clone --depth=50 {shlex.quote(auth_url)} {shlex.quote(dest)}"
    )
    if rc != 0:
        return f"ERROR: git clone 실패: {out}"
    return dest


@tool
def publish_skill(skill_path: str, branch: str | None = None) -> str:
    """Publish a completed skill to its own GitHub repository.

    All operations run inside the Docker sandbox — the host filesystem is never
    modified. GitHub credentials are automatically loaded from the tenant's OAuth
    settings — never ask the user for a token or password.

    **Path rules (container paths):**
      - /workspace/<skill-name>   → publish directly
      - /skills/<tenant-id>/<skill-name> → copied to /workspace/<skill-name> first
      - /skills/anthropics-skills/...    → rejected (read-only system skills)

    **Two-phase flow:**

    Phase 1 — call without `branch`:
      - If the GitHub repo does NOT exist: creates it and pushes to main.
        Returns the published URL.
      - If the GitHub repo ALREADY exists: returns a "CONFIRM:" message with
        the repo URL. Infer the target branch from the user's prior request —
        if the user already specified a branch name, call this tool again with
        that branch immediately without asking. Only call `ask_user` when the
        branch truly cannot be inferred from context.

    Phase 2 — call with `branch`:
      - Pushes to the specified branch (use "main" to overwrite, or a new name
        such as "v2" to preserve the current version).

    Args:
        skill_path: Docker container path to the completed skill folder.
        branch:     Target branch. Omit on first call; set once branch is known.
    """
    import httpx

    if _current_sandbox.get() is None:
        return "ERROR: sandbox not available. 스킬 퍼블리시는 Docker sandbox 환경에서만 가능합니다."

    try:
        username, token = _get_github_creds()
    except ValueError as e:
        return f"ERROR: {e}"

    # 컨테이너 내부에서 경로 존재 여부 확인
    rc, _ = _sandbox_exec(f"test -d {shlex.quote(skill_path)}")
    if rc != 0:
        return f"ERROR: not a directory: {skill_path}"

    p = Path(skill_path)
    skill_name = p.name

    # anthropics-skills는 읽기 전용 시스템 스킬 — 수정/퍼블리시 불가
    if p.is_relative_to(_CONTAINER_SKILLS / "anthropics-skills"):
        return "ERROR: anthropics-skills 하위 스킬은 수정하거나 퍼블리시할 수 없습니다."

    cwd = skill_path
    if not p.is_relative_to(_CONTAINER_WORKSPACE):
        # /skills/<tenant_id>/<skill>/ 인 경우 → /workspace/<skill>/ 로 sandbox 내부 복사
        tenant_id = _current_tenant_id.get()
        if not tenant_id:
            return "ERROR: tenant_id가 설정되지 않았습니다. ask_user를 호출하지 말고 작업을 중단하세요."

        if not p.is_relative_to(_CONTAINER_SKILLS / tenant_id):
            return (
                f"ERROR: '{skill_path}' 는 테넌트({tenant_id}) 소유 스킬 경로가 아닙니다. "
                "퍼블리시 가능한 경로: /workspace/<skill-name> 또는 "
                f"/skills/{tenant_id}/<skill-name>"
            )

        dest = f"/workspace/{skill_name}"
        rc, out = _sandbox_exec(
            f"rm -rf {shlex.quote(dest)} && cp -r {shlex.quote(skill_path)} {shlex.quote(dest)}"
        )
        if rc != 0:
            return f"ERROR: sandbox 내부 복사 실패: {out}"
        cwd = dest

    # GitHub API: 레포 존재 여부 확인 (네트워크 호출 — 호스트에서 실행)
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
        return _push_skill_sandbox(cwd, skill_name, "main", username, token, existing_repo=False)
    elif check.status_code not in (200, 404):
        return f"ERROR checking repo: {check.status_code} {check.text[:200]}"

    return _push_skill_sandbox(cwd, skill_name, branch, username, token, existing_repo=repo_exists)
