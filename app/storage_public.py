"""Upload bytes to the public Supabase Storage bucket (default ``files``) and return public URLs."""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def public_files_bucket_name() -> str:
    return (os.environ.get("SUPABASE_PUBLIC_FILES_BUCKET") or "files").strip() or "files"


def is_public_storage_upload_enabled() -> bool:
    raw = (os.environ.get("DISABLE_SUPABASE_STORAGE_UPLOAD") or "").strip().lower()
    return raw not in ("1", "true", "yes")


def _sanitize_segment(s: str, *, max_len: int = 96) -> str:
    t = (s or "").strip().replace("/", "_").replace("\\", "_")
    t = re.sub(r"[^a-zA-Z0-9._-]+", "_", t).strip("._-") or "x"
    return t[:max_len]


def build_deliverable_object_path(
    *,
    filename: str,
    tenant_id: str | None,
    task_id: str | None,
    proc_inst_id: str | None,
) -> str:
    """Stable, unique object key: ``{tenant}/{proc}/{task}/{uuid}_{basename}``."""
    base = os.path.basename((filename or "").strip()) or "artifact.bin"
    safe_name = _sanitize_segment(base, max_len=180)
    uid = uuid.uuid4().hex[:16]
    t = _sanitize_segment(tenant_id or "no-tenant", max_len=64)
    tid = _sanitize_segment(task_id or "no-task", max_len=64)
    pid = _sanitize_segment(proc_inst_id or "no-proc", max_len=64)
    return f"{t}/{pid}/{tid}/{uid}_{safe_name}"


def upload_bytes_to_public_files_bucket(
    *,
    object_path: str,
    data: bytes,
    content_type: str | None = None,
) -> str:
    """Upload object and return its **public** URL. Raises on API failure."""
    from app.database import get_supabase

    bucket = public_files_bucket_name()
    sb = get_supabase()
    opts: dict[str, Any] = {"upsert": "true"}
    if content_type:
        opts["content-type"] = content_type
    else:
        opts["content-type"] = "application/octet-stream"
    sb.storage.from_(bucket).upload(
        object_path,
        data,
        file_options=opts,
    )
    url = sb.storage.from_(bucket).get_public_url(object_path)
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("storage get_public_url returned empty")
    return url.strip()


def try_upload_bytes_to_public_files(
    *,
    object_path: str,
    data: bytes,
    content_type: str | None = None,
) -> str | None:
    """Best-effort upload; logs and returns ``None`` on failure."""
    if not is_public_storage_upload_enabled():
        return None
    try:
        return upload_bytes_to_public_files_bucket(
            object_path=object_path,
            data=data,
            content_type=content_type,
        )
    except Exception:
        logger.exception(
            "Supabase public storage upload failed | bucket=%s path=%s",
            public_files_bucket_name(),
            object_path,
        )
        return None
