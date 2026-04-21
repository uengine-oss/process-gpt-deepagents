"""UI-compatible ask_user event transform + idempotency cache.

This module is intentionally dependency-light so it can be unit-tested without
FastAPI/LangChain runtime packages installed.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_ASK_USER_TTL_SECONDS = 60 * 60
_ASK_USER_CACHE_LOCK = threading.Lock()
# question_id -> {"ts": float, "conversation_id": str, "run_state": dict|None, "evt": dict}
_ASK_USER_CACHE: dict[str, dict[str, Any]] = {}


def _now_ts() -> float:
    return time.time()


def _prune_ask_user_cache(now_ts: float | None = None) -> None:
    now = _now_ts() if now_ts is None else float(now_ts)
    cutoff = now - _ASK_USER_TTL_SECONDS
    with _ASK_USER_CACHE_LOCK:
        dead = [qid for qid, v in _ASK_USER_CACHE.items() if float(v.get("ts") or 0.0) < cutoff]
        for qid in dead:
            _ASK_USER_CACHE.pop(qid, None)


def _resolve_question_id(*, run_state: dict[str, Any] | None) -> str:
    rs = run_state if isinstance(run_state, dict) else {}
    for k in ("correlation_id", "checkpoint_thread_id"):
        v = rs.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(uuid.uuid4())


def _resolve_question_text(*, evt: dict[str, Any], run_state: dict[str, Any] | None) -> str:
    q0 = evt.get("question")
    if isinstance(q0, str) and q0.strip():
        return q0
    rs = run_state if isinstance(run_state, dict) else {}
    q1 = rs.get("last_question")
    if isinstance(q1, str) and q1.strip():
        return q1
    return ""


def _resolve_context_text(*, run_state: dict[str, Any] | None) -> str:
    rs = run_state if isinstance(run_state, dict) else {}
    v = rs.get("reason")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return "사용자 입력이 필요합니다."


def _coerce_item_list(raw: Any) -> list[dict[str, Any]]:
    """Normalize various item/choice payloads into [{id,label,description?}, ...]."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for x in raw:
        if isinstance(x, dict):
            _id = x.get("id") or x.get("value") or x.get("key") or x.get("name")
            _label = x.get("label") or x.get("title") or x.get("name") or x.get("text") or _id
            _desc = x.get("description") or x.get("desc") or x.get("detail")
            sid = str(_id or "").strip()
            slab = str(_label or "").strip()
            if not (sid and slab):
                continue
            item: dict[str, Any] = {"id": sid, "label": slab}
            if isinstance(_desc, str) and _desc.strip():
                item["description"] = _desc.strip()
            out.append(item)
        elif isinstance(x, str) and x.strip():
            s = x.strip()
            out.append({"id": s, "label": s})
    return out


def _coerce_suggestion_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
        elif isinstance(x, dict):
            lab = x.get("label") or x.get("text") or x.get("title") or x.get("name") or x.get("id")
            if isinstance(lab, str) and lab.strip():
                out.append(lab.strip())
    return list(dict.fromkeys(out))


def _choose_ask_user_type_and_payload(
    *,
    question: str,
    run_state: dict[str, Any] | None,
    evt: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    rs = run_state if isinstance(run_state, dict) else {}

    # Case A: select_items when structured list exists.
    for k in ("items", "documents", "choices", "options"):
        if k in evt:
            items = _coerce_item_list(evt.get(k))
            if items:
                return (
                    "select_items",
                    {
                        "items": items,
                        "allow_multiple": True,
                        "min_select": 1,
                        "allow_skip": False,
                    },
                )
        if k in rs:
            items = _coerce_item_list(rs.get(k))
            if items:
                return (
                    "select_items",
                    {
                        "items": items,
                        "allow_multiple": True,
                        "min_select": 1,
                        "allow_skip": False,
                    },
                )

    # Case B: suggestions when we can provide button/chip-like phrases.
    for k in ("suggestions", "choices", "options"):
        sugg = _coerce_suggestion_list(evt.get(k))
        if sugg:
            return ("suggestions", {"suggestions": sugg, "allow_skip": False})
        sugg2 = _coerce_suggestion_list(rs.get(k))
        if sugg2:
            return ("suggestions", {"suggestions": sugg2, "allow_skip": False})

    # Case C: default approve/reject with optional suggestions when it looks like approval.
    ql = " ".join((question or "").strip().split()).lower()
    looks_like_approval = any(k in ql for k in ("승인", "결재", "진행할까요", "허가", "검토", "approve", "reject"))
    payload: dict[str, Any] = {"allow_skip": False}
    if looks_like_approval:
        payload["suggestions"] = ["승인", "반려"]
    return ("approve_reject_with_edit", payload)


def transform_input_required_to_ask_user_event(evt: dict[str, Any]) -> dict[str, Any] | None:
    """Convert legacy `input_required` SSE event to UI-compatible `ask_user` event.

    Idempotency: if the derived question_id was already emitted (cached), returns None.
    """
    if not isinstance(evt, dict):
        return None
    if str(evt.get("type") or "").strip() != "input_required":
        return None

    conv_id = str(evt.get("conversation_id") or "").strip()
    run_state = evt.get("run_state") if isinstance(evt.get("run_state"), dict) else None

    question_id = _resolve_question_id(run_state=run_state)
    question = _resolve_question_text(evt=evt, run_state=run_state)
    context = _resolve_context_text(run_state=run_state)
    user_request_type, extra = _choose_ask_user_type_and_payload(question=question, run_state=run_state, evt=evt)

    ask_evt: dict[str, Any] = {
        "type": "ask_user",
        "conversation_id": conv_id,
        "user_request_type": user_request_type,
        "question_id": question_id,
        "question": question,
        "context": context,
        "run_state": run_state,
        **extra,
    }

    _prune_ask_user_cache()
    with _ASK_USER_CACHE_LOCK:
        if question_id in _ASK_USER_CACHE:
            return None
        _ASK_USER_CACHE[question_id] = {
            "ts": _now_ts(),
            "conversation_id": conv_id,
            "run_state": run_state,
            "evt": ask_evt,
        }
    return ask_evt


def resolve_cached_run_state_for_question_id(question_id: str) -> dict[str, Any] | None:
    qid = (question_id or "").strip()
    if not qid:
        return None
    _prune_ask_user_cache()
    with _ASK_USER_CACHE_LOCK:
        row = _ASK_USER_CACHE.get(qid)
        rs = row.get("run_state") if isinstance(row, dict) else None
        return rs if isinstance(rs, dict) else None

