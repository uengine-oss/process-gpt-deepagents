"""core/storage/storage.py — Supabase 'files' 버킷 storage 헬퍼 툴.

Bucket layout:
    files/
    └── skills/
        └── {tenant_id}/
            └── {task_id}/
                └── {filename or relative/path}

보안 원칙:
  - 에이전트에 노출되는 @tool 함수(download_file, get_download_url)는 anon 키
    (get_storage_supabase)만 사용한다 — 에이전트가 임의 storage_path를 골라 호출할 수
    있으므로 RLS가 방어선 역할을 한다.
  - 오케스트레이터 전용 워크스페이스 동기화 함수(upload_artifact/upload_workspace_*/
    download_workspace_directory)는 service-role 키(get_supabase)를 쓴다 — 에이전트가
    직접 호출하지 못하고 tenant_id는 항상 executor.py가 넘긴 현재 요청의 tenant_id로
    고정되므로, tenant 격리는 RLS 대신 _storage_path()의 tenant_id prefix로 코드 레벨에서
    보장한다. `files` 버킷이 public이고 anon 역할에 대한 INSERT/UPDATE RLS 정책이 없어서
    anon 키로는 업로드가 막히기 때문에 이렇게 나눴다.
  - 업로드 source 경로와 다운로드 dest_dir는 반드시 현재 테넌트 작업공간
    (WORKSPACE_ROOT/<tenant_id>) 하위여야 한다.
  - 모든 storage 경로에는 tenant_id가 prefix로 강제된다.

Required environment variables:
    SUPABASE_URL       — project URL (e.g. https://xxxx.supabase.co)
    SUPABASE_ANON_KEY  — anon (public) key; used by agent-facing @tool functions only
    SERVICE_ROLE_KEY   — used by orchestrator-only workspace sync functions
"""

from __future__ import annotations

import base64
import contextvars
import mimetypes
import os
import re
from pathlib import Path

from langchain_core.tools import tool

from ..sandbox.tenant_workspace import tenant_workspace
from .db import get_storage_supabase, get_supabase

_BUCKET = "files"
_PREFIX = "skills"

WORKSPACE_ROOT = Path(
    os.environ.get("WORKSPACE", str(Path(__file__).parent.parent.parent / "workspace"))
).resolve()

# agent.py에서 build_agent() 호출 시 set()
_current_tenant_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "storage_tenant_id", default=""
)


def set_tenant_id(tenant_id: str) -> None:
    _current_tenant_id.set(tenant_id)


def _require_tenant_id() -> str:
    tid = _current_tenant_id.get()
    if not tid:
        raise RuntimeError(
            "tenant_id가 설정되지 않았습니다. storage 도구는 tenant 컨텍스트 없이 사용할 수 없습니다."
        )
    return tid


# Supabase Storage 가 키로 받는 글자. 한글 등 나머지가 섞이면 InvalidKey 로 거부한다
# (storage-api 의 키 검사와 같은 집합).
_SAFE_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9_/!\-.*'() &$@=;:+,?]*$")
# 인코딩한 경로 조각의 표식. 뒤는 UTF-8 이름의 base64url(패딩 없음)이다.
_ENCODED_MARK = "__u8__"


