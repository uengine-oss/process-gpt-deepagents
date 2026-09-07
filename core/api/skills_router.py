"""core/api/skills_router.py — /skills 관련 엔드포인트 모음.

스킬 디렉토리를 스캔해 메타데이터를 제공하고, 테넌트 git provider를 통해
스킬 레포의 파일 커밋/브랜치/커밋이력을 조회한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import yaml

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.api.tenant_auth import (
    auth_guard,
    request_tenant_id,
    request_user_id,
    tenant_guard,
)

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    yaml_text = m.group(1)
    try:
        data = yaml.safe_load(yaml_text)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        # YAML이 깨진 경우(e.g. description에 비따옴표 콜론) regex로 핵심 필드만 추출
        return _parse_frontmatter_regex_fallback(yaml_text)


def _parse_frontmatter_regex_fallback(yaml_text: str) -> dict:
    """YAML 파싱 실패 시 regex로 핵심 필드를 추출한다.

    description 같은 긴 문자열 필드는 콜론 포함 가능성이 높아 추출을 생략하고,
    name / license / extends 처럼 값이 단순한 필드만 복원한다.
    """
    result: dict = {}
    for key in ("name", "license"):
        fm = re.search(rf"^{key}:\s*(.+)$", yaml_text, re.MULTILINE)
        if fm:
            result[key] = fm.group(1).strip().strip("\"'")
    # extends: single-value 형식
    ext_m = re.search(r"^extends:\s*(\S.*)$", yaml_text, re.MULTILINE)
    if ext_m:
        result["extends"] = ext_m.group(1).strip().strip("\"'")
    else:
        # extends: YAML 리스트 형식
        ext_list_m = re.search(r"^extends:\s*$", yaml_text, re.MULTILINE)
        if ext_list_m:
            rest = yaml_text[ext_list_m.end():]
            nk = re.search(r"^\S", rest, re.MULTILINE)
            section = rest[: nk.start()] if nk else rest
            items = [i.strip().strip("\"'") for i in re.findall(r"^\s+-\s+(.+)$", section, re.MULTILINE)]
            if items:
                result["extends"] = items
    return result


def _normalize_extends(val: object) -> list[str]:
    if not val:
        return []
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return []


_BUILTIN_COLLECTION = "anthropics-skills"


def _scan_collection(col_path: str) -> list[dict]:
    col = Path(col_path)
    if not col.is_dir():
        return []
    collection_name = col.name
    skills: list[dict] = []
    try:
        entries = sorted(col.iterdir())
    except OSError:
        return []
    for entry in entries:
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.exists():
            continue
        try:
            meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            meta = {}
        skills.append({
            "name": entry.name,
            "display_name": meta.get("name") or entry.name,
            "description": meta.get("description", ""),
            "collection": collection_name,
            "license": meta.get("license", ""),
            "path": str(entry),
            "extends": _normalize_extends(meta.get("extends")),
        })
    return skills


def _scan_skills(tenant_id: str = "") -> list[dict]:
    from core.skills.skills import _get_skills

    config_id = ""
    if tenant_id:
        try:
            provider = _get_provider(tenant_id)
            config_id = provider.config_id
        except Exception:
            pass

    # Provider-keyed skills have a linked repo
    repo_skill_names: set[str] = set()
    if config_id:
        keyed_dirs = _get_skills(tenant_id, config_id) or []
        for col_path in keyed_dirs:
            col = Path(col_path)
            if not col.is_dir() or col.name == _BUILTIN_COLLECTION:
                continue
            # Only the provider-keyed directory implies a repo link
            if col.name == config_id:
                try:
                    for entry in sorted(col.iterdir()):
                        if entry.is_dir() and (entry / "SKILL.md").exists():
                            repo_skill_names.add(entry.name)
                except OSError:
                    pass

    collection_dirs = _get_skills(tenant_id, config_id) if config_id else _get_skills(tenant_id)
    collection_dirs = collection_dirs or []

    seen_names: set[str] = set()
    skills: list[dict] = []
    for col_path in collection_dirs:
        if Path(col_path).name == _BUILTIN_COLLECTION:
            continue
        for skill in _scan_collection(col_path):
            if skill["name"] in seen_names:
                continue
            seen_names.add(skill["name"])
            skill["has_repo"] = skill["name"] in repo_skill_names
            skills.append(skill)

    # Compute children: reverse-index extends for tree rendering
    children_map: dict[str, list[str]] = {}
    for skill in skills:
        for parent_name in skill.get("extends", []):
            children_map.setdefault(parent_name, []).append(skill["name"])
    for skill in skills:
        skill["children"] = children_map.get(skill["name"], [])

    return skills


def _scan_builtin_skills() -> list[dict]:
    from core.skills.skills import _get_skills

    collection_dirs = _get_skills() or []
    for col_path in collection_dirs:
        if Path(col_path).name == _BUILTIN_COLLECTION:
            return _scan_collection(col_path)
    return []


def _find_skill_dir(skill_name: str, tenant_id: str = "", config_id: str = "") -> Path | None:
    """스킬 이름으로 디스크 상의 스킬 디렉토리를 반환한다. 없으면 None."""
    from core.skills.skills import _get_skills

    collection_dirs = _get_skills(tenant_id, config_id) or []
    for col_path in collection_dirs:
        candidate = Path(col_path) / skill_name
        if candidate.is_dir() and (candidate / "SKILL.md").exists():
            return candidate
    # 업로드된 스킬은 {tenant}/{config_id or 'local'}/{name} 에 저장되므로(컬렉션 직속이 아님) 추가 탐색.
    if tenant_id:
        td = _find_tenant_skill_dir(skill_name, tenant_id, config_id)
        if td is not None:
            return td
    return None


def _find_tenant_skill_dir(skill_name: str, tenant_id: str, config_id: str = "") -> Path | None:
    """테넌트 전용 컬렉션에서만 스킬 디렉토리를 반환한다. 없으면 None."""
    _BASE_DIR = Path(__file__).parent.parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        root = str((_BASE_DIR / p).resolve()) if not os.path.isabs(p) else p.rstrip("/")
        if config_id:
            # Provider-keyed path first
            candidate = Path(root) / tenant_id / config_id / skill_name
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                return candidate
            # Local uploads fallback
            candidate = Path(root) / tenant_id / "local" / skill_name
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                return candidate
        else:
            candidate = Path(root) / tenant_id / skill_name
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                return candidate
            # config_id 가 없어도 업로드 기본 위치(local) 는 항상 탐색(프론트 getSkillFile 은 config_id 미전달).
            candidate = Path(root) / tenant_id / "local" / skill_name
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                return candidate

        # 마지막으로 테넌트 하위 컬렉션을 전부 훑는다.
        #
        # publish_skill 이 신규 레포를 만들면 스킬 파일은 {tenant}/{provider_config_id}/{skill}
        # 로 들어간다(_auto_register_tenant_skill). 그런데 위 분기는 config_id 를 넘겨받았을
        # 때만 그 경로를 보므로, config_id 없이 부르는 호출부(프론트 삭제는 항상 mode="local"
        # → config_id="")에서는 **목록에는 보이는데 삭제하면 404** 가 난다. 실제로 그렇게
        # 지워지지 않는 스킬이 생겼다. 목록(_scan_skills)은 provider-keyed 디렉터리까지
        # 훑으므로 여기서도 같은 범위를 봐야 한다. 탐색은 테넌트 디렉터리 안으로 한정되므로
        # 다른 테넌트의 스킬이 걸릴 일은 없다.
        try:
            for collection in sorted((Path(root) / tenant_id).iterdir()):
                if not collection.is_dir():
                    continue
                candidate = collection / skill_name
                if candidate.is_dir() and (candidate / "SKILL.md").exists():
                    return candidate
        except OSError:
            pass
    return None


def _get_provider_keyed_skill_root(tenant_id: str, config_id: str) -> Path:
    """provider-keyed 스킬 컬렉션 디렉토리 경로를 반환한다 (존재 여부 무관)."""
    _BASE_DIR = Path(__file__).parent.parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    first = raw.split(",")[0].strip() or str(_BASE_DIR / "skills")
    root = Path(first) if os.path.isabs(first) else (_BASE_DIR / first).resolve()
    return root / tenant_id / config_id


async def _ensure_skill_synced(provider, skill_name: str, tenant_id: str) -> Path | None:
    """원격 스킬을 provider-keyed 로컬 캐시로 fetch한다. 실패하면 None을 반환한다."""
    config_id = provider.config_id
    if not config_id:
        return None

    skill_dir = _get_provider_keyed_skill_root(tenant_id, config_id) / skill_name
    owner = provider.username
    try:
        branch = await asyncio.to_thread(provider.detect_default_branch, owner, skill_name)
        tree = await asyncio.to_thread(provider.list_repo_tree, owner, skill_name, branch)
    except Exception as e:
        logger.warning("_ensure_skill_synced: remote fetch 실패 skill=%s: %s", skill_name, e)
        return None

    await asyncio.to_thread(skill_dir.mkdir, **{"parents": True, "exist_ok": True})
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item["path"]
        try:
            raw_bytes = await asyncio.to_thread(provider.fetch_file, owner, skill_name, branch, path)
            target = (skill_dir / path).resolve()
            target.relative_to(skill_dir.resolve())
            await asyncio.to_thread(target.parent.mkdir, **{"parents": True, "exist_ok": True})
            await asyncio.to_thread(target.write_bytes, raw_bytes)
        except Exception as e:
            logger.warning("_ensure_skill_synced: 파일 fetch 실패 %s: %s", path, e)

    return skill_dir if (skill_dir / "SKILL.md").exists() else None


def _get_provider(tenant_id: str):
    from core.skills.git_providers.factory import get_provider
    return get_provider(tenant_id)


def _save_tenant_skill(tenant_id: str, skill_name: str, owner_id: str) -> None:
    """tenant_skills 테이블에 스킬 등록 정보를 저장한다. 에이전트 도구로 노출하지 않는다."""
    from core.storage.db import get_supabase
    get_supabase().table("tenant_skills").insert({
        "tenant_id": tenant_id,
        "skill_name": skill_name,
        "owner_id": owner_id,
    }).execute()


def _as_list(value: str | list[str] | None) -> list[str]:
    """단일 값 또는 리스트를 배열 컬럼용 리스트로 정규화한다(빈 값 제거)."""
    items = [value] if isinstance(value, str) else list(value or [])
    return [i for i in items if i]


def _normalize_requester_ids(requester_id: str | list[str] | None) -> list[str]:
    """requester_id 를 uuid[] 컬럼용 리스트로 정규화한다. 값이 없으면 nil UUID 한 건."""
    import uuid

    return _as_list(requester_id) or [str(uuid.UUID(int=0))]


def _record_resource_pr_sync(
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    branch_name: str,
    base_branch: str,
    title: str,
    description: str,
    requester_id: str | list[str] | None = None,
    requester_name: str | list[str] | None = None,
    git_pr_number: int | None = None,
    git_pr_url: str = "",
    git_repo_url: str = "",
) -> tuple[dict | None, str]:
    """resource_pull_requests 에 PR 레코드를 삽입하고 (레코드, 실패사유) 를 돌려준다.

    실패를 로그로만 남기면 PR 은 깃에 만들어졌는데 병합 요청 목록에는 없는 상태가 되고,
    화면에서는 "요청을 올렸는데 목록이 비어 있다" 로만 보인다. 호출부가 응답에 실어
    사용자에게 알릴 수 있도록 사유를 함께 돌려준다.
    """
    from core.storage.db import get_supabase

    requester_ids = _normalize_requester_ids(requester_id)
    requester_names = _as_list(requester_name) or None

    try:
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
        if data:
            return data[0], ""
        return None, "삽입 결과가 비어 있습니다"
    except Exception as e:
        logger.warning(
            "resource_pull_requests 기록 실패: resource=%s/%s branch=%s",
            resource_type, resource_id, branch_name,
            exc_info=True,
        )
        return None, str(e)


@tenant_guard
async def list_skills(request: Request) -> JSONResponse:
    tenant_id = request_tenant_id(request)
    skills = _scan_skills(tenant_id)
    return JSONResponse({"skills": skills, "total": len(skills)})


def _resolve_inheritance_chain(
    skill_name: str,
    tenant_id: str = "",
    config_id: str = "",
) -> dict:
    """스킬의 extends 체인을 재귀 해소해 { skill, extends, chain, warnings } 반환."""
    from core.skills.skills import _get_skills, _parse_extends_from_skill_md

    collection_dirs = (_get_skills(tenant_id, config_id) if config_id else _get_skills(tenant_id)) or []

    # 검색 가능한 모든 스킬 인덱스 구성 (name → skill dict)
    by_name: dict[str, dict] = {}
    for col_path in collection_dirs:
        if Path(col_path).name == _BUILTIN_COLLECTION:
            continue
        for skill in _scan_collection(col_path):
            by_name.setdefault(skill["name"], skill)

    if skill_name not in by_name:
        return None  # type: ignore[return-value]

    warnings: list[str] = []
    chain: list[dict] = []
    done: set[str] = set()
    in_progress: set[str] = set()

    def _visit(name: str) -> None:
        if name in done:
            return
        if name not in by_name:
            warnings.append(f"parent skill '{name}' not found")
            return
        if name in in_progress:
            warnings.append(f"circular inheritance detected: '{name}'")
            return
        in_progress.add(name)
        for parent in _parse_extends_from_skill_md(by_name[name]["path"]):
            _visit(parent)
        in_progress.discard(name)
        if name not in done:
            done.add(name)
            chain.append(by_name[name])

    _visit(skill_name)

    root = by_name[skill_name]
    return {
        "skill": skill_name,
        "extends": root.get("extends", []),
        "chain": chain,
        "warnings": warnings,
    }


@tenant_guard
async def get_skill_inheritance(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)

    config_id = ""
    if tenant_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            config_id = provider.config_id
        except Exception:
            pass

    result = await asyncio.to_thread(
        _resolve_inheritance_chain, skill_name, tenant_id, config_id
    )
    if result is None:
        return JSONResponse({"error": f"skill '{skill_name}' not found"}, status_code=404)
    return JSONResponse(result)


@auth_guard
async def list_builtin_skills(_request: Request) -> JSONResponse:
    skills = _scan_builtin_skills()
    return JSONResponse({"skills": skills, "total": len(skills)})


@tenant_guard
async def commit_skill_file(request: Request) -> JSONResponse:
    """스킬 파일을 커밋한다.

    기본 브랜치(main)로 직접 커밋은 금지된다. 기본 브랜치로 요청 시:
      1. 자동으로 피처 브랜치를 생성
      2. 해당 브랜치에 커밋
      3. PR을 생성하고 resource_pull_requests에 기록
    """
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    file_path = body.get("file_path", "")
    content = body.get("content")
    message = body.get("message") or f"update {file_path}"
    branch = body.get("branch", "main")
    author_name = body.get("author_name") or None
    author_email = body.get("author_email") or None
    requester_id = body.get("requester_id") or None
    requester_name = body.get("requester_name") or author_name

    if not file_path or content is None:
        return JSONResponse({"error": "file_path and content are required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 기본 브랜치 감지
    try:
        default_branch = await asyncio.to_thread(
            provider.detect_default_branch, provider.username, skill_name
        )
    except Exception:
        default_branch = "main"

    is_default_branch = branch == default_branch

    # 빈 레포(초기 커밋 없음) 감지 — 첫 커밋은 main에 직접, feature branch/PR 불필요
    repo_is_empty = False
    if is_default_branch:
        try:
            existing_branches = await asyncio.to_thread(
                provider.list_branches, provider.username, skill_name
            )
            repo_is_empty = len(existing_branches) == 0
        except Exception:
            repo_is_empty = True

    # 기본 브랜치 직접 커밋 금지 → 피처 브랜치 + PR 워크플로우 (빈 레포 제외)
    if is_default_branch and not repo_is_empty:
        import time
        import uuid

        branch = f"skill/{skill_name}/{int(time.time())}"

        # 기본 브랜치에서 피처 브랜치 생성
        try:
            await asyncio.to_thread(
                provider.create_branch, provider.username, skill_name, branch, default_branch
            )
        except Exception as e:
            return JSONResponse({"error": f"브랜치 생성 실패: {e}"}, status_code=500)

    # 피처 브랜치(또는 빈 레포의 경우 default branch)에 커밋
    try:
        result = await asyncio.to_thread(
            provider.update_file, provider.username, skill_name, file_path, content, message, branch,
            author_name, author_email,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # 기본 브랜치로 요청한 경우 PR 생성 (빈 레포 제외)
    pr_result: dict = {}
    if is_default_branch and not repo_is_empty:
        try:
            pr = await asyncio.to_thread(
                provider.create_pull_request,
                provider.username,
                skill_name,
                message,
                f"파일 변경: {file_path}",
                branch,
                default_branch,
            )
            pr_result = {"pr_created": True, **pr}

            # resource_pull_requests DB 기록
            _, record_error = await asyncio.to_thread(
                _record_resource_pr_sync,
                tenant_id=tenant_id,
                resource_type="skill",
                resource_id=skill_name,
                branch_name=branch,
                base_branch=default_branch,
                title=message,
                description=f"파일 변경: {file_path}",
                requester_id=requester_id,
                requester_name=requester_name,
                git_pr_number=pr.get("number"),
                git_pr_url=pr.get("html_url", ""),
                git_repo_url=provider.get_repo_html_url(provider.username, skill_name),
            )
            # PR 은 만들어졌는데 기록만 실패한 경우를 응답에 남긴다 — 이 사실을 숨기면
            # 병합 요청 목록이 비어 보이는 이유를 화면에서 알 길이 없다.
            pr_result["pr_recorded"] = not record_error
            if record_error:
                pr_result["pr_record_error"] = record_error
        except Exception as e:
            logger.warning("PR 생성 실패 skill=%s branch=%s: %s", skill_name, branch, e)
            pr_result = {"pr_created": False, "pr_error": str(e)}

    return JSONResponse({"committed": True, "branch": branch, **result, **pr_result})


@tenant_guard
async def list_skill_branches(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        branches, default_branch = await asyncio.gather(
            asyncio.to_thread(provider.list_branches, provider.username, skill_name),
            asyncio.to_thread(provider.detect_default_branch, provider.username, skill_name),
        )
    except Exception as e:
        # 레포가 아직 없는 것(404)은 오류가 아니라 "안 만든 상태"다. 이걸 500 으로 돌려주면
        # 화면이 "레포 생성" 안내 대신 오류를 띄우고, 반대로 인증 실패(401)까지 빈 목록으로
        # 돌려주면 이미 있는 레포를 "없다"고 말하게 된다 — 둘을 구분해서 알린다.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            return JSONResponse(
                {"branches": [], "default_branch": "main", "repo_missing": True}
            )
        logger.exception("list_branches failed for skill=%s tenant=%s", skill_name, tenant_id)
        return JSONResponse({"error": str(e)}, status_code=500)

    annotated = [{**b, "default": b["name"] == default_branch} for b in branches]
    return JSONResponse({"branches": annotated, "default_branch": default_branch})


@tenant_guard
async def merge_skill_pull_request(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        number = int(request.path_params["number"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "invalid PR number"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    message = body.get("message", "")
    auto_sync = body.get("auto_sync", True)


    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        result = await asyncio.to_thread(
            provider.merge_pull_request, provider.username, skill_name, number, message
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    sync_result: dict = {}
    if auto_sync:
        try:
            owner = provider.username
            config_id = provider.config_id
            branch = await asyncio.to_thread(provider.detect_default_branch, owner, skill_name)
            tree = await asyncio.to_thread(provider.list_repo_tree, owner, skill_name, branch)
            skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
            if skill_dir is None:
                skill_dir = _get_provider_keyed_skill_root(tenant_id, config_id) / skill_name if config_id else None
            if skill_dir is None:
                from core.skills.skills import _get_skills
                collection_dirs = await asyncio.to_thread(_get_skills, tenant_id)
                if collection_dirs:
                    skill_dir = Path(collection_dirs[0]) / skill_name

            if skill_dir is not None:
                updated: list[str] = []
                sync_errors: list[str] = []
                for item in tree:
                    if item.get("type") != "blob":
                        continue
                    path = item["path"]
                    try:
                        raw = await asyncio.to_thread(provider.fetch_file, owner, skill_name, branch, path)
                        target = (skill_dir / path).resolve()
                        target.relative_to(skill_dir.resolve())
                        await asyncio.to_thread(target.parent.mkdir, **{"parents": True, "exist_ok": True})
                        await asyncio.to_thread(target.write_bytes, raw)
                        updated.append(path)
                    except Exception as e:
                        sync_errors.append(f"{path}: {e}")
                sync_result = {"synced": True, "updated": updated, "errors": sync_errors}
        except Exception as e:
            sync_result = {"synced": False, "error": str(e)}

    # resource_pull_requests 상태를 MERGED로 갱신
    try:
        from core.storage.db import get_supabase
        sb = get_supabase()
        sb.table("resource_pull_requests").update({
            "status": "MERGED",
            "merged_at": "now()",
        }).eq("tenant_id", tenant_id).eq(
            "resource_type", "skill"
        ).eq("resource_id", skill_name).eq(
            "git_pr_number", number
        ).eq("status", "OPEN").execute()
    except Exception:
        logger.warning(
            "resource_pull_requests MERGED 갱신 실패: skill=%s pr=%d",
            skill_name, number, exc_info=True,
        )

    return JSONResponse({"merged": True, "number": number, **result, "sync": sync_result})


@tenant_guard
async def create_skill_pull_request(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    title = body.get("title", "")
    description = body.get("description", "")
    head = body.get("head", "")
    base = body.get("base", "main")

    if not title or not head:
        return JSONResponse({"error": "title and head are required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        result = await asyncio.to_thread(
            provider.create_pull_request,
            provider.username,
            skill_name,
            title,
            description,
            head,
            base,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"created": True, **result})


@tenant_guard
async def get_pull_request_files(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        number = int(request.path_params["number"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "invalid PR number"}, status_code=400)

    tenant_id = request_tenant_id(request)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        files = await asyncio.to_thread(
            provider.get_pull_request_files, provider.username, skill_name, number
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"files": files})


@tenant_guard
async def list_skill_pull_requests(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)
    state = request.query_params.get("state", "open")

    if state not in ("open", "closed", "all"):
        return JSONResponse({"error": "state must be 'open', 'closed', or 'all'"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        pull_requests = await asyncio.to_thread(
            provider.list_pull_requests, provider.username, skill_name, state
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"pull_requests": pull_requests, "total": len(pull_requests)})


@tenant_guard
async def list_skill_commits(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)
    branch = request.query_params.get("branch") or None
    try:
        limit = min(int(request.query_params.get("limit", "20")), 100)
    except ValueError:
        limit = 20


    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        commits = await asyncio.to_thread(
            provider.list_commits, provider.username, skill_name, branch, limit
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"commits": commits})


@tenant_guard
async def list_skill_files(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)

    config_id = ""
    if tenant_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            config_id = provider.config_id
        except Exception:
            pass

    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is None and config_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            skill_dir = await _ensure_skill_synced(provider, skill_name, tenant_id)
        except Exception:
            pass
    if skill_dir is None:
        return JSONResponse({"error": f"skill '{skill_name}' not found"}, status_code=404)

    resolved = skill_dir.resolve()
    files = []
    for p in sorted(resolved.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        stat = p.stat()
        files.append({
            "path": str(p.relative_to(resolved)),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })

    return JSONResponse({"skill_name": skill_name, "files": files, "count": len(files)})


@tenant_guard
async def export_skill(request: Request) -> Response:
    """스킬 디렉토리 전체를 zip으로 묶어 반환한다.

    프로세스 컴포넌트 패키지(export) 파이프라인에서 스킬 파일을 동봉하기 위해 사용한다.
    zip 내부는 스킬 디렉토리 기준 상대 경로(SKILL.md 가 루트)로 구성되어,
    그대로 다시 압축하면 /skills/upload 가 받는 형태와 호환된다.
    """
    import io
    import zipfile

    skill_name = request.path_params["skill_name"]
    tenant_id = request_tenant_id(request)

    config_id = ""
    if tenant_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            config_id = provider.config_id
        except Exception:
            pass

    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is None and config_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            skill_dir = await _ensure_skill_synced(provider, skill_name, tenant_id)
        except Exception:
            pass
    if skill_dir is None:
        return JSONResponse({"error": f"skill '{skill_name}' not found"}, status_code=404)

    resolved = skill_dir.resolve()

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(resolved.rglob("*")):
                if not p.is_file() or ".git" in p.parts:
                    continue
                zf.write(p, arcname=str(p.relative_to(resolved)))
        return buf.getvalue()

    data = await asyncio.to_thread(_build_zip)
    headers = {"Content-Disposition": f'attachment; filename="{skill_name}.zip"'}
    return Response(content=data, media_type="application/zip", headers=headers)


@tenant_guard
async def delete_skill(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    mode = body.get("mode", "local")  # "local" | "repo"

    if mode not in ("local", "repo"):
        return JSONResponse({"error": "mode must be 'local' or 'repo'"}, status_code=400)

    if mode == "local":
        # 로컬 전용 삭제는 Git 연동 여부와 무관하게 동작해야 한다 (git 미연동 테넌트도 로컬 스킬은 있음).
        skill_dir = await asyncio.to_thread(_find_tenant_skill_dir, skill_name, tenant_id, "")
        if skill_dir is None:
            return JSONResponse(
                {"error": f"skill '{skill_name}' not found in tenant '{tenant_id}'"}, status_code=404
            )
        try:
            await asyncio.to_thread(shutil.rmtree, skill_dir)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"deleted": True, "mode": "local", "skill_name": skill_name})

    # mode == "repo": Git provider로 원격 레포 삭제 후 로컬도 정리
    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    config_id = provider.config_id

    try:
        await asyncio.to_thread(provider.delete_repo, provider.username, skill_name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    skill_dir = await asyncio.to_thread(_find_tenant_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is not None:
        try:
            await asyncio.to_thread(shutil.rmtree, skill_dir)
        except Exception:
            pass

    return JSONResponse({"deleted": True, "mode": "repo", "skill_name": skill_name})


@tenant_guard
async def delete_skill_file(request: Request) -> JSONResponse:
    """스킬 파일을 삭제한다. 기본 브랜치 직접 삭제는 금지 → 피처 브랜치 + PR 워크플로우."""
    skill_name = request.path_params["skill_name"]
    file_path = request.path_params["file_path"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    message = body.get("message") or f"delete {file_path}"
    branch = body.get("branch", "main")
    author_name = body.get("author_name") or None
    author_email = body.get("author_email") or None
    requester_id = body.get("requester_id") or None
    requester_name = body.get("requester_name") or author_name


    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 기본 브랜치 감지
    try:
        default_branch = await asyncio.to_thread(
            provider.detect_default_branch, provider.username, skill_name
        )
    except Exception:
        default_branch = "main"

    is_default_branch = branch == default_branch

    # 기본 브랜치 직접 삭제 금지 → 피처 브랜치 + PR 워크플로우
    if is_default_branch:
        import time

        branch = f"skill/{skill_name}/{int(time.time())}"
        try:
            await asyncio.to_thread(
                provider.create_branch, provider.username, skill_name, branch, default_branch
            )
        except Exception as e:
            return JSONResponse({"error": f"브랜치 생성 실패: {e}"}, status_code=500)

    try:
        result = await asyncio.to_thread(
            provider.delete_file, provider.username, skill_name, file_path, message, branch,
            author_name, author_email,
        )
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # 기본 브랜치로 요청한 경우 PR 생성
    pr_result: dict = {}
    if is_default_branch:
        try:
            pr = await asyncio.to_thread(
                provider.create_pull_request,
                provider.username,
                skill_name,
                message,
                f"파일 삭제: {file_path}",
                branch,
                default_branch,
            )
            pr_result = {"pr_created": True, **pr}

            _, record_error = await asyncio.to_thread(
                _record_resource_pr_sync,
                tenant_id=tenant_id,
                resource_type="skill",
                resource_id=skill_name,
                branch_name=branch,
                base_branch=default_branch,
                title=message,
                description=f"파일 삭제: {file_path}",
                requester_id=requester_id,
                requester_name=requester_name,
                git_pr_number=pr.get("number"),
                git_pr_url=pr.get("html_url", ""),
                git_repo_url=provider.get_repo_html_url(provider.username, skill_name),
            )
            pr_result["pr_recorded"] = not record_error
            if record_error:
                pr_result["pr_record_error"] = record_error
        except Exception as e:
            logger.warning("PR 생성 실패 (delete) skill=%s branch=%s: %s", skill_name, branch, e)
            pr_result = {"pr_created": False, "pr_error": str(e)}

    return JSONResponse({"deleted": True, "branch": branch, "file_path": file_path, **result, **pr_result})


@tenant_guard
async def create_skill_repo(request: Request) -> JSONResponse:
    """원격 Git 레포를 생성하고 로컬 스킬 파일을 초기 커밋으로 push한다."""
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    # 소유자는 요청 본문이 아니라 검증된 토큰에서 가져온다(위조 방지).
    owner_id: str = request_user_id(request) or body.get("owner_id", "")
    initial_content: str | None = body.get("initial_content") or None
    initial_file_path: str = body.get("file_path") or "SKILL.md"

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 이미 레포가 있으면 중복 생성 방지.
    # 단, 레포는 있는데 브랜치가 하나도 없는 "빈 레포"는 레포 생성 직후 초기 커밋 push 가
    # 실패해 남은 상태다. 이때도 409 로 막아버리면 초기 커밋을 다시 넣을 방법이 없어
    # 변경 이력·PR 화면이 영구히 "레포 없음"으로 남는다 → 초기 커밋 단계로 진행한다.
    existing = await asyncio.to_thread(provider.repo_exists, skill_name)
    html_url = ""
    if existing:
        try:
            branches = await asyncio.to_thread(provider.list_branches, provider.username, skill_name)
        except Exception:
            branches = [{"name": "main"}]  # 조회 실패 시엔 보수적으로 "있음" 취급
        if branches:
            return JSONResponse({"error": f"repo '{skill_name}' already exists"}, status_code=409)
        html_url = await asyncio.to_thread(provider.get_repo_html_url, provider.username, skill_name)
        logger.info("빈 레포에 초기 커밋을 다시 시도합니다 skill=%s", skill_name)

    if not existing:
        try:
            html_url = await asyncio.to_thread(provider.create_repo, skill_name)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # GitLab은 토큰 소유자 네임스페이스에 레포를 생성하므로 html_url에서 실제 owner를 추출한다.
    # (DB의 provider.username과 실제 GitLab username이 다를 수 있음)
    try:
        from urllib.parse import urlparse
        _path_parts = urlparse(html_url).path.strip("/").split("/")
        actual_owner = _path_parts[0] if len(_path_parts) >= 2 else provider.username
    except Exception:
        actual_owner = provider.username

    # 로컬 스킬 파일을 원격에 push (local 업로드 또는 provider-keyed 캐시에서 찾음)
    config_id = provider.config_id
    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    pushed: list[str] = []
    errors: list[str] = []

    if skill_dir is None and initial_content:
        # 로컬 파일 없음 + 초기 내용 제공 → 직접 초기 커밋으로 기본 브랜치를 생성
        try:
            await asyncio.to_thread(
                provider.update_file,
                actual_owner,
                skill_name,
                initial_file_path,
                initial_content,
                f"init: create {skill_name}",
                "main",
            )
            pushed.append(initial_file_path)
        except Exception as e:
            errors.append(f"{initial_file_path}: {e}")

        # GitHub/GitLab 커밋 성공 시 로컬 디스크에도 저장 → _scan_skills 반영
        if initial_file_path in pushed:
            from core.skills.upload_utils import get_tenant_skill_dest
            local_file = get_tenant_skill_dest(tenant_id, skill_name, config_id=config_id or "local") / initial_file_path
            try:
                await asyncio.to_thread(local_file.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(local_file.write_text, initial_content, encoding="utf-8")
            except Exception as e:
                logger.warning("로컬 스킬 파일 동기화 실패 skill=%s file=%s: %s", skill_name, initial_file_path, e)
    elif skill_dir is not None:
        resolved = skill_dir.resolve()
        local_files = [
            p for p in sorted(resolved.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        ]
        for file in local_files:
            rel = str(file.relative_to(resolved))
            try:
                content = file.read_text(encoding="utf-8")
                await asyncio.to_thread(
                    provider.update_file,
                    actual_owner,
                    skill_name,
                    rel,
                    content,
                    f"init: add {rel}",
                    "main",
                )
                pushed.append(rel)
            except Exception as e:
                errors.append(f"{rel}: {e}")

    if errors:
        logger.warning("create_skill_repo: push errors for skill=%s: %s", skill_name, errors)
    if not pushed and skill_dir is not None:
        logger.warning("create_skill_repo: no files pushed for skill=%s (skill_dir=%s)", skill_name, skill_dir)

    if pushed and owner_id:
        try:
            await asyncio.to_thread(_save_tenant_skill, tenant_id, skill_name, owner_id)
        except Exception as e:
            logger.error("tenant_skills 저장 실패 skill=%s tenant=%s: %s", skill_name, tenant_id, e)

    return JSONResponse({
        "created": True,
        "skill_name": skill_name,
        "html_url": html_url,
        "pushed": pushed,
        "errors": errors,
    })


@tenant_guard
async def upload_skill(request: Request) -> JSONResponse:
    """압축 파일(.skill, .zip)을 받아 테넌트 스킬 디렉토리에 압축 해제하여 스킬로 등록한다."""
    import tempfile
    from core.skills.upload_utils import (
        FileTooLargeError,
        check_extension,
        check_size,
        extract_archive,
        find_skill_root,
        get_tenant_skill_dest,
        list_skill_files,
        skill_name_from_filename,
    )

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "multipart/form-data parsing failed"}, status_code=400)

    tenant_id = request_tenant_id(request)

    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "file is required"}, status_code=400)

    filename = getattr(upload, "filename", None) or "upload.zip"

    try:
        check_extension(filename)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    content = await upload.read()
    try:
        check_size(len(content))
    except FileTooLargeError as e:
        return JSONResponse({"error": str(e)}, status_code=413)

    skill_name_override = form.get("skill_name", "")
    # 소유자는 요청 본문이 아니라 검증된 토큰에서 가져온다(위조 방지).
    owner_id = request_user_id(request) or form.get("owner_id", "")

    tmpdir = tempfile.mkdtemp(prefix="skill-upload-")
    tmp_path = Path(tmpdir)
    try:
        try:
            extract_archive(content, tmp_path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        skill_root = find_skill_root(tmp_path)
        if skill_root is None:
            return JSONResponse(
                {"error": "SKILL.md not found in archive — not a valid skill package"},
                status_code=422,
            )

        skill_name = (
            skill_name_override.strip()
            or (skill_root.name if skill_root != tmp_path else skill_name_from_filename(filename))
        )

        # overwrite=true 는 "이미 등록된 스킬의 내용을 교체한다"는 명시적 의사표시다.
        # (채팅 산출물 패널의 저장 버튼처럼 사용자가 직접 누른 경우.) 없으면 종전대로 409.
        overwrite = str(form.get("overwrite", "")).strip().lower() in ("1", "true", "yes")

        dest = get_tenant_skill_dest(tenant_id, skill_name, config_id="local")
        already_exists = dest.exists()
        if already_exists and not overwrite:
            return JSONResponse(
                {"error": f"skill '{skill_name}' already exists for tenant '{tenant_id}'"},
                status_code=409,
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        # 교체는 기존 디렉터리를 백업해 두고 진행한다 — 이동이 실패해도 스킬이 사라지지 않는다.
        backup = dest.with_name(f"{dest.name}.bak-{os.getpid()}") if already_exists else None
        if backup is not None:
            await asyncio.to_thread(shutil.rmtree, str(backup), True)
            await asyncio.to_thread(shutil.move, str(dest), str(backup))
        try:
            await asyncio.to_thread(shutil.move, str(skill_root), str(dest))
        except Exception as e:
            if backup is not None and backup.exists() and not dest.exists():
                await asyncio.to_thread(shutil.move, str(backup), str(dest))
            return JSONResponse({"error": f"failed to write skill files: {e}"}, status_code=500)
        if backup is not None:
            await asyncio.to_thread(shutil.rmtree, str(backup), True)
        files = list_skill_files(dest)

        # 교체(overwrite)는 이미 tenant_skills 에 행이 있으므로 다시 insert 하지 않는다
        # (_save_tenant_skill 은 upsert 가 아니라 insert 라 중복 행이 생긴다).
        if not already_exists:
            try:
                await asyncio.to_thread(_save_tenant_skill, tenant_id, skill_name, owner_id)
            except Exception as e:
                logger.error("tenant_skills 저장 실패 skill=%s tenant=%s: %s", skill_name, tenant_id, e)

        return JSONResponse(
            {"registered": True, "skill_name": skill_name, "files": files, "overwritten": already_exists},
            status_code=201,
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, True)


@tenant_guard
async def upload_skill_from_git(request: Request) -> JSONResponse:
    """Git 레포지토리 URL을 받아 테넌트 스킬 디렉토리에 clone하여 스킬로 등록한다."""
    import subprocess
    import tempfile
    from core.skills.upload_utils import (
        clone_skill_repo,
        find_skill_root,
        get_tenant_skill_dest,
        list_skill_files,
        skill_name_from_url,
        validate_url_scheme,
    )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    repo_url = body.get("repo_url", "")
    skill_name_override = body.get("skill_name", "")
    # 소유자는 요청 본문이 아니라 검증된 토큰에서 가져온다(위조 방지).
    owner_id = request_user_id(request) or body.get("owner_id", "")

    if not repo_url:
        return JSONResponse({"error": "repo_url is required"}, status_code=400)
    if not owner_id:
        return JSONResponse({"error": "owner_id is required"}, status_code=400)

    try:
        validate_url_scheme(repo_url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    skill_name = skill_name_override.strip() or skill_name_from_url(repo_url)

    dest = get_tenant_skill_dest(tenant_id, skill_name, config_id="local")
    if dest.exists():
        return JSONResponse(
            {"error": f"skill '{skill_name}' already exists for tenant '{tenant_id}'"},
            status_code=409,
        )

    tmpdir = tempfile.mkdtemp(prefix="skill-git-clone-")
    tmp_dest = Path(tmpdir) / skill_name
    try:
        try:
            await asyncio.to_thread(clone_skill_repo, repo_url, tmp_dest)
        except subprocess.TimeoutExpired:
            return JSONResponse(
                {"error": f"git clone timed out for {repo_url!r}"},
                status_code=504,
            )
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=502)

        skill_root = find_skill_root(tmp_dest)
        if skill_root is None:
            return JSONResponse(
                {"error": "SKILL.md not found in repository — not a valid skill"},
                status_code=422,
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(skill_root), str(dest))
        files = list_skill_files(dest)

        try:
            await asyncio.to_thread(_save_tenant_skill, tenant_id, skill_name, owner_id)
        except Exception as e:
            logger.error("tenant_skills 저장 실패 skill=%s tenant=%s: %s", skill_name, tenant_id, e)
            await asyncio.to_thread(shutil.rmtree, str(dest), True)
            return JSONResponse({"error": f"skill 등록 중 DB 저장 실패: {e}"}, status_code=500)

        return JSONResponse(
            {"registered": True, "skill_name": skill_name, "repo_url": repo_url, "files": files},
            status_code=201,
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, True)


@tenant_guard
async def create_skill_branch(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)
    new_branch = body.get("branch", "")
    from_branch = body.get("from", "main")

    if not new_branch:
        return JSONResponse({"error": "branch is required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        result = await asyncio.to_thread(
            provider.create_branch, provider.username, skill_name, new_branch, from_branch
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"created": True, **result})


@tenant_guard
async def list_remote_skill_files(request: Request) -> JSONResponse:
    """원격 Git 레포의 특정 브랜치 파일 목록을 반환한다 (로컬 디스크 미사용)."""
    skill_name = request.path_params["skill_name"]
    branch = request.query_params.get("branch", "")
    tenant_id = request_tenant_id(request)

    if not branch:
        return JSONResponse({"error": "branch is required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        tree = await asyncio.to_thread(
            provider.list_repo_tree, provider.username, skill_name, branch
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    files = [item for item in tree if item.get("type") == "blob"]
    return JSONResponse({"skill_name": skill_name, "branch": branch, "files": files, "count": len(files)})


@tenant_guard
async def get_remote_skill_file(request: Request) -> JSONResponse:
    """원격 Git 레포의 특정 브랜치에서 파일 내용을 반환한다 (로컬 디스크 미사용)."""
    skill_name = request.path_params["skill_name"]
    branch = request.query_params.get("branch", "")
    file_path = request.path_params["file_path"]
    tenant_id = request_tenant_id(request)

    if not branch:
        return JSONResponse({"error": "branch is required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    try:
        raw = await asyncio.to_thread(
            provider.fetch_file, provider.username, skill_name, branch, file_path
        )
    except FileNotFoundError:
        return JSONResponse({"error": f"file '{file_path}' not found on branch '{branch}'"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    try:
        content = raw.decode("utf-8")
        file_type = "text"
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(raw).decode()
        file_type = "binary"

    return JSONResponse({
        "skill_name": skill_name,
        "branch": branch,
        "file_path": file_path,
        "type": file_type,
        "size": len(raw),
        "content": content,
    })


@tenant_guard
async def sync_skill(request: Request) -> JSONResponse:
    """main 브랜치를 원격에서 가져와 로컬 디스크를 갱신한다."""
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = request_tenant_id(request)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    owner = provider.username
    try:
        branch = await asyncio.to_thread(provider.detect_default_branch, owner, skill_name)
        tree = await asyncio.to_thread(provider.list_repo_tree, owner, skill_name, branch)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    config_id = provider.config_id
    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is None:
        if config_id:
            skill_dir = _get_provider_keyed_skill_root(tenant_id, config_id) / skill_name
        else:
            from core.skills.skills import _get_skills
            collection_dirs = await asyncio.to_thread(_get_skills, tenant_id)
            if not collection_dirs:
                return JSONResponse({"error": "no skills directory configured"}, status_code=500)
            skill_dir = Path(collection_dirs[0]) / skill_name

    blob_paths = [item["path"] for item in tree if item.get("type") == "blob"]

    updated: list[str] = []
    errors: list[str] = []
    for path in blob_paths:
        try:
            raw = await asyncio.to_thread(provider.fetch_file, owner, skill_name, branch, path)
            target = (skill_dir / path).resolve()
            resolved_skill = skill_dir.resolve()
            target.relative_to(resolved_skill)  # path traversal 방지
            await asyncio.to_thread(target.parent.mkdir, **{"parents": True, "exist_ok": True})
            await asyncio.to_thread(target.write_bytes, raw)
            updated.append(path)
        except Exception as e:
            errors.append(f"{path}: {e}")

    return JSONResponse({
        "synced": True,
        "skill_name": skill_name,
        "branch": branch,
        "updated": updated,
        "errors": errors,
    })


@tenant_guard
async def put_skill_file_local(request: Request) -> JSONResponse:
    """스킬 파일 하나를 테넌트 스킬 디렉터리에 **git 없이** 직접 쓴다.

    Git 공급자가 연결되지 않은 테넌트도 스킬을 편집·저장할 수 있어야 한다(버전·병합 관리는
    git 이 연결된 경우에만 제공). git 커밋 경로(`/skills/{name}/commit`)는 provider 를
    요구하므로, 그것과 별개의 경로가 필요하다.
    """
    skill_name = request.path_params["skill_name"]
    file_path = request.path_params["file_path"]
    tenant_id = request_tenant_id(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    content = body.get("content")
    if not isinstance(content, str):
        return JSONResponse({"error": "content (string) is required"}, status_code=400)

    # git provider 가 있으면 그 config_id 디렉터리를, 없으면 'local' 업로드 디렉터리를 찾는다.
    # (_find_tenant_skill_dir 는 config_id 가 주어지면 provider 경로 → local 경로 순으로 본다.)
    config_id = "local"
    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
        config_id = provider.config_id or "local"
    except Exception:
        pass

    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is None:
        return JSONResponse({"error": f"skill '{skill_name}' not found"}, status_code=404)

    # path traversal 방지: resolve 후 skill_dir 하위인지 확인
    resolved_skill = skill_dir.resolve()
    target = (skill_dir / file_path).resolve()
    try:
        target.relative_to(resolved_skill)
    except ValueError:
        return JSONResponse({"error": "invalid file_path"}, status_code=400)
    if target.is_dir():
        return JSONResponse({"error": f"'{file_path}' is a directory"}, status_code=400)

    try:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, "utf-8")
    except Exception as e:
        return JSONResponse({"error": f"failed to write file: {e}"}, status_code=500)

    logger.info("스킬 파일 로컬 저장 skill=%s file=%s tenant=%s", skill_name, file_path, tenant_id)
    return JSONResponse({"saved": True, "skill_name": skill_name, "file_path": file_path, "bytes": len(content.encode())})


@tenant_guard
async def get_skill_file(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    file_path = request.path_params["file_path"]
    tenant_id = request_tenant_id(request)

    config_id = ""
    if tenant_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            config_id = provider.config_id
        except Exception:
            pass

    skill_dir = await asyncio.to_thread(_find_skill_dir, skill_name, tenant_id, config_id)
    if skill_dir is None and config_id:
        try:
            provider = await asyncio.to_thread(_get_provider, tenant_id)
            skill_dir = await _ensure_skill_synced(provider, skill_name, tenant_id)
        except Exception:
            pass
    if skill_dir is None:
        return JSONResponse({"error": f"skill '{skill_name}' not found"}, status_code=404)

    # path traversal 방지: resolve 후 skill_dir 하위인지 확인
    resolved_skill = skill_dir.resolve()
    target = (skill_dir / file_path).resolve()
    try:
        target.relative_to(resolved_skill)
    except ValueError:
        return JSONResponse({"error": "invalid file_path"}, status_code=400)

    if not target.exists():
        return JSONResponse({"error": f"file '{file_path}' not found in skill '{skill_name}'"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"error": f"'{file_path}' is not a file"}, status_code=400)

    stat = target.stat()
    raw = target.read_bytes()
    try:
        content = raw.decode("utf-8")
        file_type = "text"
    except UnicodeDecodeError:
        import base64
        content = base64.b64encode(raw).decode()
        file_type = "binary"

    return JSONResponse({
        "skill_name": skill_name,
        "file_path": file_path,
        "type": file_type,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "content": content,
    })


# ---------------------------------------------------------------------------
# 병합 전 검증 (pre-merge verification)
#
# "이 병합 요청을 병합하면 기존 동작이 깨지는가" 를 병합 전에 답한다.
# 리뷰어가 PR 화면에서 실행하면, 저장된 주요 시나리오를 base(변경 전)/head(변경 후)
# 두 벌로 돌려 채점하고, 통과하던 단계가 깨진 것을 골라낸다.
# 오케스트레이션은 core/skills/pr_verification.py 가 결정론적으로 수행한다.
# ---------------------------------------------------------------------------

# 진행 중인 검증 태스크. 같은 PR 에 중복 실행이 겹치지 않게 잡아 둔다.
_verification_tasks: dict[str, "asyncio.Task"] = {}


def _find_pr_record(tenant_id: str, skill_name: str, number: int) -> dict | None:
    """git PR 번호로 resource_pull_requests 행을 찾는다."""
    from core.storage.db import get_supabase

    rows = (
        get_supabase()
        .table("resource_pull_requests")
        .select("id,resource_type,resource_id,branch_name,base_branch,status,git_pr_number")
        .eq("tenant_id", tenant_id)
        .eq("resource_type", "skill")
        .eq("resource_id", skill_name)
        .eq("git_pr_number", number)
        .limit(1)
        .execute()
    )
    return (rows.data or [None])[0]


def _verification_payload(tenant_id: str, run: dict | None) -> dict:
    """회차 + (완료된 경우) variant 별 결과를 화면이 쓰는 형태로 만든다."""
    if not run:
        return {"run": None, "results": []}
    from core.skills import pr_verification as prv

    results = prv.run_results(tenant_id, run["id"]) if run.get("status") == "succeeded" else []
    return {"run": run, "results": results}


@tenant_guard
async def start_pr_verification(request: Request) -> JSONResponse:
    """병합 요청 검증을 시작한다(백그라운드). 이미 돌고 있으면 그 회차를 그대로 돌려준다."""
    skill_name = request.path_params["skill_name"]
    number = request.path_params["number"]
    tenant_id = request_tenant_id(request)

    from core.skills import pr_verification as prv

    pr = await asyncio.to_thread(_find_pr_record, tenant_id, skill_name, number)
    if not pr:
        return JSONResponse(
            {"error": f"#{number} 병합 요청 기록을 찾을 수 없습니다."}, status_code=404
        )

    existing = await asyncio.to_thread(prv.latest_run_for_pr, tenant_id, pr["id"])
    if existing and existing.get("status") == "running":
        return JSONResponse(_verification_payload(tenant_id, existing))

    created = await asyncio.to_thread(
        prv.create_run, tenant_id=tenant_id, pr=pr, created_by=request_user_id(request) or ""
    )
    if created.get("error"):
        return JSONResponse(
            {"error": created["error"], "message": created["message"]}, status_code=409
        )

    run = created["run"]
    task = asyncio.create_task(
        prv.execute_run(
            tenant_id=tenant_id, run=run, cases=created["cases"], suite_id=created["suite_id"]
        )
    )
    _verification_tasks[run["id"]] = task
    task.add_done_callback(lambda _t, rid=run["id"]: _verification_tasks.pop(rid, None))

    return JSONResponse(_verification_payload(tenant_id, run), status_code=202)


@tenant_guard
async def get_pr_verification(request: Request) -> JSONResponse:
    """이 병합 요청의 최신 검증 결과(진행 상태 포함)를 돌려준다."""
    skill_name = request.path_params["skill_name"]
    number = request.path_params["number"]
    tenant_id = request_tenant_id(request)

    from core.skills import eval_backfill as ebf, eval_tools, pr_verification as prv

    pr = await asyncio.to_thread(_find_pr_record, tenant_id, skill_name, number)
    if not pr:
        return JSONResponse(
            {"error": f"#{number} 병합 요청 기록을 찾을 수 없습니다."}, status_code=404
        )

    def _load() -> dict:
        run = prv.latest_run_for_pr(tenant_id, pr["id"])
        payload = _verification_payload(tenant_id, run)
        # 검증에 쓰이는(또는 쓰일) 시나리오도 함께 준다 — 아직 실행 전이어도 화면이
        # "무엇을 검증하게 되는지" 를 보여줄 수 있어야 한다.
        suite_id = eval_tools.find_suite(tenant_id, pr["resource_id"], pr["resource_type"])
        payload["cases"] = eval_tools.load_suite_cases(suite_id) if suite_id else []
        # 케이스 기준으로 판단한다 — 시나리오 확보 회차가 스위트 행을 먼저 만들어 두므로,
        # 스위트 존재만 보면 아직 시나리오가 없는데도 "있다" 로 보인다.
        payload["has_suite"] = bool(payload["cases"])
        # 시나리오가 없을 때 화면이 "생성 중/실패" 를 같은 폴링으로 볼 수 있게 함께 싣는다.
        payload["backfill"] = ebf.latest_run(
            tenant_id, pr["resource_id"], pr["resource_type"]
        )
        return payload

    try:
        return JSONResponse(await asyncio.to_thread(_load))
    except Exception as e:
        logger.exception("병합 전 검증 조회 실패 skill=%s pr=%s", skill_name, number)
        return JSONResponse({"error": str(e)}, status_code=500)


@tenant_guard
async def start_eval_backfill(request: Request) -> JSONResponse:
    """시나리오가 없는 스킬에 회귀 테스트 시나리오를 만들어 붙인다(백그라운드).

    기준은 **PR 의 base 브랜치**다 — 변경 후(head)를 보고 만들면 바뀐 동작을 기준선으로
    굳혀 버려서 그 검증이 아무것도 잡아내지 못한다(core/skills/eval_backfill.py).
    """
    skill_name = request.path_params["skill_name"]
    number = request.path_params["number"]
    tenant_id = request_tenant_id(request)

    # replace=true 는 "지금 시나리오가 미덥지 않으니 다시 만들어라" 는 리뷰어의 명시적 지시다.
    # 이 플래그가 없으면 기존 스위트는 그대로 지켜진다(create_run 의 suite_exists).
    try:
        body = await request.json()
    except Exception:
        body = {}
    replace = bool((body or {}).get("replace"))

    from core.skills import eval_backfill as ebf

    pr = await asyncio.to_thread(_find_pr_record, tenant_id, skill_name, number)
    if not pr:
        return JSONResponse(
            {"error": f"#{number} 병합 요청 기록을 찾을 수 없습니다."}, status_code=404
        )

    existing = await asyncio.to_thread(
        ebf.latest_run, tenant_id, pr["resource_id"], pr["resource_type"]
    )
    if existing and existing.get("status") == "running":
        return JSONResponse({"backfill": existing})

    created = await asyncio.to_thread(
        ebf.create_run,
        tenant_id=tenant_id,
        resource_id=pr["resource_id"],
        resource_type=pr["resource_type"] or "skill",
        base_ref=pr.get("base_branch") or "main",
        created_by=request_user_id(request) or "",
        replace=replace,
    )
    if created.get("error"):
        return JSONResponse(
            {"error": created["error"], "message": created["message"]}, status_code=409
        )

    run = created["run"]
    task = asyncio.create_task(
        ebf.execute_run(tenant_id=tenant_id, run=run, suite_id=created["suite_id"])
    )
    _verification_tasks[run["id"]] = task
    task.add_done_callback(lambda _t, rid=run["id"]: _verification_tasks.pop(rid, None))

    return JSONResponse({"backfill": run}, status_code=202)


routes = [
    Route("/skills", list_skills, methods=["GET"]),
    Route("/skills-builtin", list_builtin_skills, methods=["GET"]),
    Route("/skills/upload", upload_skill, methods=["POST"]),
    Route("/skills/upload-from-git", upload_skill_from_git, methods=["POST"]),
    Route("/skills/{skill_name}", delete_skill, methods=["DELETE"]),
    Route("/skills/{skill_name}/inheritance", get_skill_inheritance, methods=["GET"]),
    Route("/skills/{skill_name}/repo", create_skill_repo, methods=["POST"]),
    Route("/skills/{skill_name}/sync", sync_skill, methods=["POST"]),
    Route("/skills/{skill_name}/commit", commit_skill_file, methods=["POST"]),
    Route("/skills/{skill_name}/branches", list_skill_branches, methods=["GET"]),
    Route("/skills/{skill_name}/branches", create_skill_branch, methods=["POST"]),
    Route("/skills/{skill_name}/branches/files", list_remote_skill_files, methods=["GET"]),
    Route("/skills/{skill_name}/branches/files/{file_path:path}", get_remote_skill_file, methods=["GET"]),
    Route("/skills/{skill_name}/commits", list_skill_commits, methods=["GET"]),
    Route("/skills/{skill_name}/pull-requests", list_skill_pull_requests, methods=["GET"]),
    Route("/skills/{skill_name}/pull-requests", create_skill_pull_request, methods=["POST"]),
    Route("/skills/{skill_name}/pull-requests/{number:int}/files", get_pull_request_files, methods=["GET"]),
    Route("/skills/{skill_name}/pull-requests/{number:int}/merge", merge_skill_pull_request, methods=["POST"]),
    Route("/skills/{skill_name}/pull-requests/{number:int}/verification", get_pr_verification, methods=["GET"]),
    Route("/skills/{skill_name}/pull-requests/{number:int}/verification", start_pr_verification, methods=["POST"]),
    Route("/skills/{skill_name}/pull-requests/{number:int}/verification/scenarios", start_eval_backfill, methods=["POST"]),
    Route("/skills/{skill_name}/export", export_skill, methods=["GET"]),
    Route("/skills/{skill_name}/files", list_skill_files, methods=["GET"]),
    Route("/skills/{skill_name}/files/{file_path:path}", get_skill_file, methods=["GET"]),
    # git 없이 로컬 스킬 디렉터리에 직접 저장(버전·병합 관리 제외)
    Route("/skills/{skill_name}/files/{file_path:path}", put_skill_file_local, methods=["PUT"]),
    Route("/skills/{skill_name}/files/{file_path:path}", delete_skill_file, methods=["DELETE"]),
]
