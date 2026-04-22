"""HITL (human-in-the-loop) event helpers shared across protocols.

This module intentionally contains **no** ask_user-specific logic. It provides:
- Stable HITL question IDs (for dedupe + resume matching)
- A short-lived in-memory cache for mapping question_id -> run_state
- Helpers to extract question/context text from legacy payloads
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_HITL_TTL_SECONDS = 60 * 60
_HITL_CACHE_LOCK = threading.Lock()
# question_id -> {"ts": float, "conversation_id": str, "run_state": dict|None}
_HITL_CACHE: dict[str, dict[str, Any]] = {}


def _now_ts() -> float:
    return time.time()


def _prune_hitl_cache(now_ts: float | None = None) -> None:
    now = _now_ts() if now_ts is None else float(now_ts)
    cutoff = now - _HITL_TTL_SECONDS
    with _HITL_CACHE_LOCK:
        dead = [qid for qid, v in _HITL_CACHE.items() if float(v.get("ts") or 0.0) < cutoff]
        for qid in dead:
            _HITL_CACHE.pop(qid, None)


def question_id_from_run_state(run_state: dict[str, Any] | None) -> str:
    """Stable id for HITL dedupe and resume matching."""
    rs = run_state if isinstance(run_state, dict) else {}
    for k in ("correlation_id", "checkpoint_thread_id"):
        v = rs.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(uuid.uuid4())


def resolve_question_text(*, evt: dict[str, Any], run_state: dict[str, Any] | None) -> str:
    q0 = evt.get("question")
    if isinstance(q0, str) and q0.strip():
        return q0
    rs = run_state if isinstance(run_state, dict) else {}
    q1 = rs.get("last_question")
    if isinstance(q1, str) and q1.strip():
        return q1
    return ""


def resolve_context_text(*, run_state: dict[str, Any] | None) -> str:
    rs = run_state if isinstance(run_state, dict) else {}
    v = rs.get("reason")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return "사용자 입력이 필요합니다."


def register_run_state_for_resume(
    *,
    question_id: str,
    conversation_id: str,
    run_state: dict[str, Any] | None,
) -> bool:
    """Cache run_state for later resume matching.

    Returns True if newly registered; False if question_id was already cached.
    """
    qid = (question_id or "").strip()
    cid = (conversation_id or "").strip()
    if not qid:
        return False
    _prune_hitl_cache()
    with _HITL_CACHE_LOCK:
        if qid in _HITL_CACHE:
            return False
        _HITL_CACHE[qid] = {
            "ts": _now_ts(),
            "conversation_id": cid,
            "run_state": run_state if isinstance(run_state, dict) else None,
        }
    return True


def resolve_cached_run_state_for_question_id(question_id: str) -> dict[str, Any] | None:
    qid = (question_id or "").strip()
    if not qid:
        return None
    _prune_hitl_cache()
    with _HITL_CACHE_LOCK:
        row = _HITL_CACHE.get(qid)
        rs = row.get("run_state") if isinstance(row, dict) else None
        return rs if isinstance(rs, dict) else None

