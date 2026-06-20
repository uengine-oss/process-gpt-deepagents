from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_client = None
_storage_client = None


def get_supabase():
    """DB 작업용 클라이언트 — SERVICE_ROLE_KEY 우선 (RLS 우회 허용, 내부 인프라 전용)."""
    global _client
    if _client is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_KEY_URL")
        key = (
            os.environ.get("SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required")
        _client = create_client(url, key)
    return _client


def get_storage_supabase():
    """Storage 도구용 클라이언트 — anon 키만 사용 (RLS 적용, SERVICE_ROLE_KEY 제외)."""
    global _storage_client
    if _storage_client is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL") or os.environ.get("SUPABASE_KEY_URL")
        # SUPABASE_ANON_KEY만 사용 — SERVICE_ROLE_KEY·SUPABASE_KEY는 의도적으로 제외
        key = os.environ.get("SUPABASE_ANON_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_ANON_KEY are required for storage access. "
                "에이전트 스토리지 도구는 anon 키만 허용됩니다."
            )
        _storage_client = create_client(url, key)
    return _storage_client


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_normalize(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return obj