def encode_key_segment(segment: str) -> str:
    """키로 쓸 수 없는 글자가 있는 경로 조각을 되돌릴 수 있게 바꾼다.

    그대로 두면 '견적서_한빛상사.docx' 같은 산출물이 업로드에서 빠져, 다른 파드나
    인계받은 방에서 그 파일을 찾지 못한다.
    """
    if _SAFE_KEY_SEGMENT.match(segment) and not segment.startswith(_ENCODED_MARK):
        return segment
    raw = base64.urlsafe_b64encode(segment.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{_ENCODED_MARK}{raw}"


def decode_key_segment(segment: str) -> str:
    if not segment.startswith(_ENCODED_MARK):
        return segment
    raw = segment[len(_ENCODED_MARK):]
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return segment


def encode_key_path(rel: str) -> str:
    return "/".join(encode_key_segment(p) for p in str(rel).replace("\\", "/").split("/"))


def decode_key_path(rel: str) -> str:
    return "/".join(decode_key_segment(p) for p in str(rel).split("/"))


def _storage_path(tenant_id: str, task_id: str, rel: str) -> str:
    """bucket 내 오브젝트 키: skills/{tenant_id}/{task_id}/{rel}. rel 의 한글 등은 인코딩한다."""
    return f"{_PREFIX}/{tenant_id}/{task_id}/{encode_key_path(rel)}"


def _validate_workspace_path(p: Path, label: str) -> None:
    """경로가 현재 테넌트 작업공간 하위인지 확인 — 아니면 ValueError.

    볼륨 루트 기준으로만 보면 호스트에서 도는 이 도구로 다른 테넌트 디렉터리를 읽고 쓸 수 있다.
    """
    root = tenant_workspace(WORKSPACE_ROOT, _require_tenant_id()).resolve()
    try:
        p.resolve().relative_to(root)
    except ValueError:
        raise ValueError(
            f"{label} '{p}' 는 현재 테넌트 작업공간({root}) 하위여야 합니다. "
            "보안상 workspace 외부 경로는 허용되지 않습니다."
        )


@tool
def download_file(storage_path: str, dest_dir: str) -> str:
    """Download a single file from the 'files' Supabase bucket to a local directory.

    Args:
        storage_path: Path within the 'files' bucket (e.g. 'knowledge/doc.docx').
        dest_dir:     Absolute local directory path to save the file into.
                      Must be under the workspace root.

    Returns:
        Absolute local path to the downloaded file, or an error message.
    """
    try:
        dest = Path(dest_dir).expanduser().resolve()
        _validate_workspace_path(dest, "dest_dir")
        dest.mkdir(parents=True, exist_ok=True)
        file_name = Path(storage_path).name
        local_path = dest / file_name
        data = get_storage_supabase().storage.from_(_BUCKET).download(storage_path)
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
        tenant_id = _require_tenant_id()
        storage_path = _storage_path(tenant_id, task_id, remote_name)
        return get_storage_supabase().storage.from_(_BUCKET).get_public_url(storage_path)
    except Exception as exc:
        return f"ERROR: {exc}"


def upload_artifact(task_id: str, file_path: str | Path, file_data: bytes, remote_name: str | None = None) -> str:
    """Upload a single artifact to Supabase storage under skills/{tenant_id}/{task_id}/.

    Called by orchestrator code after agents finish — NOT exposed as an agent tool.

    Args:
        task_id:     Unique identifier for the current task or skill iteration.
        file_path:   Original file path (used for mime-type detection and default name).
        file_data:   File contents as bytes.
        remote_name: Override the remote filename. Defaults to the original filename.

    Returns:
        The public download URL on success, or an error message.
    """
    try:
        tenant_id = _require_tenant_id()
        path = Path(file_path)
        dest_name = remote_name or path.name
        storage_path = _storage_path(tenant_id, task_id, dest_name)
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        client = get_supabase()
        client.storage.from_(_BUCKET).upload(
            path=storage_path,
            file=file_data,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
        return client.storage.from_(_BUCKET).get_public_url(storage_path)
    except Exception as exc:
        return f"ERROR: {exc}"


def upload_workspace_file(task_id: str, file_path: str | Path, remote_name: str | None = None) -> str:
    """Read a workspace file and upload it to Supabase storage.

    Called by orchestrator code after agents finish — NOT exposed as an agent tool.

    Args:
        task_id:     Unique identifier for the current task or skill iteration.
        file_path:   Absolute path to the local file (must be under workspace root).
        remote_name: Override the remote filename. Defaults to the local filename.

    Returns:
        The public download URL on success, or an error message.
    """
    try:
        path = Path(file_path).expanduser().resolve()
        _validate_workspace_path(path, "file_path")
        if not path.exists():
            return f"ERROR: file not found: {file_path}"
        return upload_artifact(task_id, path, path.read_bytes(), remote_name)
    except Exception as exc:
        return f"ERROR: {exc}"


def upload_workspace_directory(task_id: str, dir_path: str | Path, pattern: str = "**/*") -> str:
    """Upload all matching files from a workspace directory to Supabase storage.

    Called by orchestrator code after agents finish — NOT exposed as an agent tool.

    Args:
        task_id:  Unique identifier for the current task or skill iteration.
        dir_path: Absolute path to the local directory (must be under workspace root).
        pattern:  Glob pattern relative to dir_path (default: all files recursively).

    Returns:
        A summary listing uploaded storage paths and any per-file errors.
    """
    try:
        tenant_id = _require_tenant_id()
        base = Path(dir_path).expanduser().resolve()
        _validate_workspace_path(base, "dir_path")
        if not base.is_dir():
            return f"ERROR: not a directory: {dir_path}"

        uploaded: list[str] = []
        errors: list[str] = []

        for local_file in sorted(base.glob(pattern)):
            if not local_file.is_file():
                continue
            rel = local_file.relative_to(base)
            try:
                url = upload_artifact(task_id, local_file, local_file.read_bytes(), str(rel))
                if url.startswith("ERROR:"):
                    errors.append(f"{rel}: {url}")
                else:
                    uploaded.append(_storage_path(tenant_id, task_id, str(rel)))
            except Exception as exc:
                errors.append(f"{rel}: {exc}")

        lines = [f"Uploaded {len(uploaded)} file(s) to {_PREFIX}/{tenant_id}/{task_id}/"]
        lines.extend(f"  ok  {p}" for p in uploaded)
        if errors:
            lines.append(f"Errors ({len(errors)}):")
            lines.extend(f"  err {e}" for e in errors)
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def _list_all_objects(client, prefix: str) -> list[str]:
    """prefix 하위 모든 파일의 버킷 키를 재귀적으로 나열한다.

    Supabase Storage의 .list()는 비재귀적이라, 하위 폴더 항목(id가 None)을 만나면
    직접 재귀 호출해서 내려간다.
    """
    results: list[str] = []
    entries = client.storage.from_(_BUCKET).list(prefix) or []
    for entry in entries:
        name = entry.get("name")
        if not name:
            continue
        full_path = f"{prefix}/{name}" if prefix else name
        if entry.get("id") is None:
            results.extend(_list_all_objects(client, full_path))
        else:
            results.append(full_path)
    return results


def download_workspace_directory(task_id: str, dest_dir: str | Path) -> str:
    """Supabase storage의 skills/{tenant_id}/{task_id}/ 하위 전체를 dest_dir로 내려받는다.

    Called by orchestrator code before an agent turn starts(하이드레이션) — NOT exposed
    as an agent tool. upload_workspace_directory()의 대칭 함수.

    Args:
        task_id:  업로드 시 쓰인 것과 동일한 task_id(예: chat_room_id).
        dest_dir: 로컬로 복원할 절대 경로(workspace root 하위여야 함).

    Returns:
        내려받은/실패한 파일 목록 요약 문자열.
    """
    try:
        tenant_id = _require_tenant_id()
        dest = Path(dest_dir).expanduser().resolve()
        _validate_workspace_path(dest, "dest_dir")
        dest.mkdir(parents=True, exist_ok=True)

        prefix = f"{_PREFIX}/{tenant_id}/{task_id}"
        client = get_supabase()
        keys = _list_all_objects(client, prefix)

        downloaded: list[str] = []
        errors: list[str] = []
        for key in keys:
            rel = decode_key_path(key[len(prefix):].lstrip("/"))
            if not rel:
                continue
            local_path = dest / rel
            try:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                data = client.storage.from_(_BUCKET).download(key)
                local_path.write_bytes(data)
                downloaded.append(rel)
            except Exception as exc:
                errors.append(f"{rel}: {exc}")

        lines = [f"Downloaded {len(downloaded)} file(s) from {prefix}/"]
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
    client = get_storage_supabase()
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
