"""LangChain tool wrappers around the sandbox runtime."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shlex
import threading
import uuid
from typing import Any

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

_ws_lock = threading.Lock()
_WORKSPACE_IDS_BY_TASK: dict[str, set[str]] = {}


def _current_task_id() -> str:
    try:
        tid = str(task_id_var.get() or "").strip()
    except Exception:
        tid = ""
    return tid


def _note_workspace_for_current_task(workspace_id: str) -> None:
    tid = _current_task_id()
    wid = str(workspace_id or "").strip()
    if not tid or not wid:
        return
    with _ws_lock:
        bucket = _WORKSPACE_IDS_BY_TASK.setdefault(tid, set())
        bucket.add(wid)


def take_task_workspace_ids(task_id: str) -> set[str]:
    """Return workspace ids created/used by this task (without clearing)."""
    tid = str(task_id or "").strip()
    if not tid:
        return set()
    with _ws_lock:
        return set(_WORKSPACE_IDS_BY_TASK.get(tid, set()) or set())


def discard_task_workspace_ids(task_id: str) -> None:
    tid = str(task_id or "").strip()
    if not tid:
        return
    with _ws_lock:
        _WORKSPACE_IDS_BY_TASK.pop(tid, None)


def _get_workspace(workspace_id: str) -> SandboxWorkspace:
    ws = _WORKSPACES.get(workspace_id)
    if ws is not None:
        return ws

    # Probe filesystem for multi-process / restart re-attachment.
    # Workspace IDs are directory names in the sandbox base directory.
    base = Path(_RUNTIME.base_dir)
    probe = base / (workspace_id or "")
    if workspace_id and probe.is_dir():
        # Recover best-effort metadata from name: sbx_{agent}_{job}_...
        agent_name = "recovered"
        job_id = "recovered"
        parts = workspace_id.split("_")
        if len(parts) >= 4 and parts[0] == "sbx":
            agent_name = parts[1]
            job_id = parts[2]

        ws = SandboxWorkspace(
            workspace_id=workspace_id,
            path=str(probe.resolve()),
            job_id=job_id,
            agent_name=agent_name,
        )
        _WORKSPACES[workspace_id] = ws
        logger.info("Re-attached to sandbox workspace from disk: %s", workspace_id)
        return ws

    # Search in-memory workspaces by job_id — LLMs sometimes pass the job_id they
    # provided to sandbox_create instead of the returned workspace_id.
    if workspace_id:
        for existing_ws in list(_WORKSPACES.values()):
            if existing_ws.job_id == workspace_id:
                logger.info(
                    "Resolved workspace by job_id=%s -> workspace_id=%s",
                    workspace_id,
                    existing_ws.workspace_id,
                )
                return existing_ws

        # Scan the filesystem for a directory whose name embeds the job_id.
        # sandbox_create uses prefix=f"sbx_{agent}_{job_id}_", so the directory
        # name is sbx_{agent}_{job_id}_{random}.  Matching _{job_id}_ handles
        # job_ids that contain underscores (e.g. "rca_pack_001").
        pattern = f"_{workspace_id}_"
        try:
            for entry in base.iterdir():
                if not entry.is_dir():
                    continue
                if pattern not in entry.name:
                    continue
                parts = entry.name.split("_")
                agent_name = parts[1] if len(parts) >= 2 else "recovered"
                recovered = SandboxWorkspace(
                    workspace_id=entry.name,
                    path=str(entry.resolve()),
                    job_id=workspace_id,
                    agent_name=agent_name,
                )
                _WORKSPACES[entry.name] = recovered
                logger.info(
                    "Re-attached sandbox workspace by job_id=%s: %s",
                    workspace_id,
                    entry.name,
                )
                return recovered
        except Exception:
            logger.exception("Filesystem scan for workspace by job_id failed: %s", workspace_id)

    raise ValueError(f"Unknown sandbox workspace: {workspace_id}")


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
    try:
        ws = _get_workspace(workspace_id)
        _note_workspace_for_current_task(ws.workspace_id)
        return ws
    except ValueError:
        pass

    effective_job = (fallback_job_id or workspace_id or "").strip() or str(uuid.uuid4())
    ws = _RUNTIME.create_workspace(job_id=effective_job, agent_name=agent_name)
    _WORKSPACES[ws.workspace_id] = ws
    _note_workspace_for_current_task(ws.workspace_id)
    return ws


@tool
def sandbox_create(job_id: str = "", agent_name: str = "subagent") -> dict[str, Any]:
    """Create a sandbox workspace for a subagent task."""
    effective_job = job_id.strip() or str(uuid.uuid4())
    ws = _RUNTIME.create_workspace(job_id=effective_job, agent_name=agent_name)
    _WORKSPACES[ws.workspace_id] = ws
    _note_workspace_for_current_task(ws.workspace_id)
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
    """Copy skill files from the shared volume into the sandbox workspace.

    Tenant-uploaded skills live under ``<SKILLS_ROOT>/<tenant_id>/<skill_name>/`` on the Docker volume.
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
        # Do not raise: tool exceptions abort the whole LangGraph run.
        # Return a structured error so the agent can recover (e.g. use sandbox_write_and_run directly).
        return {
            "source": None,
            "dest": None,
            "copied_files": [],
            "error": "missing_required_params",
            "message": "tenant_id and skill_name must be non-empty",
            "hint": (
                "Call sandbox_copy_mounted_skill with non-empty tenant_id/skill_name, or skip "
                "skill materialization and use sandbox_write_and_run to operate directly on input asset URLs."
            ),
        }

    # Tier 1: tenant volume search (explicit root / SKILLS_ROOT_DIR / /app/skills)
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
    dest_preview = str((Path(ws.path).resolve() / (dest_subdir or "skill").strip("/")).resolve())
    return {
        "source": None,
        "dest": dest_preview,
        "copied_files": [],
        "error": "skill_not_on_volume",
        "message": (
            f"No volume directory {tenant!r}/{skill!r} under skills roots [{tried_vol}]. "
            "Skill may not be synced to the shared volume."
        ),
        "hint": "Use read_skill_document + sandbox_write_files to materialize the skill.",
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

    from app.database import (
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
        # Best-effort: remove from any task buckets.
        try:
            wid = str(workspace_id or "").strip()
            if wid:
                with _ws_lock:
                    for tid, bucket in list(_WORKSPACE_IDS_BY_TASK.items()):
                        if wid in bucket:
                            bucket.discard(wid)
                        if not bucket:
                            _WORKSPACE_IDS_BY_TASK.pop(tid, None)
        except Exception:
            pass
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


def collect_task_open_workspace_paths_for_globs(task_id: str, globs: list[str]) -> list[str]:
    """Like collect_open_workspace_paths_for_globs, but restricted to workspaces used by this task."""
    tid = str(task_id or "").strip()
    if not tid:
        return []
    if not globs:
        return []
    workspace_ids = take_task_workspace_ids(tid)
    if not workspace_ids:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for wid in sorted(workspace_ids):
        ws = _WORKSPACES.get(wid)
        if ws is None:
            continue
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
                logger.exception("collect_task_open_workspace_paths_for_globs failed | task_id=%s glob=%s", tid, pat)
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

