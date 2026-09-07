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
# 현재 요청의 대화방 id — executor에서 set(). 산출물을 방별 디렉터리로 격리(경로 충돌 방지)한다.
_current_conversation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_conversation_id", default=""
)
# 사용자의 이번 요청 텍스트(프로세스명 도출용). executor 가 턴 시작 시 설정한다.
_current_user_request: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_request", default=""
)
# 현재 요청 사용자 UUID — PR requester_id 로 사용. executor/agent 에서 set() 호출.
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tools_user_id", default=""
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


def _conv_slug(conv: str) -> str:
    """대화방 id를 디렉터리명으로 안전하게 슬러그화(full-uuid 그대로 사용).

    uuid 는 그 자체로 고유하므로 축약/레지스트리 없이 전체를 폴더명으로 쓴다
    (경로 단순·예측 가능). 에이전트가 긴 uuid 를 옮겨 적다 한두 글자 오타를 내면
    complete 의 `_reconcile_scattered_room_dirs`(편집거리 병합)가 보정한다."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(conv or "")).strip("_")


def _to_snake(s: str) -> str:
    """camelCase/공백/특수문자 → 영문 소문자+언더스코어 id. (요소 id·폼 파일명 매칭 일관용)"""
    s = str(s or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if s and not s[0].isalpha():
        s = "n_" + s
    return s or "node"


def _substitute_ws(s: str) -> str:
    """미치환 {ws} 플레이스홀더를 실제 workspace root로 교체한다."""
    return s.replace("{ws}", str(WORKSPACE_ROOT))


# 샌드박스 작업 루트(컨테이너 /workspace/.bpmn ↔ 호스트 WORKSPACE_ROOT/.bpmn — bind mount 동일 파일).
BPMN_WORK_SUBDIR = ".bpmn"


def _resolve_ws_path(path: str) -> Path:
    """컨테이너/플레이스홀더 경로(/workspace/..., {ws}/..., 상대경로)를 호스트 WORKSPACE_ROOT 기준 경로로 변환."""
    s = _substitute_ws(str(path or "")).strip().replace("\\", "/")
    ws = str(WORKSPACE_ROOT).replace("\\", "/")
    if s.startswith(ws):
        return Path(s)
    for pre in ("/workspace/", "workspace/", "/workspace", "workspace"):
        if s == pre.rstrip("/") or s.startswith(pre):
            s = s[len(pre):]
            break
    return WORKSPACE_ROOT / s.lstrip("/")


def _bpmn_work_dir() -> Path:
    """방별 산출물 작업 디렉터리(host). 대화방 id가 있으면 .bpmn/<room>/ 로 격리한다."""
    base = WORKSPACE_ROOT / BPMN_WORK_SUBDIR
    slug = _conv_slug(_current_conversation_id.get())
    return base / slug if slug else base


def bpmn_container_base(conversation_id: str = "") -> str:
    """에이전트에게 알려줄 컨테이너 기준 경로(/workspace/.bpmn 또는 .bpmn/<room>)."""
    slug = _conv_slug(conversation_id or _current_conversation_id.get())
    return f"/workspace/{BPMN_WORK_SUBDIR}/{slug}" if slug else f"/workspace/{BPMN_WORK_SUBDIR}"


def canonical_process_dir_name(conversation_id: str = "") -> str:
    """Return the deterministic process directory injected into the agent prompt.

    Keeping this calculation in one place prevents HITL resumes and completion
    scans from disagreeing about which ``process-*`` directory belongs to the
    current generation attempt.
    """
    import uuid as _uuid

    seed = str(conversation_id or _current_conversation_id.get() or _current_tenant_id.get() or "processgpt")
    return f"process-{_uuid.uuid5(_uuid.NAMESPACE_URL, seed).hex[:7]}"


def process_work_dirs(room: Path, conversation_id: str = "") -> list[Path]:
    """Return only the current attempt's process directories.

    Older versions generated a random directory after every HITL resume. Those
    stale folders must not be completed or re-emitted with the current process.
    Multi-process output is represented by the canonical directory followed by
    ``-2``, ``-3`` and so on.
    """
    primary = canonical_process_dir_name(conversation_id)
    candidates: list[tuple[int, Path]] = []
    pattern = re.compile(rf"^{re.escape(primary)}(?:-(\d+))?$")
    if room.is_dir():
        for child in room.iterdir():
            if not child.is_dir():
                continue
            match = pattern.fullmatch(child.name)
            if match:
                candidates.append((int(match.group(1) or "1"), child))
    return [path for _, path in sorted(candidates, key=lambda item: item[0])]


def write_upload_to_workspace(file_name: str, content: bytes) -> str:
    """업로드 문서 바이트를 호스트 workspace(/workspace ↔ bind mount)에 써서 컨테이너 경로를 돌려준다.

    executor 가 실행 전 Supabase Storage 에서 받은 파일을 여기에 넣으면, 에이전트가
    `read_file('/workspace/uploads/<name>')` 로 바로 읽을 수 있다.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(file_name or "")).strip("_") or "upload"
    d = WORKSPACE_ROOT / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    (d / safe).write_bytes(content)
    return f"/workspace/uploads/{safe}"


_SKILL_NAME = "bpmn-process-generation-skill"


def _skill_scripts_dir() -> Path | None:
    """bpmn-process-generation-skill 의 scripts 디렉토리(호스트)를 찾는다 (검증/flatten 재사용).

    번들 원본(`system-skills/process-gpt-system/`)을 **항상 먼저** 본다. 예전에는
    `skills/**` 를 glob 해 사전순 첫 히트를 썼는데, 개발 환경에 남은 테넌트 사본
    (`skills/<tenant>/bpmn-process-generation-skill/`)이 'p'(process-gpt-system)보다
    앞서 잡혀 **오래된 검증 스크립트가 로드**되는 일이 있었다. 원본을 고정 우선으로
    두면 어떤 환경에서도 같은 검증기를 쓴다.
    """
    from .local_system_skills import SYSTEM_COLLECTION_NAME, _resolve_skills_root

    base = Path(__file__).parent.parent.parent
    candidates = [
        base / "system-skills" / SYSTEM_COLLECTION_NAME / _SKILL_NAME / "scripts",
    ]
    try:
        candidates.append(_resolve_skills_root() / SYSTEM_COLLECTION_NAME / _SKILL_NAME / "scripts")
    except Exception:
        pass
    for c in candidates:
        if c.is_dir():
            return c
    # 최후 폴백: 어디든 남아있는 사본(사전순 고정).
    for c in sorted((base / "skills").glob(f"**/{_SKILL_NAME}/scripts")):
        if c.is_dir():
            return c
    return None


def _ensure_skill_scripts_on_path() -> Path | None:
    import sys as _sys
    d = _skill_scripts_dir()
    if d and str(d) not in _sys.path:
        _sys.path.insert(0, str(d))
    return d


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

def _auto_register_tenant_skill(cwd: str, skill_name: str, html_url: str) -> str:
    """신규 레포 push 후 skills/ 디렉토리에 파일을 sync하고 tenant_skills DB에 등록한다."""
    import shutil as _shutil
    import logging as _logging

    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        return (
            f"Published '{skill_name}' → {html_url}\n"
            "NOTE: tenant_id가 없어 테넌트 스킬 자동 등록을 건너뜁니다."
        )

    host_src = _resolve_ws_path(cwd)
    if not host_src.is_dir():
        return (
            f"Published '{skill_name}' → {html_url}\n"
            f"WARNING: 로컬 파일 경로를 찾을 수 없어 자동 등록을 건너뜁니다: {host_src}"
        )

    try:
        provider = _get_git_provider()
        config_id = getattr(provider, "config_id", "") or "local"
    except Exception:
        config_id = "local"

    _BASE_DIR = Path(__file__).parent.parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    first = raw.split(",")[0].strip() or str(_BASE_DIR / "skills")
    skills_root = Path(first) if os.path.isabs(first) else (_BASE_DIR / first).resolve()
    skill_dir = skills_root / tenant_id / config_id / skill_name

    try:
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        if skill_dir.exists():
            _shutil.rmtree(skill_dir)
        _shutil.copytree(str(host_src), str(skill_dir))
    except Exception as e:
        return (
            f"Published '{skill_name}' → {html_url}\n"
            f"WARNING: 파일 sync 실패로 자동 등록을 건너뜁니다: {e}"
        )

    owner_id = _current_user_id.get() or ""
    try:
        from ..storage.db import get_supabase
        get_supabase().table("tenant_skills").insert({
            "tenant_id": tenant_id,
            "skill_name": skill_name,
            "owner_id": owner_id,
        }).execute()
    except Exception as e:
        _logging.getLogger(__name__).warning("tenant_skills 등록 실패 skill=%s: %s", skill_name, e)
        _shutil.rmtree(str(skill_dir), ignore_errors=True)
        return (
            f"Published '{skill_name}' → {html_url}\n"
            f"WARNING: DB 등록 실패로 파일 sync를 롤백했습니다: {e}"
        )

    return f"Published '{skill_name}' → {html_url}\n✓ 테넌트 스킬로 등록 완료"


def _push_skill_sandbox(
    cwd: str,
    skill_name: str,
    branch: str,
    clone_url: str,
    html_url: str,
    *,
    existing_repo: bool = False,
) -> str:
    """sandbox 컨테이너 내부에서 git init/commit/push를 수행한다.

    신규 레포(existing_repo=False)일 때만 main 직접 푸시를 허용하고,
    기존 레포를 수정할 때는 반드시 피처 브랜치에 푸시 후 PR을 생성한다.
    """
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

    # 기존 레포 수정 시 기본 브랜치 직접 푸시 금지 → 피처 브랜치 + PR 워크플로우
    if existing_repo and branch == "main":
        import time as _time
        branch = f"skill/{_safe_name(skill_name)}/{int(_time.time())}"

    if branch != "main":
        _git_sb(cwd, "checkout", "-b", branch)

    push_args = ["push", "-u", "origin", branch]
    if not existing_repo:
        push_args.append("--force")
    code, out = _git_sb(cwd, *push_args)
    if code != 0:
        return f"ERROR git push: {out}"

    # 기존 레포: PR 생성 및 DB 기록
    if existing_repo:
        return _create_pr_after_push(
            skill_name=skill_name,
            branch=branch,
            base_branch="main",
            html_url=html_url,
            title=f"feat: update {skill_name}",
            description=f"스킬 '{skill_name}' 업데이트",
        )

    # 신규 레포: 파일 sync + tenant_skills DB 삽입 자동 수행
    return _auto_register_tenant_skill(cwd, skill_name, html_url)


def _create_pr_after_push(
    *,
    skill_name: str,
    branch: str,
    base_branch: str,
    html_url: str,
    title: str,
    description: str,
) -> str:
    """피처 브랜치 푸시 후 Git provider에 PR을 생성하고 resource_pull_requests에 기록한다."""
    import json as _json

    provider = _get_git_provider()
    owner = provider.username

    # Git provider에 PR 생성
    try:
        pr_result = provider.create_pull_request(
            owner=owner,
            repo=skill_name,
            title=title,
            body=description,
            head=branch,
            base=base_branch,
        )
    except Exception as e:
        return (
            f"Pushed '{skill_name}' → {html_url}/tree/{branch}\n"
            f"WARNING: PR 생성 실패: {e}\n"
            f"수동으로 PR을 생성해 주세요."
        )

    pr_number = pr_result.get("number")
    pr_url = pr_result.get("html_url", "")

    # resource_pull_requests 테이블에 기록
    _record_resource_pr(
        resource_type="skill",
        resource_id=skill_name,
        branch_name=branch,
        base_branch=base_branch,
        title=title,
        description=description,
        git_pr_number=pr_number,
        git_pr_url=pr_url,
        git_repo_url=html_url,
    )

    return (
        f"PR created for '{skill_name}' → {pr_url}\n"
        f"Branch: {branch} → {base_branch}\n"
        f"PR #{pr_number}: {title}\n\n"
        f"기본 브랜치에 직접 커밋하지 않고 PR을 생성했습니다. "
        f"리뷰 후 병합해 주세요."
    )


def _as_list(value: str | list[str] | None) -> list[str]:
    """단일 값 또는 리스트를 배열 컬럼용 리스트로 정규화한다(빈 값 제거)."""
    items = [value] if isinstance(value, str) else list(value or [])
    return [i for i in items if i]


