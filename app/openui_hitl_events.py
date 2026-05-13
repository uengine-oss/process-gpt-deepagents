"""OpenUI Lang-based HITL events for HTTP chat streaming (SSE).

Wire contract (schema ``1.0``)
-------------------------------

Each SSE ``data`` line is one JSON object (SDK /chat/stream contract).

Legacy ``input_required`` is expanded into a short sequence.

Optional on the ``input_required`` dict: ``openui_lang`` — canonical Lang (same as
``ChatResponse.openui_lang`` / DB). When ``run_state`` omits the raw agent string
(streaming payloads), this field keeps SSE ``end.openui_lang`` aligned with storage.

- ``type: openui`` + ``op: delta`` (repeated): incremental ``openui_lang_chunk`` text.
  - ``seq == 0`` frames may also include ``question``, ``context``, ``run_state``
    so the client can render chrome before Lang completes.
- ``type: openui`` + ``op: end`` (once): carries full ``openui_lang``, ``run_state``,
  ``question_id``, and ``lang_char_len`` for verification.

Common fields on every frame: ``type``, ``op``, ``schema_version``, ``conversation_id``, ``question_id``.

When ``run_state`` is attached to frames, ``openui_lang`` is **omitted** there; the canonical
definition is only in ``op: end`` → ``openui_lang`` (and must begin with ``root =``).

OpenUI Lang source (M1 vs default template)
-------------------------------------------

- **M1**: If ``run_state["openui_lang"]`` is a non-empty string, common **named
  keyword** forms (``title=``, ``name=``, ``options=``, …) are rewritten to the
  **positional** call style the web client expects, then validated.
- **Default (template)**: Otherwise a minimal snippet is built from ``question``
  and ``context`` using ``root = Card([header, formBlock])`` and ``Form`` fields.

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


_DQ_STR = r'"(?:[^"\\]|\\.)*"'
_SQ_STR = r"'(?:[^'\\]|\\.)*'"
_STR_TOKEN = rf"(?:{_DQ_STR}|{_SQ_STR})"


def _split_top_level_commas(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    depth_brace = 0
    depth_bracket = 0
    depth_paren = 0
    in_str: str | None = None
    escape = False
    for ch in inner:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if in_str:
            buf.append(ch)
            if ch == "\\":
                escape = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
            buf.append(ch)
            continue
        if ch == "{":
            depth_brace += 1
            buf.append(ch)
            continue
        if ch == "}":
            depth_brace -= 1
            buf.append(ch)
            continue
        if ch == "[":
            depth_bracket += 1
            buf.append(ch)
            continue
        if ch == "]":
            depth_bracket -= 1
            buf.append(ch)
            continue
        if ch == "(":
            depth_paren += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth_paren -= 1
            buf.append(ch)
            continue
        if ch == "," and depth_brace == 0 and depth_bracket == 0 and depth_paren == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _parse_kw_fragment(part: str) -> tuple[str, str] | None:
    m = re.match(r"^\s*([a-zA-Z_]\w*)\s*=\s*(.+)$", part, re.DOTALL)
    if not m:
        return None
    return m.group(1).strip().lower(), m.group(2).strip()


def _inner_to_kw_dict(inner: str) -> dict[str, str] | None:
    parts = _split_top_level_commas(inner)
    if not parts:
        return None
    kw: dict[str, str] = {}
    for p in parts:
        kv = _parse_kw_fragment(p)
        if not kv:
            return None
        kw[kv[0]] = kv[1]
    return kw


def _bool_literal_for_dsl(value_token: str) -> str:
    t = (value_token or "").strip()
    if t.lower() in ("false", "true"):
        return t.lower()
    return t


def _find_matching_paren(s: str, open_paren_idx: int) -> int | None:
    if open_paren_idx >= len(s) or s[open_paren_idx] != "(":
        return None
    depth = 0
    i = open_paren_idx
    in_str: str | None = None
    escape = False
    while i < len(s):
        ch = s[i]
        if escape:
            escape = False
            i += 1
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


_WIDGET_CALL_RE = re.compile(
    r"\b(CardHeader|Card|Form|Input|Textarea|Select|Radio|Checkbox)\s*\(",
)


def _normalize_openui_named_kwargs_to_positional(openui_lang: str) -> str:
    """Rewrite LLM-friendly keyword calls to positional-only Card DSL."""

    def pass_cardheader_regex(s: str) -> str:
        out = s
        subs: list[tuple[str, str]] = [
            (
                rf"CardHeader\s*\(\s*title\s*=\s*({_STR_TOKEN})\s*,\s*subtitle\s*=\s*({_STR_TOKEN})\s*\)",
                r"CardHeader(\1, \2)",
            ),
            (
                rf"CardHeader\s*\(\s*title\s*=\s*({_STR_TOKEN})\s*,\s*description\s*=\s*({_STR_TOKEN})\s*\)",
                r"CardHeader(\1, \2)",
            ),
            (
                rf"CardHeader\s*\(\s*subtitle\s*=\s*({_STR_TOKEN})\s*,\s*title\s*=\s*({_STR_TOKEN})\s*\)",
                r"CardHeader(\2, \1)",
            ),
            (
                rf"CardHeader\s*\(\s*description\s*=\s*({_STR_TOKEN})\s*,\s*title\s*=\s*({_STR_TOKEN})\s*\)",
                r"CardHeader(\2, \1)",
            ),
        ]
        for pat, repl in subs:
            out = re.sub(pat, repl, out, flags=re.DOTALL)
        return out

    s = pass_cardheader_regex(openui_lang)

    def _positional_inner_for_named_widget(name: str, inner: str) -> str | None:
        if "=" not in inner:
            return None
        new_inner: str | None = None

        if name == "CardHeader":
            kw = _inner_to_kw_dict(inner)
            if kw:
                t = kw.get("title")
                sub = kw.get("subtitle") or kw.get("description")
                if t and sub:
                    new_inner = f"{t}, {sub}"
                elif t:
                    new_inner = f'{t}, ""'

        elif name == "Card":
            kw = _inner_to_kw_dict(inner)
            if kw and "children" in kw:
                new_inner = kw["children"].strip()

        elif name == "Form":
            kw = _inner_to_kw_dict(inner)
            if kw:
                fields = kw.get("fields")
                desc = kw.get("description") or kw.get("subtitle") or kw.get("help")
                submit = (
                    kw.get("submitlabel")
                    or kw.get("submit_label")
                    or kw.get("submit")
                    or kw.get("buttontext")
                    or kw.get("button_text")
                )
                if fields and desc and submit:
                    new_inner = f"{fields}, {desc}, {submit}"

        elif name == "Input":
            kw = _inner_to_kw_dict(inner)
            if kw:
                n = kw.get("name")
                lab = kw.get("label")
                dft = kw.get("default") or kw.get("value")
                if n and lab and dft is not None:
                    new_inner = f"{n}, {lab}, {dft}"

        elif name == "Textarea":
            kw = _inner_to_kw_dict(inner)
            if kw:
                n = kw.get("name")
                lab = kw.get("label")
                dft = kw.get("default") or kw.get("value")
                ph = kw.get("placeholder") or kw.get("hint") or '""'
                rows = kw.get("rows") or kw.get("rowcount") or "4"
                if n and lab and dft is not None:
                    new_inner = f"{n}, {lab}, {dft}, {ph}, {rows.strip()}"

        elif name == "Radio":
            kw = _inner_to_kw_dict(inner)
            if kw:
                n = kw.get("name")
                lab = kw.get("label")
                opts = kw.get("options")
                dft = kw.get("default") or kw.get("value")
                if n and lab and opts is not None and dft is not None and opts.lstrip().startswith("{"):
                    new_inner = f"{n}, {lab}, {opts}, {dft}"

        elif name == "Select":
            kw = _inner_to_kw_dict(inner)
            if kw:
                n = kw.get("name")
                lab = kw.get("label")
                opts = kw.get("options")
                dft = kw.get("default") or kw.get("value")
                hint = kw.get("placeholder") or kw.get("hint") or kw.get("help")
                if n and lab and opts is not None and dft is not None and opts.lstrip().startswith("{"):
                    if hint:
                        new_inner = f"{n}, {lab}, {opts}, {dft}, {hint}"
                    else:
                        new_inner = f"{n}, {lab}, {opts}, {dft}"

        elif name == "Checkbox":
            kw = _inner_to_kw_dict(inner)
            if kw:
                n = kw.get("name")
                lab = kw.get("label")
                dft = kw.get("default") or kw.get("checked") or kw.get("value")
                if n and lab and dft is not None:
                    new_inner = f"{n}, {lab}, {_bool_literal_for_dsl(dft)}"

        return new_inner

    # One replacement per iteration, rightmost match first, so nested spans stay valid.
    for _ in range(256):
        best: tuple[int, int, str] | None = None
        best_start = -1
        for m in _WIDGET_CALL_RE.finditer(s):
            wname = m.group(1)
            open_idx = m.end() - 1
            close_idx = _find_matching_paren(s, open_idx)
            if close_idx is None:
                continue
            inner = s[open_idx + 1 : close_idx]
            new_inner = _positional_inner_for_named_widget(wname, inner)
            if new_inner is None:
                continue
            if m.start() >= best_start:
                best_start = m.start()
                best = (m.start(), close_idx + 1, f"{wname}({new_inner})")
        if not best:
            break
        a, b, text = best
        s = s[:a] + text + s[b:]

    s = pass_cardheader_regex(s)
    return s


_FUNC_DSL_REQUIRED_COLUMN_ROOT_RE = re.compile(r"(?m)^\s*root\s*=\s*Column\(\)\s*$")
_FUNC_DSL_COLUMN_WIDGET_RE = re.compile(
    r"\b(?:Column|RadioGroup|TextArea|TextInput|Button|Select|Checkbox|Row|Stack)\s*\(",
)
_FUNC_DSL_REQUIRED_CARD_ROOT_RE = re.compile(r"(?m)^\s*root\s*=\s*Card\s*\(")
_FUNC_DSL_CARD_WIDGET_RE = re.compile(
    r"\b(?:Card|CardHeader|Form|Input|Textarea|Select|Radio|Checkbox)\s*\(",
)
_FUNC_DSL_DISALLOWED_MARKERS_RE = re.compile(r"(?m)^\s*(component\s+\w+|\{|\})\s*$")


def _lang_starts_with_root_assignment(openui_lang: str) -> bool:
    return bool(re.match(r"\A\s*root\s*=", (openui_lang or "").strip(), flags=re.DOTALL))


def _ensure_root_assignment_prefix(openui_lang: str) -> str:
    """Clients require the document to start with ``root =``; bare ``Card(...)`` gets prefixed."""
    s = (openui_lang or "").strip()
    if not s:
        return s
    if _lang_starts_with_root_assignment(s):
        return s
    if re.match(r"\A\s*Card\s*\(", s, flags=re.DOTALL):
        return "root = " + s.lstrip()
    return s


_FIELD_VAR_SHORT: dict[str, str] = {
    "environment": "env",
    "project_ref": "proj",
    "confirm_delete": "conf",
    "note": "note",
}


def _first_quoted_id_from_widget(expr: str) -> str | None:
    m = re.match(
        r"^\s*(?:Select|Input|Radio|Textarea|Checkbox)\s*\(\s*\"([^\"]+)\"",
        expr.strip(),
    )
    return m.group(1) if m else None


def _allocate_field_var_name(first_id: str, used: set[str]) -> str:
    base = _FIELD_VAR_SHORT.get(first_id) or re.sub(r"[^a-zA-Z0-9_]", "_", first_id) or "field"
    if base and not (base[0].isalpha() or base[0] == "_"):
        base = f"f_{base}"
    name = base[:48]
    n = 2
    while name in used:
        name = f"{base[:40]}_{n}"
        n += 1
    used.add(name)
    return name


def _rewrite_nested_card_inline_to_named_assignments(openui_lang: str) -> str | None:
    """``root = Card( CardHeader(...), Form( w1, w2, ... ) )`` → ``root = Card([hdr, frm])`` + named lines.

    Already-list style ``root = Card([hdr, frm])`` is left unchanged (returns None).
    """
    s = (openui_lang or "").strip()
    if not s:
        return None
    m = re.match(r"^root\s*=\s*Card\s*\(", s, flags=re.DOTALL)
    if not m:
        return None
    open_paren = m.end() - 1
    j = open_paren + 1
    while j < len(s) and s[j] in " \t\n\r":
        j += 1
    if j < len(s) and s[j] == "[":
        return None
    close_paren = _find_matching_paren(s, open_paren)
    if close_paren is None or s[close_paren + 1 :].strip():
        return None
    inner = s[open_paren + 1 : close_paren].strip()
    parts = _split_top_level_commas(inner)
    if len(parts) != 2:
        return None
    p0, p1 = parts[0].strip(), parts[1].strip()
    if not (p0.startswith("CardHeader(") and p1.startswith("Form(")):
        return None
    fo = p1.find("(")
    fc = _find_matching_paren(p1, fo)
    if fc is None:
        return None
    form_inner = p1[fo + 1 : fc].strip()
    widgets = [w.strip() for w in _split_top_level_commas(form_inner) if w.strip()]
    if not widgets:
        return None
    used: set[str] = set()
    names: list[str] = []
    for w in widgets:
        fid = _first_quoted_id_from_widget(w)
        if not fid:
            return None
        names.append(_allocate_field_var_name(fid, used))
    lines = [
        "root = Card([hdr, frm])",
        f"hdr = {p0}",
        'frm = Form([' + ", ".join(names) + '], "확인 후 제출해 주세요.", "제출")',
    ]
    lines.extend(f"{nm} = {widgets[i]}" for i, nm in enumerate(names))
    return "\n".join(lines) + "\n"


def run_state_without_openui_lang(run_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shallow copy of ``run_state`` without ``openui_lang`` (avoids duplicating the end-frame field)."""
    if run_state is None:
        return None
    out = dict(run_state)
    out.pop("openui_lang", None)
    return out


