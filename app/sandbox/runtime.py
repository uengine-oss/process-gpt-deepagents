"""Sandbox runtime abstraction for subagent shell/file execution."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SandboxWorkspace:
    """Materialized workspace metadata for a job."""

    workspace_id: str
    path: str
    job_id: str
    agent_name: str


@dataclass
class SandboxCommandResult:
    """Result payload from sandbox command execution."""

    exit_code: int
    stdout: str
    stderr: str
    command: str


class LocalSandboxRuntime:
    """Filesystem-backed sandbox runtime.

    This implementation works in local/dev and Kubernetes pod filesystem.
    It is intentionally pluggable so a containerized runtime can replace it.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        root = base_dir or os.environ.get("SANDBOX_BASE_DIR") or tempfile.gettempdir()
        self.base_dir = str(Path(root).resolve())
        os.makedirs(self.base_dir, exist_ok=True)

    def create_workspace(self, job_id: str, agent_name: str) -> SandboxWorkspace:
        safe_agent = agent_name.replace("/", "_").replace(" ", "_")
        os.makedirs(self.base_dir, exist_ok=True)
        ws_path = tempfile.mkdtemp(prefix=f"sbx_{safe_agent}_{job_id}_", dir=self.base_dir)
        ws_id = Path(ws_path).name
        return SandboxWorkspace(
            workspace_id=ws_id,
            path=ws_path,
            job_id=job_id,
            agent_name=agent_name,
        )

    def write_files(self, workspace: SandboxWorkspace, files: list[dict[str, Any]]) -> list[str]:
        written: list[str] = []
        for item in files:
            rel_path = str(item.get("path") or "").strip()
            if not rel_path:
                continue
            full_path = (Path(workspace.path) / rel_path).resolve()
            workspace_root = Path(workspace.path).resolve()
            if workspace_root not in full_path.parents and full_path != workspace_root:
                raise ValueError(f"Path escapes workspace: {rel_path}")
            full_path.parent.mkdir(parents=True, exist_ok=True)
            content = str(item.get("content") or "")
            full_path.write_text(content, encoding="utf-8")
            written.append(str(full_path))
        return written

    def run_shell(
        self,
        workspace: SandboxWorkspace,
        command: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> SandboxCommandResult:
        merged_env = None
        if env:
            merged_env = dict(os.environ)
            # Force string values; ignore None.
            for k, v in env.items():
                kk = str(k)
                if v is None:
                    continue
                merged_env[kk] = str(v)
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workspace.path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
        )
        return SandboxCommandResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            command=command,
        )

    def collect_artifacts(self, workspace: SandboxWorkspace, patterns: list[str]) -> list[str]:
        root = Path(workspace.path)
        collected: list[str] = []
        for pattern in patterns:
            for p in root.glob(pattern):
                if p.is_file():
                    collected.append(str(p.resolve()))
        return sorted(set(collected))

    def cleanup(self, workspace: SandboxWorkspace, keep_on_failure: bool = False) -> None:
        if keep_on_failure:
            return
        shutil.rmtree(workspace.path, ignore_errors=True)

    def copy_mounted_skill_tree(
        self,
        workspace: SandboxWorkspace,
        *,
        skills_root: str,
        tenant_id: str,
        skill_rel_path: str,
        dest_subdir: str = "skill",
    ) -> dict[str, Any]:
        """Copy a tenant skill directory from the host mount into the sandbox workspace.

        Validates that the source resolves under ``skills_root/<tenant_id>/`` to avoid
        path escape. Returns a list of copied files (workspace-relative POSIX paths).
        """
        root = Path(skills_root).resolve()
        tenant = tenant_id.strip().strip("/")
        if not tenant:
            raise ValueError("tenant_id is required")
        tenant_root = (root / tenant).resolve()
        rel = Path(*Path(skill_rel_path.strip()).parts) if skill_rel_path.strip() else Path(".")
        if ".." in rel.parts:
            raise ValueError("Invalid skill_rel_path")
        source = (tenant_root / rel).resolve()
        try:
            source.relative_to(tenant_root)
        except ValueError as e:
            raise ValueError("skill path escapes tenant directory") from e
        if not source.exists():
            return {"source": str(source), "copied_files": [], "error": "source_missing"}
        if not source.is_dir():
            return {"source": str(source), "copied_files": [], "error": "source_not_dir"}

        dest_root = (Path(workspace.path) / dest_subdir.strip("/")).resolve()
        workspace_root = Path(workspace.path).resolve()
        if workspace_root not in dest_root.parents and dest_root != workspace_root:
            raise ValueError("dest_subdir escapes workspace")
        dest_root.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        for src_file in source.rglob("*"):
            if not src_file.is_file():
                continue
            rel_to_skill = src_file.relative_to(source)
            dst = dest_root / rel_to_skill
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)
            rel_ws = dst.relative_to(workspace_root)
            copied.append(rel_ws.as_posix())
        return {"source": str(source), "dest": str(dest_root), "copied_files": sorted(copied)}

