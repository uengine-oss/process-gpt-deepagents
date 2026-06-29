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

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^([^:\n]+?)\s*:\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    return {k.strip(): v.strip() for k, v in _KV_RE.findall(m.group(1))}


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


def _record_resource_pr_sync(
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    branch_name: str,
    base_branch: str,
    title: str,
    description: str,
    requester_id: str | None = None,
    requester_name: str | None = None,
    git_pr_number: int | None = None,
    git_pr_url: str = "",
    git_repo_url: str = "",
) -> dict | None:
    """resource_pull_requests 테이블에 PR 레코드를 삽입한다 (동기 버전, API 라우터용)."""
    import uuid
    from core.storage.db import get_supabase

    if not requester_id:
        requester_id = str(uuid.UUID(int=0))

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
            "requester_id": requester_id,
            "requester_name": requester_name,
            "git_pr_number": git_pr_number,
            "git_pr_url": git_pr_url,
            "git_repo_url": git_repo_url,
        }
        resp = sb.table("resource_pull_requests").insert(row).execute()
        data = getattr(resp, "data", None)
        return data[0] if data else None
    except Exception:
        logger.warning(
            "resource_pull_requests 기록 실패: resource=%s/%s branch=%s",
            resource_type, resource_id, branch_name,
            exc_info=True,
        )
        return None


async def list_skills(request: Request) -> JSONResponse:
    tenant_id = request.query_params.get("tenant_id", "")
    skills = _scan_skills(tenant_id)
    return JSONResponse({"skills": skills, "total": len(skills)})


