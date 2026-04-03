"""LangChain tool wrappers around the sandbox runtime."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import shlex
import uuid
import urllib.request
from typing import Any
from urllib.parse import quote

from processgpt_agent_utils.utils.context_manager import proc_inst_id_var, task_id_var

logger = logging.getLogger(__name__)

try:
    from langchain.tools import tool
except Exception:  # pragma: no cover - test fallback without langchain installed
    def tool(func=None, **_kwargs):  # type: ignore
        if func is None:
            return lambda f: f
        return func

from pathlib import Path

from app.sandbox.runtime import LocalSandboxRuntime, SandboxWorkspace

_RUNTIME = LocalSandboxRuntime()
_WORKSPACES: dict[str, SandboxWorkspace] = {}


def _skills_http_api_base_candidates() -> list[str]:
    """Bases for claude-skills file API (GET /skills/<name>/files, .../files/<path>)."""
    raw = (os.environ.get("PROCESSGPT_SKILLS_API_BASE_URL") or "").strip().rstrip("/")
    out: list[str] = []
    if raw:
        out.append(raw)
    # Local dev: skills server on host. In Docker Compose, set PROCESSGPT_SKILLS_API_BASE_URL instead.
    fallback = "http://localhost:8765"
    if fallback not in out:
        out.append(fallback)
    seen: set[str] = set()
    uniq: list[str] = []
    for b in out:
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def _decode_claude_skills_file_payload(body: bytes) -> bytes:
    """claude-skills returns JSON {{type, content}} for text; raw bytes otherwise."""
    if not body:
        return body
    try:
        text = body.decode("utf-8")
    except Exception:
        return body
    if not text.lstrip().startswith("{"):
        return body
    try:
        d = json.loads(text)
    except Exception:
        return body
    if not isinstance(d, dict):
        return body
    typ = d.get("type")
    if typ == "text" and isinstance(d.get("content"), str):
        return d["content"].encode("utf-8")
    if typ == "binary" and isinstance(d.get("content"), str):
        try:
            return base64.b64decode(d["content"])
        except Exception:
            logger.exception("Failed base64 decode for claude-skills binary payload")
            return body
    return body


def _http_get_bytes(url: str, *, timeout_s: float = 120.0) -> bytes:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _materialize_skill_via_claude_skills_http(
    ws: SandboxWorkspace,
    *,
    skill_name: str,
    dest_subdir: str,
    base_url: str,
) -> dict[str, Any]:
    """Copy all files for a catalog skill from claude-skills HTTP API into the workspace."""
    skill = (skill_name or "").strip().strip("/")
    if not skill or ".." in skill:
        raise ValueError("invalid skill_name")
    base = base_url.rstrip("/")
    list_url = f"{base}/skills/{quote(skill, safe='')}/files"
    raw_list = _http_get_bytes(list_url, timeout_s=60.0)
    try:
        meta = json.loads(raw_list.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"skills API list not JSON: {list_url}") from e
    if not isinstance(meta, dict):
        raise RuntimeError(f"skills API list bad shape: {list_url}")
    files = meta.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError(f"skills API returned no files: {list_url}")

    workspace_root = Path(ws.path).resolve()
    dest_root = (workspace_root / (dest_subdir or "skill").strip("/")).resolve()
    if workspace_root not in dest_root.parents and dest_root != workspace_root:
        raise ValueError("dest_subdir escapes workspace")

    copied: list[str] = []
    skipped: list[str] = []
    for ent in files:
        if not isinstance(ent, dict):
            continue
        rel = str(ent.get("path") or "").strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            skipped.append(rel or "(empty)")
            continue
        read_url = f"{base}/skills/{quote(skill, safe='')}/files/{quote(rel, safe='/')}"
        try:
            payload = _http_get_bytes(read_url, timeout_s=120.0)
            out_bytes = _decode_claude_skills_file_payload(payload)
        except Exception as e:
            skipped.append(f"{rel}: {e}")
            continue
        dst = (dest_root / rel).resolve()
        try:
            dst.relative_to(dest_root)
        except ValueError:
            skipped.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(out_bytes)
        rel_ws = dst.relative_to(workspace_root)
        copied.append(rel_ws.as_posix())

    if not copied:
        raise RuntimeError(
            f"claude-skills API listed {len(files)} file(s) but none could be written "
            f"(base={base_url!r}, skipped={skipped[:8]})"
        )
    return {
        "source": list_url,
        "dest": str(dest_root),
        "copied_files": sorted(copied),
        "materialization": "claude_skills_http_api",
        "skills_api_base": base,
        "skill_name": skill,
    }


def _get_workspace(workspace_id: str) -> SandboxWorkspace:
    ws = _WORKSPACES.get(workspace_id)
    if ws is None:
        raise ValueError(f"Unknown sandbox workspace: {workspace_id}")
    return ws


def _get_or_create_workspace(
    workspace_id: str,
    *,
    fallback_job_id: str = "",
    agent_name: str = "subagent",
) -> SandboxWorkspace:
    """Return a workspace by id; if unknown, create one (best-effort).

    LLMs sometimes confuse job_id with workspace_id. In that case, failing hard
    creates brittle behavior and prevents useful fallbacks. For write/run style
    tools we auto-create a workspace so the run can still proceed.
    """
    ws = _WORKSPACES.get(workspace_id)
    if ws is not None:
        return ws
    effective_job = (fallback_job_id or workspace_id or "").strip() or str(uuid.uuid4())
    ws = _RUNTIME.create_workspace(job_id=effective_job, agent_name=agent_name)
    _WORKSPACES[ws.workspace_id] = ws
    return ws


@tool
def sandbox_create(job_id: str = "", agent_name: str = "subagent") -> dict[str, Any]:
    """Create a sandbox workspace for a subagent task."""
    effective_job = job_id.strip() or str(uuid.uuid4())
    ws = _RUNTIME.create_workspace(job_id=effective_job, agent_name=agent_name)
    _WORKSPACES[ws.workspace_id] = ws
    return {"workspace_id": ws.workspace_id, "path": ws.path, "job_id": ws.job_id}


@tool
def sandbox_write_files(workspace_id: str, files_json: str) -> dict[str, Any]:
    """Write one or more files in sandbox workspace.

    files_json format:
    [
      {"path": "scripts/run.py", "content": "print('hello')"},
      ...
    ]
    """
    ws = _get_or_create_workspace(workspace_id, fallback_job_id="write_files", agent_name="subagent")
    files = json.loads(files_json)
    if not isinstance(files, list):
        raise ValueError("files_json must decode to a list")
    written = _RUNTIME.write_files(ws, files)
    return {"written_files": written, "count": len(written)}


@tool
def sandbox_write_files_and_run(
    workspace_id: str,
    files_json: str,
    command: str,
    timeout_seconds: int = 120,
    env_json: str = "",
) -> dict[str, Any]:
    """Write multiple files, then run a shell command with optional env_json.

    Use this to materialize credentials/config files from context (files_json) and then execute
    a skill-based script with environment variables (env_json) in a single deterministic step.
    """
    ws = _get_or_create_workspace(workspace_id, fallback_job_id="write_files_and_run", agent_name="subagent")
    files = json.loads(files_json)
    if not isinstance(files, list):
        raise ValueError("files_json must decode to a list")
    written = _RUNTIME.write_files(ws, files)
    cmd = (command or "").strip()
    if not cmd:
        raise ValueError("command must be non-empty")
    env: dict[str, str] | None = None
    if env_json and str(env_json).strip():
        parsed = json.loads(env_json)
        if not isinstance(parsed, dict):
            raise ValueError("env_json must decode to an object/dict")
        env = {str(k): str(v) for k, v in parsed.items() if v is not None}
    result = _RUNTIME.run_shell(ws, command=cmd, timeout=int(timeout_seconds), env=env)
    return {
        "written_files": written,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": result.command,
    }


@tool
def sandbox_write_and_run(
    workspace_id: str,
    relative_path: str,
    content: str,
    command: str = "",
    timeout_seconds: int = 120,
    env_json: str = "",
) -> dict[str, Any]:
    """Write one UTF-8 file in the workspace, then run a shell command (default: python3 <file>).

    **Skill-missing fallback:** when mounted skills or read_file cannot supply scripts, synthesize a
    complete runnable script from the work-item (embed credentials from InputData as Python literals
    when safe), then call this tool so execution always happens in one step.
    """
    ws = _get_or_create_workspace(workspace_id, fallback_job_id="write_and_run", agent_name="subagent")
    rel = (relative_path or "").strip()
    if not rel or ".." in Path(rel).parts:
        raise ValueError("relative_path must be non-empty and must not contain '..'")
    written = _RUNTIME.write_files(ws, [{"path": rel, "content": content}])
    cmd = (command or "").strip() or f"python3 {shlex.quote(rel)}"
    env: dict[str, str] | None = None
    if env_json and str(env_json).strip():
        parsed = json.loads(env_json)
        if not isinstance(parsed, dict):
            raise ValueError("env_json must decode to an object/dict")
        env = {str(k): str(v) for k, v in parsed.items() if v is not None}
    result = _RUNTIME.run_shell(ws, command=cmd, timeout=int(timeout_seconds), env=env)
    return {
        "written_files": written,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": result.command,
    }


@tool
def sandbox_run_shell(workspace_id: str, command: str, timeout_seconds: int = 120) -> dict[str, Any]:
    """Run a shell command in sandbox workspace."""
    ws = _get_or_create_workspace(workspace_id, fallback_job_id="run_shell", agent_name="subagent")
    result = _RUNTIME.run_shell(ws, command=command, timeout=timeout_seconds, env=None)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": command,
    }


@tool
def sandbox_read_artifacts(workspace_id: str, patterns_csv: str = "**/*") -> dict[str, Any]:
    """Collect artifact file paths from sandbox workspace."""
    ws = _get_workspace(workspace_id)
    patterns = [p.strip() for p in patterns_csv.split(",") if p.strip()]
    if not patterns:
        patterns = ["**/*"]
    files = _RUNTIME.collect_artifacts(ws, patterns)
    return {"artifacts": files, "count": len(files)}


@tool
def sandbox_copy_mounted_skill(
    workspace_id: str,
    tenant_id: str,
    skill_name: str,
    dest_subdir: str = "skill",
    skills_root: str = "",
) -> dict[str, Any]:
    """Copy skill files into the sandbox: Tier 1 = shared volume, Tier 1b = claude-skills HTTP API.

    Tenant-uploaded skills live under ``<SKILLS_ROOT>/<tenant_id>/<skill_name>/`` on the Docker volume.
    GitHub/catalog skills from ``config.json`` (e.g. anthropics/skills) are often **not** mirrored to
    that path; they are still available via the claude-skills service. When the volume path is missing,
    this tool tries ``PROCESSGPT_SKILLS_API_BASE_URL`` (see Docker Compose) and copies via
    ``GET /skills/<skill_name>/files`` + per-file fetches — same materialization idea as
    ``read_skill_document`` + ``sandbox_write_files``, but batched.

    Optional ``skills_root`` overrides env ``SKILLS_ROOT_DIR`` (default ``/app/skills``).
    """
    ws = _get_or_create_workspace(workspace_id, fallback_job_id="copy_skill", agent_name="subagent")
    # Resolve the actual skills root deterministically. In local (non-docker) runs,
    # `/app/skills` may not exist; failing fast prevents "invent random code" fallbacks.
    candidates: list[str] = []
    sr = skills_root.strip()
    if sr:
        candidates.append(sr)
    env_root = (os.environ.get("SKILLS_ROOT_DIR") or "").strip()
    if env_root:
        candidates.append(env_root)
    # Default docker mount path.
    candidates.append("/app/skills")
    # De-dup while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for c in candidates:
        key = c.rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(key)

    tenant = (tenant_id or "").strip().strip("/")
    skill = (skill_name or "").strip().strip("/")
    if not tenant or not skill:
        raise ValueError("tenant_id and skill_name must be non-empty")

    root = ""
    for base in uniq:
        probe = Path(base).resolve() / tenant / skill
        if probe.is_dir():
            root = str(Path(base).resolve())
            break
    if root:
        return _RUNTIME.copy_mounted_skill_tree(
            ws,
            skills_root=root,
            tenant_id=tenant_id,
            skill_rel_path=skill_name,
            dest_subdir=dest_subdir or "skill",
        )

    tried_vol = ", ".join(uniq) if uniq else "(none)"
    http_errs: list[str] = []
    for api_base in _skills_http_api_base_candidates():
        try:
            out = _materialize_skill_via_claude_skills_http(
                ws,
                skill_name=skill,
                dest_subdir=dest_subdir or "skill",
                base_url=api_base,
            )
            if out.get("copied_files"):
                return out
        except Exception as e:
            http_errs.append(f"{api_base}: {e}")
            logger.info(
                "sandbox_copy_mounted_skill HTTP fallback miss | skill=%r api_base=%r err=%s",
                skill,
                api_base,
                e,
            )

    dest_preview = str((Path(ws.path).resolve() / (dest_subdir or "skill").strip("/")).resolve())
    bases = ", ".join(_skills_http_api_base_candidates()) or "(none)"
    return {
        "source": None,
        "dest": dest_preview,
        "copied_files": [],
        "error": "skill_not_on_volume_or_api",
        "message": (
            f"No volume directory {tenant!r}/{skill!r} under skills roots [{tried_vol}]. "
            f"GitHub/catalog skills are not always synced to the shared volume. "
            f"HTTP materialization from claude-skills was tried (bases: {bases}) but did not succeed."
        ),
        "hint": (
            "Set PROCESSGPT_SKILLS_API_BASE_URL to the claude-skills service "
            "(Docker Compose example: http://claude-skills:8765). "
            "Alternatively use read_skill_document + sandbox_write_files for the same skill."
        ),
        "http_fallback_errors": http_errs[:6],
    }


@tool
def sandbox_read_workspace_files(workspace_id: str, paths_csv: str, max_chars_per_file: int = 200_000) -> dict[str, Any]:
    """Read UTF-8 text files from the sandbox workspace (comma-separatedrelative paths)."""

    ws = _get_workspace(workspace_id)
    base = Path(ws.path).resolve()
    paths = [p.strip() for p in paths_csv.split(",") if p.strip()]
    out: dict[str, str] = {}
    errors: dict[str, str] = {}
    for rel in paths:
        if ".." in Path(rel).parts:
            errors[rel] = "invalid_path"
            continue
        fp = (base / rel).resolve()
        if base not in fp.parents and fp != base:
            errors[rel] = "path_escapes_workspace"
            continue
        if not fp.is_file():
            errors[rel] = "not_found"
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars_per_file:
                text = text[:max_chars_per_file] + "\n... [truncated]\n"
            out[rel] = text
        except OSError as e:
            errors[rel] = str(e)
    return {"files": out, "errors": errors}


@tool
def sandbox_upload_file_to_public_storage(
    workspace_id: str,
    relative_path: str,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Upload a workspace file to Supabase **public** Storage (default bucket ``files``).

    Returns ``public_url`` the user can open in a browser. Use for deliverables (xlsx, pdf, csv, zip, …).
    Requires ``SUPABASE_URL`` and service key (same as the app). Optional ``tenant_id`` scopes object paths.
    """
    ws = _get_workspace(workspace_id)
    base = Path(ws.path).resolve()
    rel = (relative_path or "").strip()
    if not rel or ".." in Path(rel).parts:
        return {"error": "invalid_path", "message": "relative_path must be non-empty and must not contain '..'"}

    fp = (base / rel).resolve()
    if base not in fp.parents and fp != base:
        return {"error": "path_escapes_workspace", "message": "path escapes sandbox workspace"}
    if not fp.is_file():
        return {"error": "not_found", "message": str(rel)}

    max_b = int(os.environ.get("OUTPUT_FILE_MAX_BYTES") or 26_214_400)
    try:
        size = fp.stat().st_size
    except OSError as e:
        return {"error": "stat_failed", "message": str(e)}
    if size > max_b:
        return {"error": "file_too_large", "message": f"max {max_b} bytes", "size_bytes": size}

    try:
        raw = fp.read_bytes()
    except OSError as e:
        return {"error": "read_failed", "message": str(e)}

    mime, _ = mimetypes.guess_type(fp.name)
    tid = (tenant_id or "").strip() or None
    task_id = (task_id_var.get() or "").strip() or None
    proc_inst = (proc_inst_id_var.get() or "").strip() or None

    from app.storage_public import (
        build_deliverable_object_path,
        is_public_storage_upload_enabled,
        try_upload_bytes_to_public_files,
    )

    if not is_public_storage_upload_enabled():
        return {
            "error": "storage_upload_disabled",
            "message": "Set DISABLE_SUPABASE_STORAGE_UPLOAD=0 and configure Supabase keys to enable uploads.",
        }

    object_path = build_deliverable_object_path(
        filename=fp.name,
        tenant_id=tid,
        task_id=task_id,
        proc_inst_id=proc_inst,
    )
    url = try_upload_bytes_to_public_files(object_path=object_path, data=raw, content_type=mime)
    if not url:
        return {
            "error": "upload_failed",
            "message": "Upload failed or Supabase misconfigured; check logs.",
            "storage_path": object_path,
        }
    return {
        "public_url": url,
        "storage_path": object_path,
        "name": fp.name,
        "size_bytes": len(raw),
        "mime_type": mime,
    }


