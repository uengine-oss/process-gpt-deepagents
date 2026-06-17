"""core/skills/tools.py — 딥 에이전트 및 skill-creator 서브에이전트 공용 툴.

extract_document_structure, list_git_repos, publish_skill을 제공한다.

보안 원칙:
  - /skills/ 하위 파일 읽기는 호스트에서 허용한다.
  - 스킬 생성·수정·Git 배포 등 모든 쓰기 작업은 Docker sandbox 내부에서만 수행한다.
  - sandbox가 없으면 쓰기 작업은 거부된다.
  - /skills/ 내 스킬을 수정할 경우 sandbox의 /workspace/ 로 복사한 뒤 작업한다.
"""

from __future__ import annotations

import contextvars
import os
import re
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

# 임의 git 원격 URL 매칭 (http/https/ssh/git+ssh)
_GIT_URL_RE = re.compile(
    r"(?:https?://|git@|ssh://git@)([^/:]+)[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _substitute_ws(s: str) -> str:
    """미치환 {ws} 플레이스홀더를 실제 workspace root로 교체한다."""
    return s.replace("{ws}", str(WORKSPACE_ROOT))


# ---------------------------------------------------------------------------
# Sandbox 실행 헬퍼 (쓰기 작업 전용)
# ---------------------------------------------------------------------------

def _require_sandbox():
    sandbox = _current_sandbox.get()
    if sandbox is None:
        raise RuntimeError(
            "sandbox not available. 스킬 쓰기 작업은 Docker sandbox 환경에서만 가능합니다."
        )
    return sandbox


def _sandbox_exec(cmd: str) -> tuple[int, str]:
    sandbox = _current_sandbox.get()
    if sandbox is None:
        return 1, "ERROR: sandbox not available"
    resp = sandbox.execute(cmd)
    return resp.exit_code or 0, resp.output.strip()


def _git_sb(cwd: str, *args: str) -> tuple[int, str]:
    parts = " ".join(shlex.quote(a) for a in args)
    return _sandbox_exec(f"cd {shlex.quote(cwd)} && git {parts}")


# ---------------------------------------------------------------------------
# Git provider 헬퍼
# ---------------------------------------------------------------------------

def _get_git_provider():
    """현재 테넌트의 Git provider 인스턴스를 반환한다."""
    from .git_providers.factory import get_provider

    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        raise ValueError(
            "tenant_id가 설정되지 않았습니다. ask_user를 호출하지 말고 작업을 중단하세요."
        )
    return get_provider(tenant_id)


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
# Git 푸시 헬퍼 (sandbox 내부 실행)
# ---------------------------------------------------------------------------

def _push_skill_sandbox(
    cwd: str,
    skill_name: str,
    branch: str,
    clone_url: str,
    html_url: str,
    *,
    existing_repo: bool = False,
) -> str:
    """sandbox 컨테이너 내부에서 git init/commit/push를 수행한다."""
    author_name = _current_user_name.get() or "Skill Creator Agent"
    author_email = _current_user_email.get() or "skill-creator@agent"

    rc, _ = _sandbox_exec(f"test -d {shlex.quote(cwd + '/.git')}")
    if rc != 0:
        _git_sb(cwd, "init", "-b", "main")
        _git_sb(cwd, "remote", "add", "origin", clone_url)
    else:
        _git_sb(cwd, "remote", "set-url", "origin", clone_url)

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

    return f"Published '{skill_name}' → {html_url}/tree/{branch}"


# ---------------------------------------------------------------------------
# 공개 툴
# ---------------------------------------------------------------------------

@tool
def list_git_repos() -> str:
    """List all repositories owned by the configured tenant's Git account.

    Returns a JSON array of objects with name, description, html_url, updated_at,
    and default_branch. Use this to check whether a skill already exists before
    creating a new one.

    Works with any configured Git provider (GitHub, GitLab, Gitea, etc.).
    """
    import json

    try:
        provider = _get_git_provider()
    except ValueError as e:
        return f"ERROR: {e}"

    try:
        repos = provider.list_repos()
    except Exception as e:
        return f"ERROR: {e}"

    return json.dumps(repos, ensure_ascii=False, indent=2)


@tool
def checkout_to_workspace(source: str) -> str:
    """Bring a skill or Git repository into /workspace/ for editing.

    Call this FIRST whenever you need to read, modify, build, or run code from
    a skill or any Git repository. Never operate on /skills/ paths directly.

    **Source resolution order:**
    1. Git URL (https://…, git@…)  → git clone into /workspace/<repo-name>
    2. Absolute /skills/ path      → cp -r to /workspace/<skill-name>
    3. Bare skill/repo name        → search /skills/<tenant-id>/<name> first;
                                     if not found, look up the Git repo by
                                     exact name via the configured provider and clone it.

    Returns the absolute container path of the checked-out directory, e.g.:
      /workspace/my-skill
    On failure returns an ERROR: … string.

    **Decision rule:** Make this call autonomously based on user context.
    Do NOT ask the user which path to use — resolve it yourself using the
    priority above and proceed.

    Args:
        source: Git URL, absolute /skills/ path, or bare skill/repo name.
    """
    if _current_sandbox.get() is None:
        return "ERROR: sandbox not available."

    # Case 1: Git URL
    m = _GIT_URL_RE.search(source.strip())
    if m:
        repo = m.group(3).removesuffix(".git")
        owner = m.group(2)
        try:
            provider = _get_git_provider()
            auth_url = provider.get_authenticated_clone_url(owner, repo)
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

    # 3b: configured Git provider에서 레포 검색
    try:
        provider = _get_git_provider()
    except ValueError as e:
        return f"ERROR: 로컬 스킬을 찾을 수 없고 Git 계정도 없습니다: {e}"

    repo_info = provider.repo_exists(name)
    if not repo_info:
        return (
            f"ERROR: '{name}' 스킬을 /skills/ 에서도 Git 레포에서도 찾을 수 없습니다. "
            "정확한 스킬명 또는 Git URL을 확인하세요."
        )

    auth_url = provider.get_authenticated_clone_url(provider.username, name)
    dest = f"/workspace/{name}"
    rc, out = _sandbox_exec(
        f"rm -rf {shlex.quote(dest)} && git clone --depth=50 {shlex.quote(auth_url)} {shlex.quote(dest)}"
    )
    if rc != 0:
        return f"ERROR: git clone 실패: {out}"
    return dest


@tool
def publish_skill(skill_path: str, branch: str | None = None) -> str:
    """Publish a completed skill to a Git repository via the configured provider.

    All operations run inside the Docker sandbox — the host filesystem is never
    modified. Git credentials are automatically loaded from the tenant's settings —
    never ask the user for a token or password.

    Works with any configured Git provider (GitHub, GitLab, Gitea, etc.).

    **Path rules (container paths):**
      - /workspace/<skill-name>   → publish directly
      - /skills/<tenant-id>/<skill-name> → copied to /workspace/<skill-name> first
      - /skills/anthropics-skills/...    → rejected (read-only system skills)

    **Two-phase flow:**

    Phase 1 — call without `branch`:
      - If the repo does NOT exist: creates it and pushes to main.
        Returns the published URL.
      - If the repo ALREADY exists: returns a "CONFIRM:" message with
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
    if _current_sandbox.get() is None:
        return "ERROR: sandbox not available. 스킬 퍼블리시는 Docker sandbox 환경에서만 가능합니다."

    try:
        provider = _get_git_provider()
    except ValueError as e:
        return f"ERROR: {e}"

    # 컨테이너 내부에서 경로 존재 여부 확인
    rc, _ = _sandbox_exec(f"test -d {shlex.quote(skill_path)}")
    if rc != 0:
        return f"ERROR: not a directory: {skill_path}"

    p = Path(skill_path)
    skill_name = p.name

    if p.is_relative_to(_CONTAINER_SKILLS / "anthropics-skills"):
        return "ERROR: anthropics-skills 하위 스킬은 수정하거나 퍼블리시할 수 없습니다."

    cwd = skill_path
    if not p.is_relative_to(_CONTAINER_WORKSPACE):
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

    repo_info = provider.repo_exists(skill_name)
    repo_exists = repo_info is not None

    if branch is None:
        if repo_exists:
            html_url = provider.get_repo_html_url(provider.username, skill_name)
            return (
                f"CONFIRM: '{skill_name}' 레포가 이미 존재합니다 ({html_url}). "
                "ask_user를 호출해 main에 커밋으로 업데이트할지, "
                "새 브랜치명을 쓸지 확인한 뒤 "
                "branch 값을 지정해서 publish_skill을 다시 호출하세요."
            )
        try:
            html_url = provider.create_repo(skill_name)
        except RuntimeError as e:
            return f"ERROR creating repo: {e}"

        clone_url = provider.get_authenticated_clone_url(provider.username, skill_name)
        return _push_skill_sandbox(
            cwd, skill_name, "main", clone_url, html_url, existing_repo=False
        )

    clone_url = provider.get_authenticated_clone_url(provider.username, skill_name)
    html_url = provider.get_repo_html_url(provider.username, skill_name)
    return _push_skill_sandbox(
        cwd, skill_name, branch, clone_url, html_url, existing_repo=repo_exists
    )


# ---------------------------------------------------------------------------
# Skill name extraction from tool arguments
# ---------------------------------------------------------------------------

def _skills_from_tool_args(tool_args: dict) -> list[str]:
    """Return skill names whose directory paths appear in any string value of tool_args."""
    from core.skills.skills import _get_skills

    bases = _get_skills() or []
    if not bases:
        skills_root = os.getenv("SKILLS_DIRS", str(Path(__file__).parent.parent.parent / "skills"))
        bases = [str(Path(skills_root).resolve())]

    # Agents run inside Docker and use container paths like /skills/{collection}/{skill-name}.
    # Add the container-side equivalents so we match both host and container paths.
    container_bases = [f"/skills/{Path(p).name}" for p in bases if Path(p).name]
    all_bases = list(dict.fromkeys([*bases, *container_bases]))

    found: list[str] = []

    def _scan(v: object) -> None:
        if isinstance(v, dict):
            for item in v.values():
                _scan(item)
        elif isinstance(v, list):
            for item in v:
                _scan(item)
        elif isinstance(v, str):
            for base in all_bases:
                base = base.rstrip("/")
                idx = v.find(base)
                if idx == -1:
                    continue
                rest = v[idx + len(base):]
                parts = [p for p in rest.split("/") if p]
                if parts:
                    found.append(parts[0])

    _scan(tool_args)
    return list(dict.fromkeys(found))
