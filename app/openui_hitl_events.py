"""OpenUI Lang-based HITL events for HTTP chat streaming (SSE).

Wire contract (schema ``1.0``)
-------------------------------

Each SSE ``data`` line is one JSON object (existing /chat/stream rule).

Legacy ``input_required`` is expanded into a short sequence:

- ``type: openui`` + ``op: delta`` (repeated): incremental ``openui_lang_chunk`` text.
  - ``seq == 0`` frames may also include ``question``, ``context``, ``run_state``
    so the client can render chrome before Lang completes.
- ``type: openui`` + ``op: end`` (once): carries full ``openui_lang``, ``run_state``,
  ``question_id``, and ``lang_char_len`` for verification.

Common fields on every frame: ``type``, ``op``, ``schema_version``, ``conversation_id``, ``question_id``.

OpenUI Lang source (M1 vs default template)
-------------------------------------------

- **M1**: If ``run_state["openui_lang"]`` is a non-empty string, it is streamed
  verbatim (graph / tool produced UI definition).
- **Default (template)**: Otherwise a minimal OpenUI Lang snippet is built from
  ``question`` and ``context`` using ``Stack`` / ``TextContent`` identifiers from
  Thesys examples; the client library must define matching components or override
  via M1.

Resume metadata
---------------

Clients should send the same payload shape as ``ask_user_response`` under
``metadata.openui_hitl_response`` (it maps to ``human_response_answer`` in ``extras``).
"""

from __future__ import annotations

import re
from typing import Any

from app.hitl_events import (
    question_id_from_run_state,
    register_run_state_for_resume,
    resolve_context_text,
    resolve_question_text,
)

OPENUI_HITL_SCHEMA_VERSION = "1.0"
# Keep chunks small so SSE frames stay well below typical proxy limits.
_DEFAULT_LANG_CHUNK_SIZE = 512
_MAX_LANG_CHARS = 48_000


def _escape_openui_string(s: str) -> str:
    t = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\n", "\\n")
    return t


_FUNC_DSL_REQUIRED_ROOT_RE = re.compile(r"(?m)^\s*root\s*=\s*Column\(\)\s*$")
_FUNC_DSL_ALLOWED_CALL_RE = re.compile(
    r"\b(?:Column|RadioGroup|TextArea|TextInput|Button|Select|Checkbox|Row|Stack)\s*\(",
)
_FUNC_DSL_DISALLOWED_MARKERS_RE = re.compile(r"(?m)^\s*(component\s+\w+|\{|\})\s*$")


def _looks_like_function_dsl(openui_lang: str) -> bool:
    s = (openui_lang or "").strip()
    if not s:
        return False
    if _FUNC_DSL_DISALLOWED_MARKERS_RE.search(s):
        return False
    if not _FUNC_DSL_REQUIRED_ROOT_RE.search(s):
        return False
    return bool(_FUNC_DSL_ALLOWED_CALL_RE.search(s))


def _build_function_dsl_fallback(*, question: str, context: str) -> str:
    # Client only supports a restricted function-style DSL, so keep this minimal and stable.
    q = _escape_openui_string((question or "").strip())[:8000]
    c = _escape_openui_string((context or "").strip())[:8000]
    return (
        "root = Column()\n"
        f'question = TextArea(label="질문", placeholder="{q}")\n'
        f'context = TextArea(label="컨텍스트", placeholder="{c}")\n'
        'approval = RadioGroup(label="진행 승인", options=["승인", "취소"])\n'
        'comment = TextArea(label="추가 메모", placeholder="예: 운영 DB 맞음, 지금 진행")\n'
        'submit = Button(label="제출")\n'
    )


def build_openui_lang_for_hitl(
    *,
    question: str,
    context: str,
    run_state: dict[str, Any] | None,
) -> str:
    """Return full OpenUI Lang text for this HITL (function-DSL enforced)."""
    rs = run_state if isinstance(run_state, dict) else {}
    custom = rs.get("openui_lang")
    if isinstance(custom, str) and custom.strip():
        body = custom.strip()
        if len(body) > _MAX_LANG_CHARS:
            body = body[:_MAX_LANG_CHARS]
        if _looks_like_function_dsl(body):
            return body
        # Reject incompatible custom OpenUI Lang and fall back to function DSL.
        return _build_function_dsl_fallback(question=question, context=context)

    return _build_function_dsl_fallback(question=question, context=context)


def expand_input_required_to_openui_hitl_events(
    evt: dict[str, Any],
    *,
    chunk_size: int = _DEFAULT_LANG_CHUNK_SIZE,
) -> list[dict[str, Any]] | None:
    """Expand ``input_required`` into OpenUI HITL SSE events, or None to use legacy path.

    Returns None if ``evt`` is not an ``input_required`` dict.
    Returns a non-empty list on success; on idempotent duplicate returns ``[evt]``
    (passthrough) so the client still sees a HITL signal.
    """
    if not isinstance(evt, dict):
        return None
    if str(evt.get("type") or "").strip() != "input_required":
        return None

    conv_id = str(evt.get("conversation_id") or "").strip()
    run_state = evt.get("run_state") if isinstance(evt.get("run_state"), dict) else None
    question_id = question_id_from_run_state(run_state)
    question = resolve_question_text(evt=evt, run_state=run_state)
    context = resolve_context_text(run_state=run_state)

    if not register_run_state_for_resume(
        question_id=question_id,
        conversation_id=conv_id,
        run_state=run_state,
    ):
        return [dict(evt)]

    full_lang = build_openui_lang_for_hitl(question=question, context=context, run_state=run_state)
    chunk_size = max(64, min(int(chunk_size) or _DEFAULT_LANG_CHUNK_SIZE, 4096))
    chunks: list[str] = []
    for i in range(0, len(full_lang), chunk_size):
        chunks.append(full_lang[i : i + chunk_size])

    out: list[dict[str, Any]] = []
    for seq, piece in enumerate(chunks):
        row: dict[str, Any] = {
            "type": "openui",
            "op": "delta",
            "schema_version": OPENUI_HITL_SCHEMA_VERSION,
            "conversation_id": conv_id,
            "question_id": question_id,
            "seq": seq,
            "openui_lang_chunk": piece,
        }
        if seq == 0:
            row["question"] = question
            row["context"] = context
            if run_state is not None:
                row["run_state"] = run_state
        out.append(row)

    out.append(
        {
            "type": "openui",
            "op": "end",
            "schema_version": OPENUI_HITL_SCHEMA_VERSION,
            "conversation_id": conv_id,
            "question_id": question_id,
            "seq": len(chunks),
            "finished": True,
            "run_state": run_state,
            "openui_lang": full_lang,
            "lang_char_len": len(full_lang),
        }
    )
    return out