@tool
def sandbox_cleanup(workspace_id: str, keep_on_failure: bool = False) -> dict[str, Any]:
    """Cleanup sandbox workspace."""
    ws = _get_workspace(workspace_id)
    _RUNTIME.cleanup(ws, keep_on_failure=keep_on_failure)
    if not keep_on_failure:
        _WORKSPACES.pop(workspace_id, None)
    return {"workspace_id": workspace_id, "cleaned": not keep_on_failure}


def collect_open_workspace_paths_for_globs(globs: list[str]) -> list[str]:
    """Return absolute paths under still-open sandbox workspaces matching glob patterns each.

    Used at task end to pick up binaries (e.g. xlsx) produced by scripts even when only
    the script path appeared in ``written_files``.
    """
    if not globs:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ws in list(_WORKSPACES.values()):
        for g in globs:
            pat = (g or "").strip()
            if not pat:
                continue
            try:
                for p in _RUNTIME.collect_artifacts(ws, [pat]):
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
            except Exception:
                logger.exception("collect_open_workspace_paths_for_globs failed | glob=%s", pat)
    return sorted(out)


def get_sandbox_tools() -> list[Any]:
    """Return all sandbox tools to attach to subagents (always enabled)."""
    return [
        sandbox_create,
        sandbox_copy_mounted_skill,
        sandbox_write_files,
        sandbox_write_files_and_run,
        sandbox_write_and_run,
        sandbox_run_shell,
        sandbox_read_artifacts,
        sandbox_read_workspace_files,
        sandbox_upload_file_to_public_storage,
        sandbox_cleanup,
    ]