def _normalize_requester_ids(requester_id: str | list[str] | None) -> list[str]:
    """requester_id 를 uuid[] 컬럼용 리스트로 정규화한다. 값이 없으면 nil UUID 한 건."""
    import uuid as _uuid

    return _as_list(requester_id) or [str(_uuid.UUID(int=0))]


def _record_resource_pr(
    *,
    resource_type: str,
    resource_id: str,
    branch_name: str,
    base_branch: str,
    title: str,
    description: str,
    git_pr_number: int | None = None,
    git_pr_url: str = "",
    git_repo_url: str = "",
) -> dict | None:
    """resource_pull_requests 테이블에 PR 레코드를 삽입한다."""
    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        return None

    requester_ids = _normalize_requester_ids(_current_user_id.get())
    requester_names = _as_list(_current_user_name.get()) or None

    try:
        from ..storage.db import get_supabase
        sb = get_supabase()
        row = {
            "tenant_id": tenant_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "branch_name": branch_name,
            "base_branch": base_branch,
            "title": title,
            "description": description,
            "status": "OPEN",
            "requester_id": requester_ids,
            "requester_name": requester_names,
            "git_pr_number": git_pr_number,
            "git_pr_url": git_pr_url,
            "git_repo_url": git_repo_url,
        }
        resp = sb.table("resource_pull_requests").insert(row).execute()
        data = getattr(resp, "data", None)
        return data[0] if data else None
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "resource_pull_requests 기록 실패: resource=%s/%s branch=%s",
            resource_type, resource_id, branch_name,
            exc_info=True,
        )
        return None


def _read_process_definition_file() -> dict | None:
    """샌드박스 작업 파일 `.bpmn/process-definition.json`(elements[] 형식)을 호스트에서 읽는다."""
    import json as _json
    f = _bpmn_work_dir() / "process-definition.json"
    if not f.is_file():
        return None
    try:
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── ProcessGPT 프로세스 정의 스키마 적합성 검사 ──────────────────────────────
# pdf2bpmn(process_definition_prompt.py) 규격을 강제한다. 흐름(static_check) 이전에 스키마부터 본다.
# 모델이 일반 Camunda BPMN(UserTask/assignee/formKey 등)으로 생성하는 것을 차단하는 게이트.
_ALLOWED_ACTIVITY_TYPES = {"UserActivity"}
_ALLOWED_EVENT_TYPES = {"StartEvent", "EndEvent", "IntermediateCatchEvent", "IntermediateThrowEvent"}
_ALLOWED_GATEWAY_TYPES = {"ExclusiveGateway", "ParallelGateway", "InclusiveGateway"}
# 일반 BPMN/Camunda 잔재 키 — 사용 금지(우리 규격엔 role/inputData/outputData/tool 등을 쓴다)
_FORBIDDEN_ELEMENT_KEYS = {"assignee", "formKey", "candidateGroups", "candidateUsers", "implementation"}
_PROC_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# 일반 BPMN(Camunda/Zeebe) type → ProcessGPT (elementType, type) 매핑(소문자 키).
_BPMN_TYPE_MAP = {
    "startevent": ("Event", "StartEvent"),
    "endevent": ("Event", "EndEvent"),
    "intermediatecatchevent": ("Event", "IntermediateCatchEvent"),
    "intermediatethrowevent": ("Event", "IntermediateThrowEvent"),
    "usertask": ("Activity", "UserActivity"),
    "task": ("Activity", "UserActivity"),
    "useractivity": ("Activity", "UserActivity"),
    "manualtask": ("Activity", "UserActivity"),
    "servicetask": ("Activity", "UserActivity"),
    "scripttask": ("Activity", "UserActivity"),
    "businessruletask": ("Activity", "UserActivity"),
    "approvertask": ("Activity", "UserActivity"),
    "exclusivegateway": ("Gateway", "ExclusiveGateway"),
    "parallelgateway": ("Gateway", "ParallelGateway"),
    "inclusivegateway": ("Gateway", "InclusiveGateway"),
    "sequenceflow": ("Sequence", "Sequence"),
}


def _normalize_to_processgpt_schema(pd: dict) -> tuple:
    """모델이 일반 BPMN(Camunda)식으로 만든 정의를 ProcessGPT 규격(elements[]+elementType)으로
    **결정적으로** 변환한다(LLM 준수에 의존하지 않는 안전망). (정규화된 dict, 변경여부) 반환."""
    import uuid as _uuid

    if not isinstance(pd, dict):
        return pd, False
    if isinstance(pd.get("processDefinition"), dict):
        pd = pd["processDefinition"]
    changed = False

    # 최상위 필드 보강
    if not pd.get("processDefinitionName"):
        pd["processDefinitionName"] = (pd.get("name") or "생성된 프로세스")
        changed = True
    if not pd.get("processDefinitionId"):
        pd["processDefinitionId"] = str(_uuid.uuid4())
        changed = True

    # 별도 flows/sequenceFlows 배열(모델이 시퀀스를 elements 밖에 두는 경우가 잦다) →
    # elements 의 Sequence 요소로 흡수한다(없으면 최종본에 시퀀스가 빠져 끊긴 프로세스가 된다).
    _flows = pd.get("flows") or pd.get("sequenceFlows")
    if isinstance(_flows, list) and _flows:
        _base_els = pd.get("elements")
        _base_els = list(_base_els) if isinstance(_base_els, list) else []
        _has_seq = any(isinstance(x, dict) and x.get("elementType") == "Sequence" for x in _base_els)
        if not _has_seq:
            for _i, _f in enumerate(_flows):
                if not isinstance(_f, dict):
                    continue
                _src = _f.get("source") or _f.get("sourceRef") or ""
                _tgt = _f.get("target") or _f.get("targetRef") or ""
                if not (_src and _tgt):
                    continue
                _seq = {
                    "id": _f.get("id") or f"seq_{_i}",
                    "elementType": "Sequence",
                    "type": "SequenceFlow",
                    "source": _src,
                    "target": _tgt,
                }
                if _f.get("conditionData") or _f.get("condition"):
                    _seq["conditionData"] = _f.get("conditionData") or _f.get("condition")
                _base_els.append(_seq)
            pd["elements"] = _base_els
            pd.pop("flows", None)
            pd.pop("sequenceFlows", None)
            changed = True

    els = pd.get("elements")
    # flattened(activities/events/...) 형식이면 elements[] 로 합친다.
    if not isinstance(els, list) or not els:
        merged = []
        for key, et, default_type in (
            ("events", "Event", "StartEvent"),
            ("activities", "Activity", "UserActivity"),
            ("gateways", "Gateway", "ExclusiveGateway"),
            ("sequences", "Sequence", "Sequence"),
        ):
            arr = pd.get(key)
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, dict):
                        x = dict(it)
                        x.setdefault("elementType", et)
                        merged.append(x)
        if merged:
            els = merged
            changed = True
        else:
            els = els if isinstance(els, list) else []

    new_els = []
    for e in els:
        if not isinstance(e, dict):
            continue
        et = e.get("elementType")
        t = e.get("type")
        if et in ("Event", "Activity", "Gateway", "Sequence"):
            e2 = dict(e)
        else:
            mapped = _BPMN_TYPE_MAP.get(str(t or "").strip().lower())
            if not mapped:
                new_els.append(e)  # 추론 불가 → 그대로(스키마 검사가 잡음)
                continue
            net, ntype = mapped
            e2 = dict(e)
            e2["elementType"] = net
            if net != "Sequence":
                e2["type"] = ntype
            changed = True
            et = net
        # flattened 저장 형식은 event/gateway type 의 첫 글자가 소문자다.
        # elementType 이 이미 보강된 상태에서도 canonical ProcessGPT type 으로 복원한다.
        if e2.get("elementType") == "Event":
            canonical = {
                "startevent": "StartEvent",
                "endevent": "EndEvent",
                "intermediatecatchevent": "IntermediateCatchEvent",
                "intermediatethrowevent": "IntermediateThrowEvent",
            }.get(str(e2.get("type") or "").lower())
            if canonical and e2.get("type") != canonical:
                e2["type"] = canonical
                changed = True
        elif e2.get("elementType") == "Gateway":
            canonical = {
                "exclusivegateway": "ExclusiveGateway",
                "parallelgateway": "ParallelGateway",
                "inclusivegateway": "InclusiveGateway",
            }.get(str(e2.get("type") or "").lower())
            if canonical and e2.get("type") != canonical:
                e2["type"] = canonical
                changed = True
        # Sequence: sourceRef/targetRef → source/target
        if e2.get("elementType") == "Sequence":
            src = e2.get("source") or e2.get("sourceRef") or ""
            tgt = e2.get("target") or e2.get("targetRef") or ""
            if e2.get("sourceRef") or e2.get("targetRef"):
                changed = True
            e2["source"] = src
            e2["target"] = tgt
            for k in ("sourceRef", "targetRef", "type"):
                e2.pop(k, None)
        else:
            # assignee → role, 금지 키 제거
            if not e2.get("role") and e2.get("assignee"):
                e2["role"] = str(e2["assignee"])
                changed = True
            for k in _FORBIDDEN_ELEMENT_KEYS:
                if k in e2:
                    e2.pop(k, None)
                    changed = True
            if e2.get("elementType") == "Activity":
                role = e2.get("role")
                if isinstance(role, list):
                    e2["role"] = "/".join(str(item).strip() for item in role if str(item).strip())
                    changed = True
                elif role is not None and not isinstance(role, str):
                    e2["role"] = str(role)
                    changed = True
                _eid = str(e2.get("id") or "").lower()
                # DMN 을 노드/활동으로 잘못 만든 경우(예: id 'dmn1_decision') → 분기 게이트웨이로 교정.
                # DMN 은 노드가 아니라 게이트웨이 속성이므로, 흐름은 유지하되 타입만 Gateway 로 바꿔
                # (폼 미생성·'도달 불가 DMN 노드' 검증오류 제거) 정상 분기점이 되게 한다.
                if _eid.startswith("dmn") or "_dmn" in _eid or _eid.endswith("_decision"):
                    e2["elementType"] = "Gateway"
                    e2["type"] = "ExclusiveGateway"
                    for k in ("role", "assignee", "outputData", "inputData", "tool", "skills", "agent"):
                        e2.pop(k, None)
                    changed = True
                else:
                    od = e2.get("outputData")
                    if not (isinstance(od, list) and len(od) > 0):
                        e2["outputData"] = [f"{e2.get('name') or e2.get('id') or '작업'} 결과"]
                        changed = True
                    if e2.get("type") not in _ALLOWED_ACTIVITY_TYPES:
                        e2["type"] = "UserActivity"
                        changed = True
        new_els.append(e2)

    # id 를 영문 소문자+언더스코어로 일관 변환(요소 id ↔ Sequence source/target ↔ element.source 동시 remap).
    id_map: dict = {}
    used: set = set()
    for e in new_els:
        if e.get("elementType") == "Sequence":
            continue
        old = str(e.get("id") or "")
        if not old:
            continue
        if _PROC_ID_RE.match(old):
            used.add(old)
            continue
        snake = _to_snake(old)
        base = snake
        n = 2
        while snake in used or snake in id_map.values():
            snake = f"{base}_{n}"
            n += 1
        id_map[old] = snake
        used.add(snake)
    if id_map:
        changed = True
        for e in new_els:
            if not isinstance(e, dict):
                continue
            # id_map 키는 str(위 빌드 루프의 old=str(...))이므로, 조회 값도 str 로 coerce 한다.
            # (모델이 id/source/target 에 dict 등 비문자열을 넣으면 `x in id_map` 이 unhashable 로 크래시)
            if e.get("elementType") == "Sequence":
                src = str(e.get("source") or "")
                if src in id_map:
                    e["source"] = id_map[src]
                tgt = str(e.get("target") or "")
                if tgt in id_map:
                    e["target"] = id_map[tgt]
            else:
                eid = str(e.get("id") or "")
                if eid in id_map:
                    e["id"] = id_map[eid]
                src = str(e.get("source") or "")
                if src in id_map:
                    e["source"] = id_map[src]

    pd["elements"] = new_els
    # flattened 잔재 제거(중복 방지)
    for key in ("activities", "events", "gateways", "sequences"):
        if key in pd and pd.get("elements"):
            pd.pop(key, None)
            changed = True
    return pd, changed