def _looks_like_function_dsl(openui_lang: str) -> bool:
    s = (openui_lang or "").strip()
    if not s:
        return False
    if not _lang_starts_with_root_assignment(s):
        return False
    if _FUNC_DSL_DISALLOWED_MARKERS_RE.search(s):
        return False
    if _FUNC_DSL_REQUIRED_COLUMN_ROOT_RE.search(s) and _FUNC_DSL_COLUMN_WIDGET_RE.search(s):
        return True
    if _FUNC_DSL_REQUIRED_CARD_ROOT_RE.search(s) and _FUNC_DSL_CARD_WIDGET_RE.search(s):
        return True
    return False


def _build_function_dsl_fallback(*, question: str, context: str) -> str:
    # Card/Form function-style DSL; keep field ids stable for clients.
    q = _escape_openui_string((question or "").strip())[:8000]
    c = _escape_openui_string((context or "").strip())[:8000]
    return (
        "root = Card([header, formBlock])\n"
        'header = CardHeader("사람 입력 필요", "에이전트가 확인이 필요합니다. 아래 양식을 작성한 뒤 제출하세요.")\n'
        'formBlock = Form([questionField, contextField, approvalField, commentField], '
        '"질문에 답한 뒤 제출 버튼을 눌러주세요.", "제출")\n'
        f'questionField = Textarea("hitl_question", "질문", "", "{q}", 4)\n'
        f'contextField = Textarea("hitl_context", "컨텍스트", "", "{c}", 4)\n'
        'approvalField = Radio("hitl_approval", "진행", {"approve": "승인", "cancel": "취소"}, "approve")\n'
        'commentField = Textarea("hitl_comment", "추가 메모", "", "예: 운영 DB 맞음, 지금 진행", 3)\n'
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
        body = _normalize_openui_named_kwargs_to_positional(body)
        body = _ensure_root_assignment_prefix(body)
        if _looks_like_function_dsl(body):
            flattened = _rewrite_nested_card_inline_to_named_assignments(body)
            if flattened and _looks_like_function_dsl(flattened):
                return flattened
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

    pre = evt.get("openui_lang")
    if isinstance(pre, str) and pre.strip():
        full_lang = pre.strip()
        if len(full_lang) > _MAX_LANG_CHARS:
            full_lang = full_lang[:_MAX_LANG_CHARS]
        flat = _rewrite_nested_card_inline_to_named_assignments(full_lang)
        if flat and _looks_like_function_dsl(flat):
            full_lang = flat.rstrip("\n")
    else:
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
            rs_evt = run_state_without_openui_lang(run_state)
            if rs_evt is not None:
                row["run_state"] = rs_evt
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
            "run_state": run_state_without_openui_lang(run_state),
            "openui_lang": full_lang,
            "lang_char_len": len(full_lang),
        }
    )
    return out
