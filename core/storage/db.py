from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_client = None


def get_supabase():
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_normalize(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return obj
