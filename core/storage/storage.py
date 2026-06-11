"""core/storage/storage.py — Supabase 'files' 버킷 storage 헬퍼 툴.

Bucket layout:
    files/
    └── skills/
        └── {task_id}/
            └── {filename or relative/path}

Required environment variables:
    SUPABASE_URL  — project URL (e.g. https://xxxx.supabase.co)
    SUPABASE_KEY  — service-role or anon key
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from langchain_core.tools import tool

from .db import get_supabase

_BUCKET = "files"
_PREFIX = "skills"


@tool
def download_file(storage_path: str, dest_dir: str) -> str:
    """Download a single file from the 'files' Supabase bucket to a local directory.

    Args:
        storage_path: Path within the 'files' bucket (e.g. 'knowledge/doc.docx').
        dest_dir:     Absolute local directory path to save the file into.

    Returns:
        Absolute local path to the downloaded file, or an error message.
    """
    try:
        dest = Path(dest_dir).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        file_name = Path(storage_path).name
        local_path = dest / file_name
        data = get_supabase().storage.from_(_BUCKET).download(storage_path)
        local_path.write_bytes(data)
        return str(local_path)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def get_download_url(task_id: str, remote_name: str) -> str:
    """Get the public download URL for an already-uploaded artifact.

    Args:
        task_id:     Task identifier used when the file was uploaded.
        remote_name: The remote filename (just the name, not the full path).

    Returns:
        A public download URL, or an error message.
    """
    try:
        storage_path = f"{_PREFIX}/{task_id}/{remote_name}"
        return get_supabase().storage.from_(_BUCKET).get_public_url(storage_path)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def upload_artifact(task_id: str, file_path: str, remote_name: str | None = None) -> str:
    """Upload a single artifact file to Supabase storage under skills/{task_id}/.

    Args:
        task_id:     Unique identifier for the current task or skill iteration.
        file_path:   Absolute path to the local file to upload.
        remote_name: Override the remote filename. Defaults to the local filename.

    Returns:
        The storage path and public download URL on success, or an error message.
    """
    try:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return f"ERROR: file not found: {file_path}"

        dest_name = remote_name or path.name
        storage_path = f"{_PREFIX}/{task_id}/{dest_name}"
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        client = get_supabase()
        with path.open("rb") as fh:
            data = fh.read()
        client.storage.from_(_BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
        download_url = client.storage.from_(_BUCKET).get_public_url(storage_path)
        return f"Uploaded: {storage_path}\nDownload URL: [다운로드]({download_url})"
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def upload_directory(task_id: str, dir_path: str, pattern: str = "**/*") -> str:
    """Upload all matching files from a local directory to Supabase storage.

    Each file is stored at skills/{task_id}/{relative_path_within_dir}.

    Args:
        task_id:  Unique identifier for the current task or skill iteration.
        dir_path: Absolute path to the local directory to upload.
        pattern:  Glob pattern relative to dir_path (default: all files recursively).

    Returns:
        A summary listing uploaded storage paths and any per-file errors.
    """
    try:
        base = Path(dir_path).expanduser().resolve()
        if not base.is_dir():
            return f"ERROR: not a directory: {dir_path}"

        client = get_supabase()
        uploaded: list[str] = []
        errors: list[str] = []

        for local_file in sorted(base.glob(pattern)):
            if not local_file.is_file():
                continue
            rel = local_file.relative_to(base)
            storage_path = f"{_PREFIX}/{task_id}/{rel}"
            mime_type, _ = mimetypes.guess_type(str(local_file))
            mime_type = mime_type or "application/octet-stream"
            try:
                with local_file.open("rb") as fh:
                    data = fh.read()
                client.storage.from_(_BUCKET).upload(
                    path=storage_path,
                    file=data,
                    file_options={"content-type": mime_type, "upsert": "true"},
                )
                uploaded.append(storage_path)
            except Exception as exc:
                errors.append(f"{rel}: {exc}")

        lines = [f"Uploaded {len(uploaded)} file(s) to {_PREFIX}/{task_id}/"]
        lines.extend(f"  ok  {p}" for p in uploaded)
        if errors:
            lines.append(f"Errors ({len(errors)}):")
            lines.extend(f"  err {e}" for e in errors)
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def download_knowledge_docs(knowledge_docs: list[dict], dest_dir: Path) -> list[str]:
    """Supabase storage에서 knowledge_docs를 로컬 dest_dir로 다운로드한다.

    각 doc의 `id` 필드를 버킷 내 파일 경로로 사용한다.
    Returns: 성공적으로 다운로드된 로컬 파일 경로 목록.
    """
    if not knowledge_docs:
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    client = get_supabase()
    local_paths: list[str] = []

    for doc in knowledge_docs:
        storage_path = doc.get("id", "")
        if not storage_path:
            continue
        file_name = doc.get("file_name") or Path(storage_path).name
        local_path = dest_dir / file_name
        try:
            data = client.storage.from_(_BUCKET).download(storage_path)
            local_path.write_bytes(data)
            local_paths.append(str(local_path))
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("failed to download %s: %s", storage_path, exc)

    return local_paths