def _schema_conformance_defects(pd: dict) -> list:
    """elements[] 형식 정의가 ProcessGPT 규격을 따르는지 검사해 critical defect 목록을 반환."""
    d: list = []
    if not isinstance(pd, dict):
        return [{"severity": "critical", "type": "schema", "detail": "정의가 JSON 객체가 아닙니다."}]
    if not (pd.get("processDefinitionName") or pd.get("processDefinitionId")):
        d.append({"severity": "critical", "type": "schema_top",
                  "detail": "최상위에 processDefinitionName 이 필요합니다."})
    els = pd.get("elements")
    if not isinstance(els, list) or not els:
        if any(isinstance(pd.get(k), list) and pd.get(k) for k in ("activities", "events", "gateways", "sequences")):
            d.append({"severity": "critical", "type": "schema_format",
                      "detail": "반드시 elements[] 형식이어야 합니다(activities/events/... 분리 배열 금지). "
                                "Event/Activity/Gateway/Sequence 를 elementType 으로 구분해 elements 한 배열에 담으세요."})
        else:
            d.append({"severity": "critical", "type": "schema_empty",
                      "detail": "elements[] 가 비었거나 없습니다. 실제 요소를 생성하세요."})
        return d
    n_start = n_end = 0
    for i, e in enumerate(els):
        if not isinstance(e, dict):
            d.append({"severity": "critical", "type": "schema_el", "detail": f"elements[{i}] 가 객체가 아닙니다."})
            continue
        eid = str(e.get("id") or "")
        et = e.get("elementType")
        t = e.get("type")
        bad_keys = sorted(k for k in _FORBIDDEN_ELEMENT_KEYS if k in e)
        if bad_keys:
            d.append({"severity": "critical", "type": "schema_forbidden_key",
                      "detail": f"'{eid or i}': 일반 BPMN 키 {bad_keys} 사용 금지 — role/inputData/outputData/tool 등 ProcessGPT 필드를 쓰세요."})
        if et not in ("Event", "Activity", "Gateway", "Sequence"):
            d.append({"severity": "critical", "type": "schema_elementType",
                      "detail": f"elements[{i}] elementType 누락/오류({et!r}). Event/Activity/Gateway/Sequence 중 하나여야 합니다."})
            continue
        if et != "Sequence" and eid and not _PROC_ID_RE.match(eid):
            d.append({"severity": "critical", "type": "schema_id",
                      "detail": f"id '{eid}' 는 영문 소문자+언더스코어만 허용(camelCase/대문자 금지)."})
        if et == "Activity":
            if t not in _ALLOWED_ACTIVITY_TYPES:
                d.append({"severity": "critical", "type": "schema_activity_type",
                          "detail": f"'{eid}': Activity.type 은 'UserActivity' 여야 합니다(받음: {t!r}). UserTask/ServiceTask 등 금지."})
            if not e.get("role"):
                d.append({"severity": "critical", "type": "schema_role", "detail": f"'{eid}': Activity 에 role 이 필요합니다."})
            elif not isinstance(e.get("role"), str):
                d.append({"severity": "critical", "type": "schema_role_type", "detail": f"'{eid}': Activity role 은 문자열이어야 합니다."})
            od = e.get("outputData")
            if not (isinstance(od, list) and len(od) > 0):
                d.append({"severity": "critical", "type": "schema_outputData",
                          "detail": f"'{eid}': outputData 가 최소 1개 필요합니다(빈 배열/누락 금지)."})
            elif any(not isinstance(value, str) for value in od):
                d.append({"severity": "critical", "type": "schema_outputData_type",
                          "detail": f"'{eid}': outputData 항목은 모두 문자열이어야 합니다."})
            idata = e.get("inputData")
            if idata is not None and (
                not isinstance(idata, list) or any(not isinstance(value, str) for value in idata)
            ):
                d.append({"severity": "critical", "type": "schema_inputData_type",
                          "detail": f"'{eid}': inputData 는 문자열 배열이어야 합니다."})
        elif et == "Event":
            if t not in _ALLOWED_EVENT_TYPES:
                d.append({"severity": "critical", "type": "schema_event_type",
                          "detail": f"'{eid}': Event.type 오류({t!r}). StartEvent/EndEvent/IntermediateCatchEvent 중 하나."})
            if t == "StartEvent":
                n_start += 1
            elif t == "EndEvent":
                n_end += 1
        elif et == "Gateway":
            if t not in _ALLOWED_GATEWAY_TYPES:
                d.append({"severity": "critical", "type": "schema_gateway_type",
                          "detail": f"'{eid}': Gateway.type 오류({t!r}). ExclusiveGateway/ParallelGateway/InclusiveGateway 중 하나."})
            cdata = e.get("conditionData")
            if cdata is not None and (
                not isinstance(cdata, list) or any(not isinstance(value, str) for value in cdata)
            ):
                d.append({"severity": "critical", "type": "schema_conditionData_type",
                          "detail": f"'{eid}': conditionData 는 '<form_id>.<field_name>' 문자열 배열이어야 합니다."})
        elif et == "Sequence":
            if not e.get("source") or not e.get("target"):
                d.append({"severity": "critical", "type": "schema_sequence",
                          "detail": f"Sequence '{eid}' 는 source 와 target 이 모두 필요합니다."})
            if e.get("condition") is not None and not isinstance(e.get("condition"), str):
                d.append({"severity": "critical", "type": "schema_sequence_condition_type",
                          "detail": f"Sequence '{eid}' condition 은 문자열이어야 합니다."})
    if n_start < 1:
        d.append({"severity": "critical", "type": "schema_start", "detail": "StartEvent(elementType:Event, type:StartEvent) 1개가 필요합니다."})
    if n_end < 1:
        d.append({"severity": "critical", "type": "schema_end", "detail": "EndEvent(elementType:Event, type:EndEvent) 1개가 필요합니다."})
    return d


_SCHEMA_BLOCKING_TYPES = {"schema", "schema_empty", "schema_format", "schema_elementType", "schema_el"}