async def list_builtin_skills(_request: Request) -> JSONResponse:
    skills = _scan_builtin_skills()
    return JSONResponse({"skills": skills, "total": len(skills)})


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

    tenant_id = body.get("tenant_id", "")
    file_path = body.get("file_path", "")
    content = body.get("content")
    message = body.get("message") or f"update {file_path}"
    branch = body.get("branch", "main")
    author_name = body.get("author_name") or None
    author_email = body.get("author_email") or None
    requester_id = body.get("requester_id") or None
    requester_name = body.get("requester_name") or author_name

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
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

    # 기본 브랜치 직접 커밋 금지 → 피처 브랜치 + PR 워크플로우
    if is_default_branch:
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

    # 피처 브랜치에 커밋
    try:
        result = await asyncio.to_thread(
            provider.update_file, provider.username, skill_name, file_path, content, message, branch,
            author_name, author_email,
        )
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
                f"파일 변경: {file_path}",
                branch,
                default_branch,
            )
            pr_result = {"pr_created": True, **pr}

            # resource_pull_requests DB 기록
            await asyncio.to_thread(
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
        except Exception as e:
            logger.warning("PR 생성 실패 skill=%s branch=%s: %s", skill_name, branch, e)
            pr_result = {"pr_created": False, "pr_error": str(e)}

    return JSONResponse({"committed": True, "branch": branch, **result, **pr_result})


async def list_skill_branches(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request.query_params.get("tenant_id", "")
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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
        logger.exception("list_branches failed for skill=%s tenant=%s", skill_name, tenant_id)
        return JSONResponse({"error": str(e)}, status_code=500)

    annotated = [{**b, "default": b["name"] == default_branch} for b in branches]
    return JSONResponse({"branches": annotated, "default_branch": default_branch})


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

    tenant_id = body.get("tenant_id", "")
    message = body.get("message", "")
    auto_sync = body.get("auto_sync", True)

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def create_skill_pull_request(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    title = body.get("title", "")
    description = body.get("description", "")
    head = body.get("head", "")
    base = body.get("base", "main")

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
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


async def get_pull_request_files(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        number = int(request.path_params["number"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "invalid PR number"}, status_code=400)

    tenant_id = request.query_params.get("tenant_id", "")
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def list_skill_pull_requests(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request.query_params.get("tenant_id", "")
    state = request.query_params.get("state", "open")

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
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


async def list_skill_commits(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request.query_params.get("tenant_id", "")
    branch = request.query_params.get("branch") or None
    try:
        limit = min(int(request.query_params.get("limit", "20")), 100)
    except ValueError:
        limit = 20

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def list_skill_files(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    tenant_id = request.query_params.get("tenant_id", "")

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


async def delete_skill(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    mode = body.get("mode", "local")  # "local" | "repo"

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
    if mode not in ("local", "repo"):
        return JSONResponse({"error": "mode must be 'local' or 'repo'"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    config_id = provider.config_id

    if mode == "local":
        skill_dir = await asyncio.to_thread(_find_tenant_skill_dir, skill_name, tenant_id, config_id)
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


async def delete_skill_file(request: Request) -> JSONResponse:
    """스킬 파일을 삭제한다. 기본 브랜치 직접 삭제는 금지 → 피처 브랜치 + PR 워크플로우."""
    skill_name = request.path_params["skill_name"]
    file_path = request.path_params["file_path"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    message = body.get("message") or f"delete {file_path}"
    branch = body.get("branch", "main")
    author_name = body.get("author_name") or None
    author_email = body.get("author_email") or None
    requester_id = body.get("requester_id") or None
    requester_name = body.get("requester_name") or author_name

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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

            await asyncio.to_thread(
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
        except Exception as e:
            logger.warning("PR 생성 실패 (delete) skill=%s branch=%s: %s", skill_name, branch, e)
            pr_result = {"pr_created": False, "pr_error": str(e)}

    return JSONResponse({"deleted": True, "branch": branch, "file_path": file_path, **result, **pr_result})


async def create_skill_repo(request: Request) -> JSONResponse:
    """원격 Git 레포를 생성하고 로컬 스킬 파일을 초기 커밋으로 push한다."""
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

    try:
        provider = await asyncio.to_thread(_get_provider, tenant_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # 이미 레포가 있으면 중복 생성 방지
    existing = await asyncio.to_thread(provider.repo_exists, skill_name)
    if existing:
        return JSONResponse({"error": f"repo '{skill_name}' already exists"}, status_code=409)

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

    if skill_dir is not None:
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

    return JSONResponse({
        "created": True,
        "skill_name": skill_name,
        "html_url": html_url,
        "pushed": pushed,
        "errors": errors,
    })


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

    tenant_id = form.get("tenant_id", "")
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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

        dest = get_tenant_skill_dest(tenant_id, skill_name, config_id="local")
        if dest.exists():
            return JSONResponse(
                {"error": f"skill '{skill_name}' already exists for tenant '{tenant_id}'"},
                status_code=409,
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(skill_root), str(dest))
        files = list_skill_files(dest)
        return JSONResponse({"registered": True, "skill_name": skill_name, "files": files}, status_code=201)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, True)


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

    tenant_id = body.get("tenant_id", "")
    repo_url = body.get("repo_url", "")
    skill_name_override = body.get("skill_name", "")
    owner_id = body.get("owner_id", "")

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
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


async def create_skill_branch(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    new_branch = body.get("branch", "")
    from_branch = body.get("from", "main")

    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)
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


async def list_remote_skill_files(request: Request) -> JSONResponse:
    """원격 Git 레포의 특정 브랜치 파일 목록을 반환한다 (로컬 디스크 미사용)."""
    skill_name = request.path_params["skill_name"]
    branch = request.query_params.get("branch", "")
    tenant_id = request.query_params.get("tenant_id", "")

    if not branch:
        return JSONResponse({"error": "branch is required"}, status_code=400)
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def get_remote_skill_file(request: Request) -> JSONResponse:
    """원격 Git 레포의 특정 브랜치에서 파일 내용을 반환한다 (로컬 디스크 미사용)."""
    skill_name = request.path_params["skill_name"]
    branch = request.query_params.get("branch", "")
    file_path = request.path_params["file_path"]
    tenant_id = request.query_params.get("tenant_id", "")

    if not branch:
        return JSONResponse({"error": "branch is required"}, status_code=400)
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def sync_skill(request: Request) -> JSONResponse:
    """main 브랜치를 원격에서 가져와 로컬 디스크를 갱신한다."""
    skill_name = request.path_params["skill_name"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tenant_id = body.get("tenant_id", "")
    if not tenant_id:
        return JSONResponse({"error": "tenant_id is required"}, status_code=400)

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


async def get_skill_file(request: Request) -> JSONResponse:
    skill_name = request.path_params["skill_name"]
    file_path = request.path_params["file_path"]
    tenant_id = request.query_params.get("tenant_id", "")

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


routes = [
    Route("/skills", list_skills, methods=["GET"]),
    Route("/skills-builtin", list_builtin_skills, methods=["GET"]),
    Route("/skills/upload", upload_skill, methods=["POST"]),
    Route("/skills/upload-from-git", upload_skill_from_git, methods=["POST"]),
    Route("/skills/{skill_name}", delete_skill, methods=["DELETE"]),
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
    Route("/skills/{skill_name}/files", list_skill_files, methods=["GET"]),
    Route("/skills/{skill_name}/files/{file_path:path}", get_skill_file, methods=["GET"]),
    Route("/skills/{skill_name}/files/{file_path:path}", delete_skill_file, methods=["DELETE"]),
]
