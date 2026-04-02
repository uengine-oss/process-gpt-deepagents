"""LangChain tool wrappers around the sandbox runtime."""

from __future__ import annotations

import json
import os
import shlex
import uuid
from typing import Any

try:
    from langchain.tools import tool
except Exception:  # pragma: no cover - test fallback without langchain installed
    def tool(func=None, **_kwargs):  # type: ignore
        if func is None:
            return lambda f: f
        return func

from pathlib import Path

from sandbox_runtime import LocalSandboxRuntime, SandboxWorkspace

_RUNTIME = LocalSandboxRuntime()
_WORKSPACES: dict[str, SandboxWorkspace] = {}


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
    """Copy files from the mounted skills volume into this sandbox (host → workspace).

    Use this so scripts can run with real skill content when filesystem tools miss files.
    ``skill_name`` is the folder under ``<SKILLS_ROOT>/<tenant_id>/`` (e.g. ``google-sheets``).
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
    if not root:
        tried = ", ".join(uniq) if uniq else "(none)"
        raise FileNotFoundError(
            "Mounted skill volume is not available for this run. "
            f"Skill path not found for tenant_id={tenant!r} skill_name={skill!r}. "
            f"Tried skills_root candidates: {tried}. "
            "Fix by mounting the skills volume and/or setting SKILLS_ROOT_DIR correctly."
        )
    return _RUNTIME.copy_mounted_skill_tree(
        ws,
        skills_root=root,
        tenant_id=tenant_id,
        skill_rel_path=skill_name,
        dest_subdir=dest_subdir or "skill",
    )


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
def sandbox_cleanup(workspace_id: str, keep_on_failure: bool = False) -> dict[str, Any]:
    """Cleanup sandbox workspace."""
    ws = _get_workspace(workspace_id)
    _RUNTIME.cleanup(ws, keep_on_failure=keep_on_failure)
    if not keep_on_failure:
        _WORKSPACES.pop(workspace_id, None)
    return {"workspace_id": workspace_id, "cleaned": not keep_on_failure}


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
        sandbox_cleanup,
    ]