def _atomic_write_json(target: Path, payload: dict) -> None:
    """Write one complete JSON document without exposing a partial file."""
    import json as _json
    import uuid as _uuid

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{_uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Parse the exact bytes before replacing the last known-good definition.
        _json.loads(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


@tool
def write_process_definition(
    definition: dict,
    path: str = ".bpmn/process-definition.json",
) -> str:
    """Atomically replace a ProcessGPT process definition with one JSON object.

    Use this tool for every creation or update of ``process-definition.json``.
    It rejects empty or structurally invalid definitions before the previous
    valid file is replaced. Never use generic ``edit_file`` for that file.
    """
    import hashlib as _hashlib
    import json as _json

    if path in (".bpmn/process-definition.json", "process-definition.json"):
        target = _bpmn_work_dir() / "process-definition.json"
    else:
        target = _resolve_ws_path(path)

    payload = definition
    if isinstance(payload, dict) and isinstance(payload.get("processDefinition"), dict):
        payload = payload["processDefinition"]
    if not isinstance(payload, dict):
        return _json.dumps({
            "written": False,
            "error": "definition must be one JSON object",
        }, ensure_ascii=False)

    normalized, _ = _normalize_to_processgpt_schema(payload)
    blocking = [
        item for item in _schema_conformance_defects(normalized)
        if str(item.get("type")) in _SCHEMA_BLOCKING_TYPES
    ]
    if blocking:
        return _json.dumps({
            "written": False,
            "error": "schema_blocked",
            "defects": blocking,
        }, ensure_ascii=False)

    _atomic_write_json(target, normalized)
    content = target.read_bytes()
    return _json.dumps({
        "written": True,
        "path": str(target),
        "sha256": _hashlib.sha256(content).hexdigest(),
        "elements": len(normalized.get("elements") or []),
    }, ensure_ascii=False)


@tool
def update_process_definition(
    element_updates: list[dict],
    top_level_updates: dict | None = None,
    path: str = ".bpmn/process-definition.json",
) -> str:
    """Safely update selected fields while preserving the existing definition.

    ``element_updates`` entries have ``id`` and ``changes`` keys. The changes
    are deep-merged into the matching element. ``top_level_updates`` is used
    for metadata such as skills and DMN arrays. Structural replacement of the
    complete ``elements`` array is deliberately rejected; use
    ``write_process_definition`` for a deliberate structural rewrite.
    """
    import hashlib as _hashlib
    import json as _json

    if path in (".bpmn/process-definition.json", "process-definition.json"):
        target = _bpmn_work_dir() / "process-definition.json"
    else:
        target = _resolve_ws_path(path)
    if not target.is_file():
        return _json.dumps({"updated": False, "error": "missing_file", "path": str(target)}, ensure_ascii=False)
    try:
        current = _json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return _json.dumps({"updated": False, "error": "parse_error", "detail": str(exc)}, ensure_ascii=False)
    if isinstance(current, dict) and isinstance(current.get("processDefinition"), dict):
        current = current["processDefinition"]
    if not isinstance(current, dict) or not isinstance(current.get("elements"), list):
        return _json.dumps({"updated": False, "error": "invalid_definition"}, ensure_ascii=False)

    def merge(target_obj: dict, changes: dict) -> None:
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(target_obj.get(key), dict):
                merge(target_obj[key], value)
            else:
                target_obj[key] = value

    by_id = {
        str(item.get("id")): item
        for item in current["elements"]
        if isinstance(item, dict) and item.get("id")
    }
    changed_ids: list[str] = []
    for update in element_updates or []:
        element_id = str(update.get("id") or "") if isinstance(update, dict) else ""
        changes = update.get("changes") if isinstance(update, dict) else None
        if not element_id or element_id not in by_id or not isinstance(changes, dict):
            return _json.dumps({
                "updated": False,
                "error": "invalid_element_update",
                "element_id": element_id,
            }, ensure_ascii=False)
        if "id" in changes or "elementType" in changes:
            return _json.dumps({
                "updated": False,
                "error": "immutable_element_identity",
                "element_id": element_id,
            }, ensure_ascii=False)
        merge(by_id[element_id], changes)
        changed_ids.append(element_id)

    top = top_level_updates or {}
    if not isinstance(top, dict) or "elements" in top or "processDefinition" in top:
        return _json.dumps({"updated": False, "error": "structural_update_rejected"}, ensure_ascii=False)
    merge(current, top)

    blocking = [
        item for item in _schema_conformance_defects(current)
        if str(item.get("type")) in _SCHEMA_BLOCKING_TYPES
    ]
    if blocking:
        return _json.dumps({
            "updated": False,
            "error": "schema_blocked",
            "defects": blocking,
        }, ensure_ascii=False)

    _atomic_write_json(target, current)
    return _json.dumps({
        "updated": True,
        "path": str(target),
        "sha256": _hashlib.sha256(target.read_bytes()).hexdigest(),
        "changed_element_ids": changed_ids,
        "top_level_keys": sorted(top.keys()),
    }, ensure_ascii=False)
_VALIDATION_RECEIPT = ".process-validation.json"


def _static_process_defects(pd: dict) -> tuple[dict, list]:
    """Normalize a definition and run the same deterministic checks as validation."""
    import copy as _copy

    normalized, _ = _normalize_to_processgpt_schema(_copy.deepcopy(pd))
    schema = _schema_conformance_defects(normalized)
    if any(str(item.get("type")) in _SCHEMA_BLOCKING_TYPES for item in schema):
        return normalized, schema
    _ensure_skill_scripts_on_path()
    try:
        from save_to_supabase import flatten as _flatten  # type: ignore
        from validation.process_validator import ProcessValidator  # type: ignore

        validator = ProcessValidator(
            llm_call=None,
            save_definition=None,
            engine_base_url="",
            tenant_id=_current_tenant_id.get() or "localhost",
        )
        return normalized, schema + validator._static_check(_flatten(normalized))
    except Exception as exc:
        return normalized, schema + [{
            "severity": "critical",
            "type": "validator_load",
            "detail": str(exc),
        }]


def _definition_digest(path: Path) -> str:
    import hashlib as _hashlib

    return _hashlib.sha256(path.read_bytes()).hexdigest()


def _write_validation_receipt(target: Path, result: dict) -> None:
    """Persist proof that the exact current definition passed validation."""
    import json as _json

    receipt = target.parent / _VALIDATION_RECEIPT
    if result.get("passed") is True:
        receipt.write_text(_json.dumps({
            "passed": True,
            "definition_sha256": _definition_digest(target),
            "iterations": result.get("iterations", 0),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    elif receipt.exists():
        receipt.unlink()


def _validated_definition_defects(target: Path) -> list:
    """Return completion-blocking defects for a definition/validation receipt."""
    import json as _json

    try:
        pd = _json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return [{"severity": "critical", "type": "parse_error", "detail": str(exc)}]
    _, defects = _static_process_defects(pd)
    if defects:
        return defects
    receipt_path = target.parent / _VALIDATION_RECEIPT
    try:
        receipt = _json.loads(receipt_path.read_text(encoding="utf-8"))
    except Exception:
        receipt = {}
    if receipt.get("passed") is not True:
        return [{
            "severity": "critical",
            "type": "validation_required",
            "detail": "validate_process_definition 검증 통과 기록이 없습니다.",
        }]
    if receipt.get("definition_sha256") != _definition_digest(target):
        return [{
            "severity": "critical",
            "type": "definition_changed_after_validation",
            "detail": "검증 후 process-definition.json 이 변경되었습니다. 다시 검증해야 합니다.",
        }]
    return []


@tool
def validate_process_definition(path: str = ".bpmn/process-definition.json") -> str:
    """샌드박스의 프로세스 정의(elements[])를 **스키마+로직 검증**하고, 결함이 있으면 LLM 으로 **최대 2회 자동개선**한다.

    실행 엔진/DB 없이(읽기 전용) pdf2bpmn 의 구조 검사(_static_check)를 그대로 호출한다:
    startEvent/endEvent 존재, 끊긴 시퀀스, startEvent 에서 도달 불가(흐름 단절), endEvent 미연결,
    게이트웨이 없는 분기 등. 결함이 있으면 LLM 으로 정의를 교정해 같은 샌드박스 파일에 다시 쓰고
    재검증한다(최대 2회). 결함이 elements 자체가 비었음을 의미하면 즉시 실패 — 1단계로 돌아가
    실제 elements 를 생성해야 한다.

    Args:
        path: 검증할 정의 파일 경로(기본 `.bpmn/process-definition.json`).

    Returns:
        `{"passed": bool, "iterations": int, "repaired": bool, "remaining_defects": [...]}` JSON.
    """
    import json as _json
    import asyncio as _asyncio

    # 기본 경로면 방별 작업 디렉터리(.bpmn/<room>/)에서 찾는다. 명시 경로면 그대로 해석.
    if path in (".bpmn/process-definition.json", "process-definition.json"):
        target = _bpmn_work_dir() / "process-definition.json"
    else:
        target = _resolve_ws_path(path)
    receipt = target.parent / _VALIDATION_RECEIPT
    if receipt.exists():
        try:
            receipt.unlink()
        except Exception:
            pass
    if not target.is_file():
        return _json.dumps({"passed": False, "iterations": 0, "repaired": False,
                            "remaining_defects": [{"type": "missing_file",
                                                   "detail": f"정의 파일이 없습니다: {target}. 1단계에서 먼저 생성하세요."}]},
                           ensure_ascii=False)
    try:
        pd = _json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        return _json.dumps({"passed": False, "iterations": 0, "repaired": False,
                            "remaining_defects": [{"type": "parse_error", "detail": str(e)}]}, ensure_ascii=False)
    # processDefinition 래퍼로 와도 처리
    if isinstance(pd, dict) and isinstance(pd.get("processDefinition"), dict):
        pd = pd["processDefinition"]

    # 일반 BPMN(Camunda)식으로 생성됐어도 ProcessGPT 규격으로 결정적 변환(안전망) 후 파일에 반영.
    pd, _normalized = _normalize_to_processgpt_schema(pd)
    manifest = {}
    manifest_path = target.parent / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = _json.loads(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            manifest = {}
    _ensure_forms_refs_skills(pd, target.parent, manifest if isinstance(manifest, dict) else {})
    contract_changed = _ensure_gateway_decision_contract(pd, target.parent)
    contract_changed = _ensure_requested_supplemental_artifacts(
        pd, target.parent, _current_user_request.get()
    ) or contract_changed
    if _normalized or contract_changed:
        try:
            _atomic_write_json(target, pd)
        except Exception:
            pass

    _ensure_skill_scripts_on_path()
    try:
        from save_to_supabase import flatten as _flatten  # type: ignore
        from validation.process_validator import ProcessValidator  # type: ignore
        import validate_process as _vp  # type: ignore
    except Exception as e:
        return _json.dumps({"passed": False, "iterations": 0, "repaired": False,
                            "remaining_defects": [{"type": "validator_load", "detail": str(e)}]}, ensure_ascii=False)

    _pv = ProcessValidator(llm_call=None, save_definition=None, engine_base_url="",
                           tenant_id=_current_tenant_id.get() or "localhost")
    _llm = _vp._make_llm_call()

    def _check(pd_obj):
        # 1) 스키마 적합성(ProcessGPT 규격) 먼저 — 구조 자체가 깨졌으면 흐름 검사는 의미 없음.
        schema = _schema_conformance_defects(pd_obj)
        if any(str(x.get("type")) in _SCHEMA_BLOCKING_TYPES for x in schema):
            return schema
        # 2) 흐름 검사(pdf2bpmn _static_check) — flatten 후 연결성/도달성 등.
        try:
            return schema + _pv._static_check(_flatten(pd_obj))
        except Exception as ex:
            return schema + [{"severity": "critical", "type": "flatten_error", "detail": str(ex)}]

    defects = _check(pd)
    repaired = False
    iterations = 0
    for _ in range(2):
        if not defects:
            break
        iterations += 1
        # LLM 교정 — elements[] 정의를 직접 고친다.
        sys_msg = (
            "당신은 ProcessGPT 프로세스 정의(JSON, elements[] 형식)를 고치는 도구입니다. "
            "결함을 모두 해결한 processDefinition 을 **JSON 한 덩어리로만**(코드펜스/설명 없이) 반환하세요.\n"
            "반드시 ProcessGPT 규격을 따르세요(일반 Camunda BPMN 금지):\n"
            "- 최상위: processDefinitionName, processDefinitionId, elements[] (activities/events 분리배열 금지)\n"
            "- 각 요소는 elementType ∈ {Event, Activity, Gateway, Sequence}\n"
            "- Activity: type='UserActivity'(UserTask/ServiceTask 금지), role 필수, outputData 최소 1개, id=영문소문자_언더스코어\n"
            "- Event: type ∈ {StartEvent, EndEvent, IntermediateCatchEvent} — StartEvent·EndEvent 각 1개 이상\n"
            "- Gateway: type ∈ {ExclusiveGateway, ParallelGateway, InclusiveGateway}\n"
            "- Sequence: source·target 필수 — 모든 노드를 StartEvent→EndEvent 로 끊김 없이 연결\n"
            "- assignee/formKey/candidateGroups 같은 일반 BPMN 키 금지 — role/inputData/outputData/tool 사용\n"
            "- 이름/설명/조건은 한글, JSON 주석 금지")
        user_msg = ("결함 목록:\n" + _json.dumps(defects, ensure_ascii=False, indent=2)
                    + "\n\n현재 processDefinition:\n" + _json.dumps(pd, ensure_ascii=False))
        try:
            improved = _asyncio.run(_llm([{"role": "system", "content": sys_msg},
                                          {"role": "user", "content": user_msg}], 8000))
        except Exception:
            improved = None
        if not isinstance(improved, dict):
            break  # LLM 비활성/실패 → 더 못 고침
        if isinstance(improved.get("processDefinition"), dict):
            improved = improved["processDefinition"]
        if not isinstance(improved.get("elements"), list) or not improved["elements"]:
            break
        new_defects = _check(improved)
        # 더 나빠지면 적용 안 함
        if len(new_defects) >= len(defects):
            # 한 번은 받아들여 보되 개선 없으면 종료
            pd = improved
            defects = new_defects
            repaired = True
            break
        pd = improved
        defects = new_defects
        repaired = True

    if repaired:
        try:
            _atomic_write_json(target, pd)
        except Exception:
            pass

    criticals = [d for d in defects if str(d.get("severity") or "").lower() == "critical"]
    result = {
        "passed": len(criticals) == 0,
        "iterations": iterations,
        "repaired": repaired,
        "remaining_defects": defects,
    }
    _write_validation_receipt(target, result)
    return _json.dumps(result, ensure_ascii=False)


def _safe_name(name: str) -> str:
    """스킬 디렉터리/파일용 안전 이름. 영문은 소문자-하이픈, 한글 등 유니코드 글자는 보존
    (한글 스킬명이 'skill' 로 뭉개지지 않게 한다)."""
    s = str(name or "").strip()
    s = re.sub(r"[\s/\\_\-]+", "-", s)         # 공백·슬래시·언더스코어·하이픈 → 하이픈(표기차 통일)
    s = re.sub(r"[^\w\-]+", "", s, flags=re.UNICODE)  # 단어문자(유니코드)/하이픈만
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    if re.fullmatch(r"[A-Za-z0-9\-]*", s or ""):
        s = s.lower()                          # 순수 ASCII 면 소문자화
    return s or "skill"


_DEFAULT_PROC_NAME = "생성된 프로세스"


def _derive_process_name(request: str, pd: dict | None = None) -> str:
    """사용자 요청 문장에서 프로세스명을 도출한다(빈/기본값 보정).

    예) '휴가 신청 프로세스 생성해줘' → '휴가 신청 프로세스',
        '경비 정산 만들어줘' → '경비 정산 프로세스'."""
    s = str(request or "").strip()
    # 명령/요청 상용구 제거
    s = re.sub(r"(에 대한|에 대해|관련)?\s*(업무\s*)?(프로세스|워크플로우|절차)?\s*"
               r"(를|을|좀|새로|하나)?\s*"
               r"(만들어\s*줘|만들어줘|만들어|만들|생성해\s*줘|생성해줘|생성해|생성|작성해\s*줘|작성해줘|작성|"
               r"그려\s*줘|그려줘|그려|설계해\s*줘|설계해줘|설계|짜\s*줘|짜줘|해\s*줘|해줘|부탁해|부탁)\s*$",
               "", s).strip()
    s = re.sub(r"\s+", " ", s).strip(" .,!?\n\t")
    if not s or len(s) > 40:
        # 도출 실패 → pd 의 첫 활동/설명에서 보조 도출, 그래도 없으면 기본값
        if isinstance(pd, dict):
            acts = [e for e in (pd.get("elements") or []) if isinstance(e, dict) and e.get("elementType") == "Activity"]
            if acts and acts[0].get("name"):
                return f"{acts[0]['name']} 프로세스"
        return _DEFAULT_PROC_NAME
    if not re.search(r"(프로세스|워크플로우|절차)$", s):
        s = f"{s} 프로세스"
    return s


def _name_tokens(s: str) -> set:
    return {t for t in re.split(r"[\s\-_]+", str(s or "").lower()) if len(t) >= 2}


def _remap_activity_for_skill(raw_id, skill_name, elements, acts_by_id):
    """manifest 의 skill activity_id 가 활동이 아닌 요소(이벤트/게이트웨이)나 미존재를 가리킬 때
    의도에 맞는 Activity 로 결정적으로 재매핑한다.
      1) 직접/snake 매칭 → 2) 그 요소에 인접한 Activity → 3) 스킬명과 이름 토큰 유사도."""
    acts_snake = {_to_snake(str(k)): v for k, v in acts_by_id.items()}
    # 1) 직접/snake
    rid = _to_snake(str(raw_id or ""))
    if str(raw_id) in acts_by_id:
        return acts_by_id[str(raw_id)]
    if rid in acts_snake:
        return acts_snake[rid]
    all_snake = {_to_snake(str(e.get("id"))): e for e in elements if isinstance(e, dict)}
    seqs = [(_to_snake(str(s.get("source"))), _to_snake(str(s.get("target"))))
            for s in elements if isinstance(s, dict) and s.get("elementType") == "Sequence"]
    # 2) raw_id 가 비활동 노드(이벤트/게이트웨이)면 시퀀스를 따라 가장 가까운 Activity 까지 BFS.
    #    앞쪽(predecessor) 활동 우선 — 예: EndEvent ← Gateway ← Activity 면 그 Activity 에 연결.
    node = all_snake.get(rid)
    if node is not None and node.get("elementType") != "Activity":
        def _bfs(start, neighbors):
            seen = {start}; q = [start]
            while q:
                cur = q.pop(0)
                for nb in neighbors(cur):
                    if nb in acts_snake:
                        return acts_snake[nb]
                    if nb not in seen and nb in all_snake and all_snake[nb].get("elementType") != "Activity":
                        seen.add(nb); q.append(nb)
            return None
        pred = _bfs(rid, lambda n: [s for s, t in seqs if t == n])  # 들어오는 경로(앞 활동)
        if pred is not None:
            return pred
        succ = _bfs(rid, lambda n: [t for s, t in seqs if s == n])  # 나가는 경로(뒤 활동)
        if succ is not None:
            return succ
    # 3) 이름 토큰 유사도
    want = _name_tokens(skill_name)
    best, best_score = None, 0
    for el in acts_by_id.values():
        score = len(want & _name_tokens(el.get("name")))
        if score > best_score:
            best, best_score = el, score
    return best if best_score > 0 else None


def _is_user_activity(el: dict) -> bool:
    """폼을 붙일 사용자 작업인지(userTask/UserActivity, 또는 role 만 있는 작업)."""
    if not isinstance(el, dict) or el.get("elementType") != "Activity":
        return False
    _eid = str(el.get("id") or "").lower()
    if _eid.startswith("dmn") or "_dmn" in _eid or _eid.endswith("_decision"):
        return False  # DMN 은 노드가 아님 → 폼 생성 대상 아님
    t = str(el.get("type") or "").lower()
    if "user" in t:
        return True
    # 타입이 비거나 모호하면 role 이 있는 사람 작업으로 본다(폼 항상 보장).
    if t in ("", "task", "activity"):
        return True
    return False


def _gen_form_html(activity: dict, proc_name: str) -> str:
    """UserActivity 용 폼 HTML(결정적) — **ProcessGPT 폼 컴포넌트 규격**.

    ProcessGPT 폼 빌더/렌더러는 `<section><div class='row' ...><div class='col-sm-12'>
    <text-field|textarea-field ...></...>` 구조를 요구한다. 평문 `<form><input>` 은
    폼 편집기에서 빈 화면으로 나오므로 절대 쓰지 않는다."""
    aname = str(activity.get("name") or activity.get("id") or "작업")
    aid = _to_snake(str(activity.get("id") or "field")) or "field"
    fields = []
    for i, f in enumerate(activity.get("outputData") or [], start=1):
        fname = (f.get("name") if isinstance(f, dict) else str(f)) or ""
        fname = str(fname).strip()
        if not fname:
            continue
        # 필드 name 은 고유해야 한다. 라벨이 한글이면 ascii 화 시 비므로 활동id+인덱스로 고유 보장.
        fid = re.sub(r"[^a-zA-Z0-9_]+", "_", fname).strip("_").lower()
        if not fid:
            fid = f"{aid}_{i}"
        fields.append((fid, fname))
    if fields:
        body = "\n".join(
            f"      <text-field name='{fid}' alias='{label}' type='text' disabled='false' readonly='false'></text-field>"
            for fid, label in fields
        )
    else:
        # 정보 부족 → Free Input textarea(폼은 비어있으면 안 됨 — pdf2bpmn 규칙).
        body = f"      <textarea-field name='free_input' alias='{aname}' rows='5' disabled='false' readonly='false'></textarea-field>"
    return (
        "<section>\n"
        f"  <div class='row' name='{aid}_layout' alias='{aname}' is_multidata_mode='false'>\n"
        "    <div class='col-sm-12'>\n"
        f"{body}\n"
        "    </div>\n"
        "  </div>\n"
        "</section>\n"
    )


def _normalize_form_html(html: str) -> str:
    """폼 HTML 의 존재하지 않는 필드 컴포넌트를 유효 컴포넌트로 교정한다.

    ProcessGPT 폼에는 `date-field`/`number-field` 등이 없다(날짜/숫자는 `text-field type='date|number'`).
    모델이 만든 잘못된 태그를 그대로 두면 미리보기에서 렌더되지 않으므로 교정한다."""
    h = str(html or "")
    # (긴 것부터) 잘못된 *-field → text-field type='...'
    conv = [
        ("datetime-local-field", "datetime-local"),
        ("datetime-field", "datetime-local"),
        ("date-field", "date"),
        ("time-field", "time"),
        ("month-field", "month"),
        ("week-field", "week"),
        ("number-field", "number"),
        ("email-field", "email"),
        ("tel-field", "tel"),
        ("url-field", "url"),
        ("password-field", "password"),
        ("color-field", "color"),
    ]
    for bad, typ in conv:
        if f"<{bad}" in h:
            h = re.sub(rf"<{bad}\b", f"<text-field type='{typ}'", h)
            h = h.replace(f"</{bad}>", "</text-field>")
    return h


def _ensure_gateway_decision_contract(pd: dict, work: Path) -> bool:
    """배타 게이트웨이의 분기값·직전 폼·conditionData를 하나의 실행 계약으로 맞춘다.

    모델이 분기선에는 ``승인/반려``를 쓰면서 직전 활동 폼에는 판단 필드를 만들지
    않는 경우가 있다. 이 정의는 그림으로는 정상이나 엔진은 어느 선도 선택할 수 없다.
    각 ExclusiveGateway 바로 전 UserActivity에 분기값을 그대로 선택하는 필드를 보장하고,
    gateway.conditionData가 그 필드를 참조하도록 한다.
    """
    import html as _html

    elements = pd.get("elements") or []
    if not isinstance(elements, list):
        return False
    activities = {
        str(e.get("id")): e for e in elements
        if isinstance(e, dict) and e.get("elementType") == "Activity"
    }
    sequences = [
        e for e in elements
        if isinstance(e, dict) and e.get("elementType") == "Sequence"
    ]
    form_dir = work / "forms"
    changed = False

    for gateway in elements:
        if not isinstance(gateway, dict) or gateway.get("elementType") != "Gateway":
            continue
        if "exclusive" not in str(gateway.get("type") or "").lower():
            continue
        gateway_id = str(gateway.get("id") or "")
        options = []
        for seq in sequences:
            if str(seq.get("source") or "") != gateway_id:
                continue
            value = str(seq.get("condition") or seq.get("name") or "").strip()
            if value and value not in options:
                options.append(value)
        if len(options) < 2:
            continue

        incoming = [
            activities.get(str(seq.get("source") or "")) for seq in sequences
            if str(seq.get("target") or "") == gateway_id
        ]
        incoming = [activity for activity in incoming if activity is not None]
        if len(incoming) != 1:
            continue
        activity = incoming[0]
        activity_id = _to_snake(str(activity.get("id") or ""))
        if not activity_id:
            continue

        tool_name = str(activity.get("tool") or "")
        if tool_name.startswith("formHandler:") and tool_name.split(":", 1)[1].strip():
            form_id = tool_name.split(":", 1)[1].strip()
        else:
            form_id = activity_id
            activity["tool"] = f"formHandler:{form_id}"
            changed = True

        field_name = f"{_to_snake(gateway_id) or activity_id}_decision"
        expected_ref = f"{form_id}.{field_name}"
        if gateway.get("conditionData") != [expected_ref]:
            gateway["conditionData"] = [expected_ref]
            changed = True

        form_path = next(
            (candidate for candidate in (form_dir / f"{activity_id}.form", form_dir / f"{activity_id}.html")
             if candidate.is_file()),
            None,
        )
        if form_path is None:
            continue
        try:
            content = form_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if re.search(rf"\bname\s*=\s*['\"]{re.escape(field_name)}['\"]", content):
            continue
        label = _html.escape(str(gateway.get("name") or "분기 판단"), quote=True)
        option_html = "".join(
            f"<option value='{_html.escape(value, quote=True)}'>{_html.escape(value)}</option>"
            for value in options
        )
        field_html = (
            f"      <select-field name='{field_name}' alias='{label}' disabled='false' readonly='false'>"
            f"{option_html}</select-field>\n"
        )
        insert_at = content.rfind("</div>")
        if insert_at < 0:
            continue
        form_path.write_text(content[:insert_at] + field_html + content[insert_at:], encoding="utf-8")
        output_data = activity.setdefault("outputData", [])
        if isinstance(output_data, list) and field_name not in [
            item.get("name") if isinstance(item, dict) else item for item in output_data
        ]:
            output_data.append(field_name)
        changed = True

    return changed


def _ensure_requested_supplemental_artifacts(pd: dict, work: Path, request: str) -> bool:
    """사용자가 명시한 Markdown/HTML 보조 산출물이 실제 파일로 남도록 보장한다."""
    import html as _html

    request_text = str(request or "")
    wants_markdown = bool(re.search(r"markdown|마크다운|\.md\b|운영\s*가이드", request_text, re.IGNORECASE))
    wants_html = bool(re.search(r"html|\.html\b|보고서\s*템플릿", request_text, re.IGNORECASE))
    changed = False
    elements = [element for element in (pd.get("elements") or []) if isinstance(element, dict)]
    activities = [element for element in elements if element.get("elementType") == "Activity"]
    gateways = [element for element in elements if element.get("elementType") == "Gateway"]
    process_name = str(pd.get("processDefinitionName") or "생성 프로세스")

    non_skill_markdown = [
        fp for fp in work.rglob("*.md")
        if "skills" not in {part.lower() for part in fp.relative_to(work).parts}
        and fp.name.lower() != "validation-report.md"
    ]
    if wants_markdown and not non_skill_markdown:
        step_rows = "\n".join(
            f"| {index} | {activity.get('name') or activity.get('id')} | {activity.get('role') or '-'} |"
            for index, activity in enumerate(activities, 1)
        )
        branch_rows = "\n".join(
            f"- **{gateway.get('name') or gateway.get('id')}**: "
            + ", ".join(
                str(sequence.get("condition") or sequence.get("name") or "기본 경로")
                for sequence in elements
                if sequence.get("elementType") == "Sequence" and sequence.get("source") == gateway.get("id")
            )
            for gateway in gateways
        ) or "- 분기 없음"
        (work / "operation-guide.md").write_text(
            f"# {process_name} 운영 가이드\n\n"
            "## 목적\n\n현업 담당자가 생성된 프로세스를 일관되게 실행하고 증빙을 남기기 위한 가이드입니다.\n\n"
            "## 단계와 책임\n\n| 순서 | 활동 | 담당 역할 |\n|---:|---|---|\n"
            f"{step_rows}\n\n## 분기 기준\n\n{branch_rows}\n\n"
            "## 실행 체크리스트\n\n- 각 폼의 필수 현업 데이터와 증빙 링크를 입력합니다.\n"
            "- 반려·미흡 분기에서는 보완 사유와 변경 내용을 기록합니다.\n"
            "- 종결 전 조치 결과, 효과성 지표, 문서 개정 이력을 확인합니다.\n",
            encoding="utf-8",
        )
        changed = True

    non_form_html = [
        fp for fp in list(work.rglob("*.html")) + list(work.rglob("*.htm"))
        if "forms" not in {part.lower() for part in fp.relative_to(work).parts}
    ]
    if wants_html and not non_form_html:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td></td><td></td></tr>".format(
                index,
                _html.escape(str(activity.get("name") or activity.get("id") or "")),
            )
            for index, activity in enumerate(activities, 1)
        )
        title = _html.escape(process_name)
        (work / "closeout-report.html").write_text(
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            f"<title>{title} 종결보고서</title>"
            "<style>body{font-family:sans-serif;margin:32px;color:#222}table{border-collapse:collapse;width:100%}"
            "th,td{border:1px solid #bbb;padding:8px;text-align:left}h1{color:#075985}</style></head><body>"
            f"<h1>{title} CAPA 종결보고서</h1><p>부적합 처리와 시정조치의 실행·검증 증빙을 기록합니다.</p>"
            "<h2>기본 정보</h2><ul><li>부적합 번호:</li><li>협력사/품목/LOT:</li><li>종결 승인자:</li></ul>"
            "<h2>단계별 결과</h2><table><thead><tr><th>순서</th><th>활동</th><th>처리 결과</th><th>증빙</th></tr></thead>"
            f"<tbody>{rows}</tbody></table><h2>효과성 및 종결 판단</h2><p>핵심 지표, 잔여 위험, 표준 문서 개정 이력을 기재합니다.</p>"
            "</body></html>",
            encoding="utf-8",
        )
        changed = True
    return changed


def _gen_skill_md(
    name: str,
    activity_name: str,
    description: str = "",
    extends: list[str] | None = None,
) -> str:
    """선택된 스킬의 SKILL.md(결정적 폴백). YAML frontmatter + SOP 형식 본문.

    원칙적으로 스킬은 에이전트가 skill-creator 로 풍부하게 만들어야 하지만, 누락 시
    최소한 개요/사용 시점/입력/산출물/절차를 갖춘 형식은 보장한다."""
    desc = (description or f"{activity_name} 작업을 수행합니다.").replace('"', "'").strip()
    title = name.replace("-", " ").replace("_", " ")
    act = activity_name or "해당"

    extends_list = [e.strip() for e in (extends or []) if e.strip()]
    if extends_list:
        if len(extends_list) == 1:
            extends_field = f"extends: {extends_list[0]}\n"
        else:
            extends_field = "extends:\n" + "".join(f"  - {e}\n" for e in extends_list)
        parents_str = ", ".join(extends_list)
        inheritance_directive = (
            f"> **상속 규칙**: 이 스킬은 [{parents_str}] 스킬을 상속합니다. "
            "아래 규칙이 상속된 규칙보다 우선 적용됩니다.\n\n"
        )
    else:
        extends_field = ""
        inheritance_directive = ""

    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{desc}"\n'
        f"{extends_field}"
        "---\n\n"
        f"{inheritance_directive}"
        f"# {title}\n\n"
        "## 개요\n\n"
        f"이 스킬은 '{act}' 단계를 표준 절차에 따라 수행합니다. {desc}\n\n"
        "## 사용 시점\n\n"
        f"- '{act}' 활동을 처리해야 할 때\n"
        "- 동일한 유형의 작업을 일관된 기준으로 반복 처리해야 할 때\n\n"
        "## 입력 / 사전 조건\n\n"
        "- 해당 활동의 입력 데이터(폼/이전 단계 산출물)\n\n"
        "## 산출물\n\n"
        f"- '{act}' 처리 결과\n\n"
        "## 절차\n\n"
        "1. 입력 데이터를 확인한다.\n"
        "2. 표준 기준에 따라 작업을 수행한다.\n"
        "3. 처리 결과를 산출하고 다음 단계로 전달한다.\n"
    )


def _ensure_forms_refs_skills(pd: dict, work, manifest: dict) -> None:
    """요구사항 보장(에이전트 누락 보정): 폼·참조정보는 항상, 선택된 스킬 파일은 빠짐없이 생성.

    - 모든 UserActivity 에 forms/<aid>.html 생성(이미 있으면 유지) + inputData/outputData 보강.
    - manifest.skills[].name ∪ activity.skills[] (= 사용자가 선택한 스킬) 중 SKILL.md 없는 것 생성.
    """
    elements = pd.get("elements") or []
    proc_name = pd.get("processDefinitionName") or "프로세스"

    # 1) 폼 + 참조정보 — 없으면 생성, 있어도 ProcessGPT 폼 규격이 아니면(평문 <form> 등) 교체.
    fdir = work / "forms"
    existing_form_by_aid = {}
    if fdir.is_dir():
        # 폼은 의미있는 확장자 `.form` 으로 생성한다. 기존 `.html` 폼도 함께 읽어 호환한다.
        for g in list(fdir.glob("*.form")) + list(fdir.glob("*.html")):
            existing_form_by_aid.setdefault(_to_snake(g.stem), g)

    def _is_processgpt_form(html: str) -> bool:
        h = (html or "").lower()
        return ("-field" in h) and ("class='row'" in h or 'class="row"' in h or "<section" in h)

    def _data_names(value) -> list[str]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("displayName") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                result.append(name)
        return result

    for a in elements:
        if not _is_user_activity(a):
            continue
        aid = _to_snake(str(a.get("id") or ""))
        if not aid:
            continue
        a["inputData"] = _data_names(a.get("inputData"))
        a["outputData"] = _data_names(a.get("outputData"))
        if not a["outputData"]:
            a["outputData"] = [f"{a.get('name') or aid} 결과"]
        existing = existing_form_by_aid.get(aid)
        try:
            if existing is not None:
                # ProcessGPT 규격이면 유지(단, 잘못된 필드 컴포넌트는 교정), 아니면(평문 HTML) 교체.
                try:
                    cur = existing.read_text(encoding="utf-8")
                    if _is_processgpt_form(cur):
                        fixed = _normalize_form_html(cur)
                        if fixed != cur:
                            existing.write_text(fixed, encoding="utf-8")
                        continue
                except Exception:
                    pass
                existing.write_text(_gen_form_html(a, proc_name), encoding="utf-8")
            else:
                fdir.mkdir(parents=True, exist_ok=True)
                (fdir / f"{aid}.form").write_text(_gen_form_html(a, proc_name), encoding="utf-8")
                existing_form_by_aid[aid] = fdir / f"{aid}.form"
        except Exception:
            pass

    # 2) 스킬: 선택된 스킬(manifest + activity.skills) 중 SKILL.md 없는 것 생성
    selected: dict = {}  # name -> activity_name(설명용)
    for sk in (manifest.get("skills") or []):
        if isinstance(sk, dict):
            nm = str(sk.get("name") or sk.get("safe_name") or sk.get("id") or "").strip()
            if nm:
                selected.setdefault(nm, proc_name)
    for a in elements:
        if isinstance(a, dict) and isinstance(a.get("skills"), list):
            for nm in a["skills"]:
                nm = str(nm or "").strip()
                if nm:
                    selected[nm] = str(a.get("name") or proc_name)
    if not selected:
        return
    sdir = work / "skills"
    existing_skill_keys = set()
    if sdir.is_dir():
        for sd in sdir.iterdir():
            if sd.is_dir() and (sd / "SKILL.md").is_file():
                existing_skill_keys.add(sd.name)
                existing_skill_keys.add(_safe_name(sd.name))
                # 폴더명뿐 아니라 frontmatter name 도 비교(에이전트가 dir='notification-skill',
                # name='알림 전송' 처럼 만들면 manifest 의 '알림 전송' 과 중복 폴더가 생기는 것 방지).
                try:
                    _head = (sd / "SKILL.md").read_text(encoding="utf-8")[:400]
                    _m = re.search(r"^name:\s*[\"']?([^\"'\n]+)", _head, re.MULTILINE)
                    if _m:
                        existing_skill_keys.add(_safe_name(_m.group(1)))
                except Exception:
                    pass
    for nm, act_name in selected.items():
        if nm in existing_skill_keys or _safe_name(nm) in existing_skill_keys:
            continue
        try:
            (sdir / _safe_name(nm)).mkdir(parents=True, exist_ok=True)
            (sdir / _safe_name(nm) / "SKILL.md").write_text(_gen_skill_md(nm, act_name), encoding="utf-8")
            existing_skill_keys.add(_safe_name(nm))
        except Exception:
            pass


def _capture_selection_to_manifest(answer: str) -> None:
    """후보 선택 HITL 답변에서 [스킬]/[에이전트]/[DMN] 선택을 파싱해 manifest.json 에 병합한다.

    에이전트가 선택을 받고도 manifest·skill 을 안 만드는 경우가 잦아, executor 가 답변 원문을
    직접 캡처해 각 process-* 폴더 manifest 에 기록한다(이후 complete 가 SKILL.md 생성·활동 연결)."""
    import json as _json
    a = str(answer or "")
    if not any(k in a for k in ("[스킬]", "[에이전트]", "[DMN]", "스킬:", "에이전트:", "DMN:")):
        return
    skills: list = []
    agents: list = []
    dmns: list = []
    _bad = {"(없음)", "없음", "", "skipped"}
    # 1) "[스킬] 이름" / "[에이전트] 이름" / "[DMN] 이름"
    for cat, name in re.findall(r"\[(스킬|에이전트|DMN)\]\s*([^,\n/]+)", a):
        nm = name.strip()
        if nm in _bad:
            continue
        (skills if cat == "스킬" else agents if cat == "에이전트" else dmns).append(nm)
    # 2) "스킬: A, B" / "에이전트: C" (멀티프로세스 줄 형식)
    for cat, val in re.findall(r"(스킬|에이전트|DMN)\s*:\s*([^/\n]+)", a):
        for nm in [x.strip() for x in val.split(",")]:
            if nm and nm not in _bad:
                (skills if cat == "스킬" else agents if cat == "에이전트" else dmns).append(nm)
    if not (skills or agents or dmns):
        return
    room = _bpmn_work_dir()
    if not room.exists():
        return
    # 이번 생성 시도의 폴더만 대상으로 한다. 예전 시도가 남긴 stale `process-*` 폴더에
    # 선택 결과를 병합하면 완료 단계가 엉뚱한 프로세스에 스킬/에이전트를 붙인다.
    proc_dirs = process_work_dirs(room, _current_conversation_id.get()) or [room]
    for d in proc_dirs:
        mf = d / "manifest.json"
        man: dict = {}
        if mf.is_file():
            try:
                man = _json.loads(mf.read_text(encoding="utf-8")) or {}
            except Exception:
                man = {}
        if not isinstance(man, dict):
            man = {}
        # 에이전트 실제 id 매칭(name → id) — 개별 파일 agents/<id>.json 우선, 레거시 agents.json 호환.
        ag_by_name: dict = {}
        _collect_agents: list = []
        adir = d / "agents"
        if adir.is_dir():
            for af in sorted(adir.glob("*.json")):
                try:
                    obj = _json.loads(af.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(obj, dict):
                    if not obj.get("id"):
                        obj["id"] = af.stem
                    _collect_agents.append(obj)
                elif isinstance(obj, list):
                    _collect_agents.extend([a for a in obj if isinstance(a, dict)])
        aj = d / "agents.json"
        if aj.is_file():
            try:
                _a = _json.loads(aj.read_text(encoding="utf-8"))
                arr = _a if isinstance(_a, list) else (_a.get("agents") if isinstance(_a, dict) else []) or []
                _collect_agents.extend([a for a in arr if isinstance(a, dict)])
            except Exception:
                pass
        for x in _collect_agents:
            if isinstance(x, dict) and x.get("id"):
                ag_by_name[str(x.get("name") or "").strip()] = x["id"]
        man.setdefault("skills", [])
        ex_sk = {_safe_name(s.get("name") if isinstance(s, dict) else s) for s in man["skills"]}
        for nm in skills:
            if _safe_name(nm) not in ex_sk:
                man["skills"].append({"name": nm, "activity_ids": []})
                ex_sk.add(_safe_name(nm))
        man.setdefault("agents", [])
        ex_ag = {(x.get("id") if isinstance(x, dict) else x) for x in man["agents"]}
        for nm in agents:
            aid = ag_by_name.get(nm, nm)
            if aid not in ex_ag:
                man["agents"].append({"id": aid, "name": nm, "activity_ids": []})
                ex_ag.add(aid)
        if dmns:
            man.setdefault("dmn", [])
        try:
            mf.write_text(_json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


def _complete_one_process(work, *, persist: bool = True) -> dict | None:
    """한 프로세스 폴더(work)의 산출물을 ProcessGPT 규격(flattened+통합)으로 조립하고
    process-definition.json 에 최종본을 기록한다. 출력계약 dict 반환(없으면 None)."""
    import json as _json

    pdf = work / "process-definition.json"
    if not pdf.is_file():
        return None
    try:
        pd = _json.loads(pdf.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(pd, dict):
        return None
    if isinstance(pd.get("processDefinition"), dict):
        pd = pd["processDefinition"]
    if isinstance(pd.get("processDefinition"), dict):
        pd = pd["processDefinition"]
    # 일반 BPMN(Camunda)식이어도 ProcessGPT 규격으로 결정적 변환(프론트엔 항상 규격대로 전달).
    pd, _ = _normalize_to_processgpt_schema(pd)
    elements = pd.get("elements")
    if not isinstance(elements, list) or len(elements) == 0:
        return None

    # processDefinitionId 항상 랜덤 UUID
    import uuid as _uuid
    proc_id = str(_uuid.uuid4())
    pd["processDefinitionId"] = proc_id

    # manifest 를 먼저 읽어 '사용자가 선택한' 스킬/에이전트/DMN 을 파악한다.
    manifest: dict = {}
    mf = work / "manifest.json"
    if mf.is_file():
        try:
            _m = _json.loads(mf.read_text(encoding="utf-8"))
            manifest = _m if isinstance(_m, dict) else {}
        except Exception:
            manifest = {}

    # 프로세스명: 비었거나 기본값이면 사용자 요청에서 도출(요구사항: 항상 의미있는 이름).
    _cur_name = str(pd.get("processDefinitionName") or "").strip()
    if not _cur_name or _cur_name == _DEFAULT_PROC_NAME:
        pd["processDefinitionName"] = _derive_process_name(_current_user_request.get(), pd)

    # 결정적 보장: 폼·참조정보는 항상, 선택된 스킬 파일은 빠짐없이 생성(에이전트 누락 보정).
    _ensure_forms_refs_skills(pd, work, manifest)
    _ensure_gateway_decision_contract(pd, work)
    _ensure_requested_supplemental_artifacts(pd, work, _current_user_request.get())

    # forms (파일명 stem == activity id 규칙)
    forms: list[dict] = []
    fdir = work / "forms"
    if fdir.is_dir():
        for fp in sorted(list(fdir.glob("*.form")) + list(fdir.glob("*.html"))):
            try:
                # 폼 파일명(에이전트가 만든 원본 id, camelCase 일 수 있음)을 정규화된 activity id 와
                # 동일 규칙(snake)으로 맞춰 매칭/형식을 일관화한다.
                aid_norm = _to_snake(fp.stem)
                forms.append({
                    "activity_id": aid_norm,
                    "form_id": f"{proc_id}_{aid_norm}_form",
                    "html": fp.read_text(encoding="utf-8"),
                })
            except Exception:
                pass

    # agents — 에이전트별 개별 파일 `agents/<id>.json` 우선 수집, 레거시 `agents.json`(배열/딕셔너리)도 호환.
    agents: list = []
    adir = work / "agents"
    if adir.is_dir():
        for af in sorted(adir.glob("*.json")):
            try:
                obj = _json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(obj, dict):
                # 파일 1개 = 에이전트 1명. id 가 없으면 파일명(stem)을 id 로 사용.
                if not obj.get("id"):
                    obj["id"] = af.stem
                agents.append(obj)
            elif isinstance(obj, list):
                agents.extend([a for a in obj if isinstance(a, dict)])
    ag = work / "agents.json"
    if ag.is_file():
        try:
            a = _json.loads(ag.read_text(encoding="utf-8"))
            legacy = a if isinstance(a, list) else (a.get("agents") if isinstance(a, dict) else []) or []
            seen_ids = {str(x.get("id")) for x in agents if isinstance(x, dict) and x.get("id")}
            for x in legacy:
                if isinstance(x, dict) and str(x.get("id")) not in seen_ids:
                    agents.append(x)
        except Exception:
            pass

    # skills (디렉토리명 + frontmatter name)
    skills: list[str] = []
    sdir = work / "skills"
    if sdir.is_dir():
        for sd in sorted(sdir.iterdir()):
            if not sd.is_dir():
                continue
            name = sd.name
            mdf = sd / "SKILL.md"
            if mdf.is_file():
                try:
                    head = mdf.read_text(encoding="utf-8")[:600]
                    m = re.search(r"^name:\s*[\"']?([^\"'\n]+)", head, re.MULTILINE)
                    if m:
                        name = m.group(1).strip()
                except Exception:
                    pass
            if name and name not in skills:
                skills.append(name)

    # --- 통합: 생성된 폼/에이전트를 elements 에 반영 후 flattened(proc_def) 스키마로 변환 ---
    form_by_aid = {f["activity_id"]: f["form_id"] for f in forms}
    old_form_ids: dict[str, str] = {}
    agent_by_role: dict[str, str] = {}
    for a in agents:
        if isinstance(a, dict) and a.get("id"):
            r = (a.get("role") or "").strip()
            if r:
                agent_by_role.setdefault(r, str(a["id"]))
    for el in elements:
        if not isinstance(el, dict) or el.get("elementType") != "Activity":
            continue
        aid = str(el.get("id") or "")
        old_tool = str(el.get("tool") or "")
        if old_tool.startswith("formHandler:"):
            old_form_ids[aid] = old_tool.split(":", 1)[1]
        if aid in form_by_aid:
            # 저장용 UUID가 정해진 뒤에는 항상 실제 form_def ID로 치환한다. 작업 폴더의
            # 임시 formHandler:<activity_id>를 남기면 저장 후 폼/게이트웨이 조회가 끊긴다.
            el["tool"] = f"formHandler:{form_by_aid[aid]}"
        if not el.get("agent"):
            ag_id = agent_by_role.get((el.get("role") or "").strip())
            if ag_id:
                el["agent"] = ag_id
                if (el.get("agentMode") or "none") == "none":
                    el["agentMode"] = "draft"

    # gateway.conditionData 역시 위에서 치환한 최종 form ID를 참조해야 한다.
    form_id_remap = {
        old_form_ids[aid]: final_id for aid, final_id in form_by_aid.items()
        if aid in old_form_ids
    }
    for el in elements:
        if not isinstance(el, dict) or el.get("elementType") != "Gateway":
            continue
        refs = el.get("conditionData")
        if not isinstance(refs, list):
            continue
        remapped = []
        for ref in refs:
            if not isinstance(ref, str) or "." not in ref:
                remapped.append(ref)
                continue
            prefix, field_name = ref.split(".", 1)
            remapped.append(f"{form_id_remap.get(prefix, prefix)}.{field_name}")
        el["conditionData"] = remapped

    # --- manifest.json 결정적 링크: 에이전트가 쓴 skill/agent/dmn → activity/gateway 매핑을 반영
    #     (에이전트가 JSON 을 직접 안 고쳐도 최종 JSON 에 스킬/에이전트/DMN 이 빠짐없이 들어가게 한다).
    #     manifest 는 위에서 이미 읽음. ---
    if manifest:
        # 활동/게이트웨이를 (정규화된) id 로 찾는다(manifest 의 id 는 원본 camelCase 일 수 있음).
        acts = [e for e in elements if isinstance(e, dict) and e.get("elementType") == "Activity"]
        gws = [e for e in elements if isinstance(e, dict) and e.get("elementType") == "Gateway"]
        acts_by_id = {str(e.get("id")): e for e in acts}
        gws_by_id = {str(e.get("id")): e for e in gws}

        def _find_el(raw_id, by_id):
            rid = str(raw_id or "")
            if rid in by_id:
                return by_id[rid]
            snk = _to_snake(rid)
            return by_id.get(snk)

        # skills → activity.skills(+ agentMode/orchestration), top-level skills 보강
        for sk in (manifest.get("skills") or []):
            if not isinstance(sk, dict):
                continue
            name = (sk.get("name") or sk.get("safe_name") or sk.get("id") or "").strip()
            if not name:
                continue
            # 중복 방지: 동일 스킬을 manifest 가 다른 표기('휴가_승인_알림')로 가리켜도
            # 이미 등록된(스킬 폴더 frontmatter) 표준명으로 통일한다('휴가 승인 알림').
            for existing in skills:
                if _safe_name(existing) == _safe_name(name):
                    name = existing
                    break
            if name not in skills:
                skills.append(name)
            aids = sk.get("activity_ids") or sk.get("source_activity_ids") or []
            linked = False
            for aid in aids:
                el = _find_el(aid, acts_by_id) or _remap_activity_for_skill(aid, name, elements, acts_by_id)
                if el is None:
                    continue
                cur = el.get("skills") if isinstance(el.get("skills"), list) else []
                if name not in cur:
                    cur = cur + [name]
                el["skills"] = cur
                el["agentMode"] = "complete"
                el["orchestration"] = "deepagents"
                linked = True
            # activity_ids 가 비었거나 전부 못 붙였으면 이름 유사도로라도 한 활동에 연결(미연결 방지).
            if not linked:
                el = _remap_activity_for_skill("", name, elements, acts_by_id)
                if el is not None:
                    cur = el.get("skills") if isinstance(el.get("skills"), list) else []
                    if name not in cur:
                        el["skills"] = cur + [name]
                    el["agentMode"] = "complete"
                    el["orchestration"] = "deepagents"

        # agents → activity.agent(+ agentMode)
        for ag in (manifest.get("agents") or []):
            if not isinstance(ag, dict):
                continue
            ag_id = (ag.get("id") or ag.get("name") or "").strip()
            if not ag_id:
                continue
            ag_label = (ag.get("name") or ag.get("id") or "").strip()
            linked = False
            for aid in (ag.get("activity_ids") or []):
                el = _find_el(aid, acts_by_id) or _remap_activity_for_skill(aid, ag_label, elements, acts_by_id)
                if el is None:
                    continue
                el["agent"] = ag_id
                if (el.get("agentMode") or "none") == "none":
                    el["agentMode"] = "draft"
                linked = True
            # activity_ids 가 비었으면 이름 유사도로 한 활동에 연결(미연결 방지).
            if not linked:
                el = _remap_activity_for_skill("", ag_label, elements, acts_by_id)
                if el is not None:
                    el["agent"] = ag_id
                    if (el.get("agentMode") or "none") == "none":
                        el["agentMode"] = "draft"

        # dmn → top-level dmn_decisions/dmn_rules(+ gateway 태그)
        # 선택은 됐으나 에이전트가 decisions/rules 를 비워두는 경우가 많아, 비면 게이트웨이 분기에서 결정적으로 파생한다.
        _dmn_dec: list = list(pd.get("dmn_decisions") or [])
        _dmn_rules: list = list(pd.get("dmn_rules") or [])
        _el_by_id = {str(e.get("id")): e for e in elements if isinstance(e, dict)}
        for dm in (manifest.get("dmn") or []):
            if not isinstance(dm, dict):
                continue
            dm_id = dm.get("id") or "decision"
            gw = _find_el(dm.get("gateway_id") or (dm.get("gateway_ids") or [None])[0], gws_by_id)
            dec = dm.get("decisions")
            rules = dm.get("rules")
            if not dec:  # 비면 게이트웨이명으로 결정 1개 생성
                gname = str(gw.get("name")) if isinstance(gw, dict) else "결정"
                dec = [{"id": dm_id, "name": gname, "gateway": (gw.get("id") if isinstance(gw, dict) else dm.get("gateway_id"))}]
            if not rules and isinstance(gw, dict):  # 분기(나가는 시퀀스)에서 규칙 파생
                gid = str(gw.get("id") or "")
                derived = []
                seqs = [e for e in elements if isinstance(e, dict) and e.get("elementType") == "Sequence" and str(e.get("source")) == gid]
                for _i, s in enumerate(seqs):
                    cond = s.get("conditionData") or s.get("condition") or ""
                    if not isinstance(cond, str):
                        cond = _json.dumps(cond, ensure_ascii=False)
                    tgt = _el_by_id.get(str(s.get("target")), {})
                    derived.append({
                        "id": f"{dm_id}_rule_{_i + 1}",
                        "condition": cond or f"분기 {_i + 1}",
                        "result": str(tgt.get("name") or s.get("target") or f"분기{_i + 1}"),
                    })
                rules = derived
            if dec:
                _dmn_dec.extend(dec if isinstance(dec, list) else [dec])
            if rules:
                _dmn_rules.extend(rules if isinstance(rules, list) else [rules])
            if gw is not None and dm.get("id"):
                gw["dmn"] = dm.get("id")
        if _dmn_dec:
            pd["dmn_decisions"] = _dmn_dec
        if _dmn_rules:
            pd["dmn_rules"] = _dmn_rules

    # roles 보강(없으면 elements 의 role 들로 생성) — 예시 스키마의 roles[] 형식
    if not isinstance(pd.get("roles"), list) or not pd.get("roles"):
        seen_roles: list = []
        for el in elements:
            if isinstance(el, dict):
                r = (el.get("role") or "").strip()
                if r and r not in [x["name"] for x in seen_roles]:
                    seen_roles.append({"name": r, "origin": "created",
                                       "endpoint": "role_" + re.sub(r"[^a-zA-Z0-9]+", "_", r).strip("_").lower(),
                                       "resolutionRule": ""})
        if seen_roles:
            pd["roles"] = seen_roles

    # elements[] → flattened(activities/events/gateways/sequences) — pdf2bpmn 저장 스키마
    flat = None
    _ensure_skill_scripts_on_path()
    try:
        from save_to_supabase import flatten as _flatten  # type: ignore
        flat = _flatten(pd)
    except Exception:
        flat = None
    if not isinstance(flat, dict):
        flat = pd  # 변환 실패 시 elements 유지(차선)
    else:
        flat["processDefinitionId"] = proc_id
        flat["processDefinitionName"] = pd.get("processDefinitionName") or "생성된 프로세스"
        flat.setdefault("description", pd.get("description", ""))
        flat.setdefault("isHorizontal", pd.get("isHorizontal", True))
        flat.setdefault("data", pd.get("data", []))
        flat.setdefault("participants", pd.get("participants", []))
        flat.setdefault("subProcesses", pd.get("subProcesses", []))
        if skills:
            _seen_sk: set = set()
            _uniq_sk: list = []
            for _s in skills:  # 표준명(_safe_name) 기준 중복 제거
                _k = _safe_name(_s)
                if _k not in _seen_sk:
                    _seen_sk.add(_k)
                    _uniq_sk.append(_s)
            flat["skills"] = _uniq_sk  # top-level 스킬 목록(이름)
        for k in ("dmn_decisions", "dmn_rules"):
            if pd.get(k) is not None:
                flat[k] = pd[k]
        # 이름 누락(에이전트가 name 미생성) 방지 — 비면 id 로 채워 null 방지.
        for _key in ("activities", "events", "gateways"):
            for _el in flat.get(_key, []) or []:
                if isinstance(_el, dict) and not (_el.get("name") or "").strip():
                    _el["name"] = _el.get("id") or _el.get("name") or ""

    # 최종 flattened 정의를 process-definition.json 에 그대로 기록(파일 UI·저장이 이 최종본을 본다)
    if persist:
        try:
            _atomic_write_json(work / "process-definition.json", flat)
        except Exception:
            pass

    out = {
        "type": "process-definition-result",
        "processDefinition": flat,
        "forms": forms,
        "agents": agents,
        "skills": skills,
    }
    # 산출물 전용: workspace 파일을 삭제하지 않고 보존한 채 결과만 반환한다.
    return out


def _edit_distance_leq(a: str, b: str, k: int = 2) -> bool:
    """레벤슈타인 거리 ≤ k 인지(짧은 슬러그 오타 판별용). 길이 차가 k 초과면 즉시 False."""
    if abs(len(a) - len(b)) > k:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[-1])
        if best > k:  # 이 행 최소값이 이미 k 초과 → 불가능
            return False
        prev = cur
    return prev[-1] <= k


def _reconcile_scattered_room_dirs(room: Path) -> None:
    """에이전트가 방 슬러그를 한두 글자 오타내 산출물이 거의 동일한 다른 방 폴더로
    흩어진 경우, 그 폴더들의 process-* 내용을 정규 방 폴더로 병합한다(없는 파일만 채움)."""
    import shutil
    base = room.parent
    canon = room.name
    if not base.exists() or not canon:
        return
    for sib in base.iterdir():
        if not sib.is_dir() or sib.name == canon:
            continue
        if not _edit_distance_leq(sib.name, canon, 2):
            continue
        for sp in sib.glob("process-*"):
            if not sp.is_dir():
                continue
            dst = room / sp.name
            dst.mkdir(parents=True, exist_ok=True)
            for src in sp.rglob("*"):
                rel = src.relative_to(sp)
                tgt = dst / rel
                if src.is_dir():
                    tgt.mkdir(parents=True, exist_ok=True)
                elif not tgt.exists():  # 정규 폴더 내용 우선 — 빠진 파일만 채움
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, tgt)
                    except Exception:
                        pass


@tool
def complete_process_generation() -> str:
    """**5단계: 생성 완료.** 이 대화의 모든 프로세스 폴더(`.bpmn/<room>/process-*/`)를 모아
    각각 ProcessGPT 규격(flattened + 스킬/에이전트/DMN/폼 통합)으로 최종 정리하고 파일에 기록한다.

    (한 방에 여러 프로세스 가능 — 각 `process-*/` 폴더를 개별 프로세스로 처리한다.
    프로세스 폴더가 없으면 방 루트의 process-definition.json 1개를 처리한다.)

    Returns:
        `{"count": N, "processes": [{"dir","processDefinitionId","processDefinitionName"}...]}` JSON.
        실패 시 "ERROR: ...".
    """
    import json as _json

    room = _bpmn_work_dir()
    _reconcile_scattered_room_dirs(room)  # 오타로 흩어진 거의 동일한 방 폴더 병합
    proc_dirs = process_work_dirs(room, _current_conversation_id.get())
    if not proc_dirs:
        proc_dirs = [room] if (room / "process-definition.json").is_file() else []  # 단일 프로세스 하위호환

    validation_errors: list[dict] = []
    for directory in proc_dirs:
        definition_file = directory / "process-definition.json"
        if not definition_file.is_file():
            validation_errors.append({"dir": directory.name, "defects": [{
                "severity": "critical",
                "type": "missing_file",
                "detail": "process-definition.json 이 없습니다.",
            }]})
            continue
        defects = _validated_definition_defects(definition_file)
        if defects:
            validation_errors.append({"dir": directory.name, "defects": defects})
    if validation_errors:
        return "ERROR: " + _json.dumps({
            "message": "검증을 통과하지 않은 프로세스는 완료할 수 없습니다.",
            "validation_errors": validation_errors,
        }, ensure_ascii=False)

    processes: list[dict] = []
    final_validation_errors: list[dict] = []
    for d in proc_dirs:
        out = _complete_one_process(d, persist=False)
        if not isinstance(out, dict):
            continue
        flat = out.get("processDefinition") or {}
        canonical, final_defects = _static_process_defects(flat)
        if final_defects:
            final_validation_errors.append({"dir": d.name, "defects": final_defects})
            continue

        final_flat = flat
        _, persisted_defects = _static_process_defects(final_flat)
        if persisted_defects:
            final_validation_errors.append({"dir": d.name, "defects": persisted_defects})
            continue

        definition_file = d / "process-definition.json"
        _atomic_write_json(definition_file, final_flat)
        _write_validation_receipt(definition_file, {"passed": True, "iterations": 0})
        processes.append({
            "dir": d.name,
            "processDefinitionId": final_flat.get("processDefinitionId", ""),
            "processDefinitionName": final_flat.get("processDefinitionName", ""),
            "definitionSha256": _definition_digest(definition_file),
        })

    if final_validation_errors:
        return "ERROR: " + _json.dumps({
            "message": "최종 통합본이 검증을 통과하지 못했습니다.",
            "validation_errors": final_validation_errors,
        }, ensure_ascii=False)

    if not processes:
        return ("ERROR: 처리할 process-definition.json 이 없습니다. "
                "각 프로세스를 `.bpmn/<room>/process-<id>/process-definition.json` 으로 생성했는지 확인하세요.")
    return _json.dumps({"count": len(processes), "processes": processes}, ensure_ascii=False)


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

    **Workflow:**

    - If the repo does NOT exist: creates it and pushes initial content to main.
    - If the repo ALREADY exists: creates a feature branch, pushes changes,
      and opens a Pull Request for review. Direct commits to the default branch
      are not allowed — all changes go through the PR workflow.

    Args:
        skill_path: Docker container path to the completed skill folder.
        branch:     Optional custom branch name for the PR. If omitted, an
                    auto-generated branch name is used (skill/<name>/<timestamp>).
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

    if not repo_exists:
        # 신규 레포: 생성 후 main에 초기 커밋 (PR 불필요)
        try:
            html_url = provider.create_repo(skill_name)
        except RuntimeError as e:
            return f"ERROR creating repo: {e}"

        clone_url = provider.get_authenticated_clone_url(provider.username, skill_name)
        return _push_skill_sandbox(
            cwd, skill_name, "main", clone_url, html_url, existing_repo=False
        )

    # 기존 레포: 피처 브랜치 + PR 워크플로우 (기본 브랜치 직접 커밋 금지)
    if branch and branch == "main":
        branch = None  # main 직접 푸시 요청은 자동 브랜치로 전환

    clone_url = provider.get_authenticated_clone_url(provider.username, skill_name)
    html_url = provider.get_repo_html_url(provider.username, skill_name)

    if branch is None:
        import time as _time
        branch = f"skill/{_safe_name(skill_name)}/{int(_time.time())}"

    return _push_skill_sandbox(
        cwd, skill_name, branch, clone_url, html_url, existing_repo=True
    )


@tool
def register_skill_as_tenant(skill_path: str) -> str:
    """Register a packaged skill (.skill file) as a tenant skill without git.

    Use this when there is no git provider configured. After packaging the skill
    with `package_skill.py`, call this tool with the resulting .skill file path
    to register it directly as a tenant skill.

    The tool extracts the archive, copies files to the tenant skill directory, and
    records the registration in the tenant_skills table. No UI upload needed.

    Args:
        skill_path: Container path to the .skill (or .zip) file, e.g. /workspace/my-skill.skill
    """
    import shutil as _shutil
    import tempfile as _tempfile
    import logging as _logging

    tenant_id = _current_tenant_id.get()
    if not tenant_id:
        return "ERROR: tenant_id가 설정되지 않았습니다. ask_user를 호출하지 말고 작업을 중단하세요."

    host_path = _resolve_ws_path(skill_path)
    if not host_path.is_file():
        return f"ERROR: 파일을 찾을 수 없습니다: {host_path}"

    from .upload_utils import extract_archive, find_skill_root, get_tenant_skill_dest, skill_name_from_filename

    try:
        archive_bytes = host_path.read_bytes()
    except Exception as e:
        return f"ERROR: 파일 읽기 실패: {e}"

    tmpdir = Path(_tempfile.mkdtemp(prefix="skill-register-"))
    try:
        try:
            extract_archive(archive_bytes, tmpdir)
        except ValueError as e:
            return f"ERROR: 압축 해제 실패: {e}"

        skill_root = find_skill_root(tmpdir)
        if skill_root is None:
            return "ERROR: SKILL.md가 없습니다. 유효한 스킬 패키지가 아닙니다."

        skill_name = skill_root.name if skill_root != tmpdir else skill_name_from_filename(host_path.name)
        dest = get_tenant_skill_dest(tenant_id, skill_name, config_id="local")

        if dest.exists():
            return f"ERROR: '{skill_name}' 스킬이 이미 존재합니다 (tenant: {tenant_id}). 다른 이름을 사용하거나 기존 스킬을 삭제하세요."

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _shutil.move(str(skill_root), str(dest))
        except Exception as e:
            return f"ERROR: 파일 복사 실패: {e}"
    finally:
        _shutil.rmtree(str(tmpdir), ignore_errors=True)

    owner_id = _current_user_id.get() or ""
    try:
        from ..storage.db import get_supabase
        get_supabase().table("tenant_skills").insert({
            "tenant_id": tenant_id,
            "skill_name": skill_name,
            "owner_id": owner_id,
        }).execute()
    except Exception as e:
        _logging.getLogger(__name__).warning("tenant_skills 등록 실패 skill=%s: %s", skill_name, e)
        _shutil.rmtree(str(dest), ignore_errors=True)
        return f"ERROR: DB 등록 실패로 파일을 롤백했습니다: {e}"

    return f"✓ '{skill_name}' 테넌트 스킬로 등록 완료 (경로: {dest})"


# ---------------------------------------------------------------------------
# Skill name extraction from tool arguments
# ---------------------------------------------------------------------------

def _skills_from_tool_args(tool_args: dict) -> list[str]:
    """Return skill names whose directory paths appear in any string value of tool_args."""
    from core.skills.skills import _get_skills

    tenant_id = _current_tenant_id.get()
    bases = (_get_skills(tenant_id) if tenant_id else _get_skills()) or []
    if not bases:
        skills_root = os.getenv("SKILLS_DIRS", str(Path(__file__).parent.parent.parent / "skills"))
        bases = [str(Path(skills_root).resolve())]

    known: set[str] = set()
    for base in bases:
        bp = Path(base)
        if not bp.is_dir():
            continue
        try:
            for entry in bp.iterdir():
                if not entry.is_dir():
                    continue
                if (entry / "SKILL.md").exists():
                    known.add(entry.name)
                else:
                    try:
                        for sub in entry.iterdir():
                            if sub.is_dir() and (sub / "SKILL.md").exists():
                                known.add(sub.name)
                    except OSError:
                        pass
        except OSError:
            pass

    if not known:
        return []

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
                for part in v[idx + len(base):].split("/"):
                    if part in known:
                        if part not in found:
                            found.append(part)
                        break

    _scan(tool_args)
    return found
