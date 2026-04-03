"""Collect sandbox-generated files and emit A2A file artifacts for ProcessGPT clients.

Paths are recorded when sandbox_* tools return (written_files / artifacts) and optionally
by scanning still-open workspaces at task end (catches e.g. xlsx produced by a script).
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_paths_by_task: dict[str, set[str]] = {}

SANDBOX_PATH_TOOLS = frozenset(
    {
        "sandbox_write_files",
        "sandbox_write_files_and_run",
        "sandbox_write_and_run",
        "sandbox_read_artifacts",
    }
)


def sandbox_output_paths_begin_task(task_id: str) -> None:
    if not (task_id or "").strip():
        return
    with _lock:
        _paths_by_task[str(task_id)] = set()


def sandbox_output_paths_discard_task(task_id: str) -> None:
    if not (task_id or "").strip():
        return
    with _lock:
        _paths_by_task.pop(str(task_id), None)


def note_sandbox_tool_output_paths(task_id: str, tool_name: str, tool_output: Any, error: Exception | None) -> None:
    if error is not None or not (task_id or "").strip():
        return
    if not isinstance(tool_output, dict):
        return
    name = str(tool_name or "")
    if name not in SANDBOX_PATH_TOOLS:
        return
    paths: list[str] = []
    if name in ("sandbox_write_files", "sandbox_write_files_and_run", "sandbox_write_and_run"):
        wf = tool_output.get("written_files")
        if isinstance(wf, list):
            paths.extend(str(x) for x in wf if x)
    elif name == "sandbox_read_artifacts":
        arts = tool_output.get("artifacts")
        if isinstance(arts, list):
            paths.extend(str(x) for x in arts if x)
    if not paths:
        return
    tid = str(task_id)
    with _lock:
        bucket = _paths_by_task.setdefault(tid, set())
        for p in paths:
            if p.strip():
                bucket.add(p.strip())


def take_sandbox_output_paths_task(task_id: str) -> set[str]:
    if not (task_id or "").strip():
        return set()
    with _lock:
        return set(_paths_by_task.pop(str(task_id), set()) or set())


def _sandbox_base_resolved() -> Path:
    import tempfile

    root = os.environ.get("SANDBOX_BASE_DIR") or tempfile.gettempdir()
    return Path(root).resolve()


def _allowed_to_read(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    try:
        size = resolved.stat().st_size
    except OSError:
        return False
    max_b = int(os.environ.get("OUTPUT_FILE_MAX_BYTES") or 26_214_400)  # ~25 MiB
    if size > max_b:
        logger.info("Skipping oversized artifact | path=%s size=%s", resolved, size)
        return False
    base = _sandbox_base_resolved()
    # Restrict to configured sandbox root (same default as LocalSandboxRuntime).
    if base not in resolved.parents and resolved != base:
        if not str(resolved).startswith(str(base) + os.sep):
            logger.warning("Rejecting artifact path outside SANDBOX_BASE_DIR | path=%s base=%s", resolved, base)
            return False
    return True


def default_artifact_globs() -> list[str]:
    raw = (os.environ.get("OUTPUT_ARTIFACT_GLOBS") or "").strip()
    if raw:
        return [g.strip() for g in raw.split(",") if g.strip()]
    return ["**/*.xlsx", "**/*.xlsm", "**/*.xls", "**/*.csv", "**/*.pdf", "**/*.zip"]


def collect_readable_artifacts(
    paths: set[str],
    *,
    extra_paths: list[str] | None = None,
) -> list[tuple[str, bytes, str | None]]:
    """Return list of (filename, raw_bytes, mime_or_none)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for p in sorted(paths):
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    for p in extra_paths or []:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    max_n = int(os.environ.get("OUTPUT_FILE_MAX_COUNT") or 32)
    out: list[tuple[str, bytes, str | None]] = []
    for p in ordered[:max_n]:
        fp = Path(p)
        if not _allowed_to_read(fp):
            continue
        try:
            data = fp.read_bytes()
        except OSError:
            logger.exception("Failed to read artifact | path=%s", fp)
            continue
        mime, _ = mimetypes.guess_type(fp.name)
        out.append((fp.name, data, mime))
    return out


def enqueue_sandbox_file_artifacts(
    eq: Any,
    proc_inst_id: str,
    task_id: str,
    files: list[tuple[str, bytes, str | None]],
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Emit TaskArtifactUpdateEvent per file; return a JSON-safe manifest (no bytes).

    When Supabase public storage upload is enabled, each manifest entry includes
    ``public_url`` and ``storage_path`` for the ``files`` bucket (or
    :envvar:`SUPABASE_PUBLIC_FILES_BUCKET`).
    """
    if os.environ.get("DISABLE_SANDBOX_FILE_ARTIFACTS", "").strip().lower() in ("1", "true", "yes"):
        return []
    if not files:
        return []
    manifest: list[dict[str, Any]] = []
    try:
        from a2a.types import FilePart, FileWithBytes, Part, TaskArtifactUpdateEvent
        from a2a.utils import new_artifact
    except Exception:
        logger.exception("a2a types unavailable; cannot emit file artifacts")
        return []

    from app.storage_public import build_deliverable_object_path, try_upload_bytes_to_public_files

    for name, raw, mime in files:
        safe_name = name or "artifact.bin"
        storage_path: str | None = None
        public_url: str | None = None
        try:
            opath = build_deliverable_object_path(
                filename=safe_name,
                tenant_id=tenant_id,
                task_id=task_id or None,
                proc_inst_id=proc_inst_id or None,
            )
            public_url = try_upload_bytes_to_public_files(
                object_path=opath,
                data=raw,
                content_type=mime,
            )
            if public_url:
                storage_path = opath
                logger.info(
                    "Deliverable uploaded to public storage | name=%s path=%s",
                    safe_name,
                    opath,
                )
        except Exception:
            logger.exception("Unexpected error during storage upload | name=%s", safe_name)
        try:
            b64 = base64.standard_b64encode(raw).decode("ascii")
        except Exception:
            logger.exception("base64 encode failed | name=%s", safe_name)
            continue
        artifact = new_artifact(
            [
                Part(
                    root=FilePart(
                        file=FileWithBytes(bytes=b64, mime_type=mime, name=safe_name),
                    )
                )
            ],
            name=safe_name,
            description="Sandbox generated file",
        )
        try:
            eq.enqueue_event(
                TaskArtifactUpdateEvent(
                    artifact=artifact,
                    lastChunk=True,
                    contextId=proc_inst_id,
                    taskId=task_id,
                )
            )
        except Exception:
            logger.exception("Failed to enqueue file TaskArtifactUpdateEvent | name=%s", safe_name)
            continue
        entry: dict[str, Any] = {
            "name": safe_name,
            "size_bytes": len(raw),
            "mime_type": mime,
        }
        if public_url:
            entry["public_url"] = public_url
        if storage_path:
            entry["storage_path"] = storage_path
        manifest.append(entry)
    return manifest
