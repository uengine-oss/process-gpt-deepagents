import json
import re
from typing import Any, Dict, Tuple


_BPMN_PATH_HINT_RE = re.compile(r"\.bpmn\b", re.IGNORECASE)
# Last `{"output": ...}` object in a model reply (suffix), or whole-string JSON.
_OUTPUT_OBJECT_START_RE = re.compile(r"\{\s*\"output\"\s*:")


def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    # Remove leading/trailing code fences (single block)
    stripped = text.strip()
    stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", stripped)
    stripped = re.sub(r"\n```$", "", stripped)
    return stripped.strip()


def _try_parse_json(text: str) -> Tuple[Dict[str, Any] | None, str]:
    """Return (json_dict_or_none, cleaned_text)."""
    cleaned = _strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj, cleaned
    except Exception:
        pass
    return None, cleaned


def _output_result_string_from_dict(parsed: dict[str, Any]) -> str | None:
    out = parsed.get("output")
    if not isinstance(out, dict):
        return None
    r = out.get("result")
    if isinstance(r, str):
        return r
    if isinstance(r, (dict, list)):
        return json.dumps(r, ensure_ascii=False)
    if r is None:
        return None
    return str(r)


def unwrap_output_wrapper_result_text(text: str) -> str:
    """Remove raw ``{"output":{...}}`` from text while keeping user-visible prose.

    - If the **entire** string is that JSON object, returns only ``output.result`` (no prefix to keep).
    - If that JSON is a **suffix** (prose then ``{"output":...}``), returns
      ``<prefix> + "\\n\\n" + output.result`` so the structured ``result`` is **appended**, not a substitute
      for the leading content.
    - Otherwise returns the stripped original string.

    The wrapper JSON itself never appears in the returned string.
    """
    raw = text if isinstance(text, str) else ""
    s = raw.strip()
    if not s:
        return ""

    parsed, _ = _try_parse_json(s)
    if isinstance(parsed, dict):
        inner = _output_result_string_from_dict(parsed)
        if inner is not None:
            return inner.strip()

    for m in reversed(list(_OUTPUT_OBJECT_START_RE.finditer(s))):
        tail = s[m.start() :]
        try:
            obj = json.loads(tail)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        inner = _output_result_string_from_dict(obj)
        if inner is None:
            continue
        inner = inner.strip()
        prefix = s[: m.start()].rstrip()
        if prefix:
            return f"{prefix}\n\n{inner}"
        return inner

    return s


def ensure_trailing_newline(text: str) -> str:
    """Guarantee a single trailing newline for SSE / chat ``content`` fields."""
    s = "" if text is None else str(text)
    return s if s.endswith("\n") else s + "\n"


def sanitize_deliverable_form_fields(
    pure_form_data: dict[str, Any],
    *,
    file_manifest: list[dict[str, Any]] | None,
    preferred_keys: tuple[str, ...] = ("draft_file", "draftFile", "file", "attachment", "result_file"),
) -> dict[str, Any]:
    """폼의 파일 필드를 업로드 산출물 `public_url`로 보정합니다.

    - 입력 `.bpmn` 경로가 들어간 경우: 업로드된 산출물 public_url로 치환
    - 파일 필드가 빈 값인 경우: (해당 키가 존재하면) public_url로 채움
    """
    if not isinstance(pure_form_data, dict):
        return pure_form_data
    manifest = file_manifest or []
    candidates: list[str] = []
    for f in manifest:
        if not isinstance(f, dict):
            continue
        url = f.get("public_url")
        if not isinstance(url, str) or not url.strip():
            continue
        mime = str(f.get("mime_type") or "").lower()
        name = str(f.get("name") or "").lower()
        if "spreadsheet" in mime or mime.endswith("sheet") or name.endswith(".xlsx") or name.endswith(".xls"):
            candidates.append(url.strip())
        elif not mime and not name:
            candidates.append(url.strip())
    if not candidates:
        for f in manifest:
            if isinstance(f, dict) and isinstance(f.get("public_url"), str) and f["public_url"].strip():
                candidates.append(str(f["public_url"]).strip())
    out = dict(pure_form_data)
    for k in preferred_keys:
        if k not in out:
            continue
        v = out.get(k)
        if v is None:
            if candidates:
                out[k] = candidates[0]
            continue
        if isinstance(v, str):
            s = v.strip()
            if not s:
                if candidates:
                    out[k] = candidates[0]
                continue
            if _BPMN_PATH_HINT_RE.search(s) or s.lower().endswith(".bpmn"):
                if candidates:
                    out[k] = candidates[0]
    return out


def convert_agent_output(
    raw_result: Any,
    form_id: str | None,
    form_fields: Any,
) -> tuple[dict, dict, str, dict, dict]:
    """Convert deepagents output into ProcessGPT expected payload.

    Returns:
      pure_form_data: dict
      wrapped_result: dict (for artifact)
      original_wo_form: str
      report_fields: dict
      slide_fields: dict

    Notes:
    - We keep this intentionally conservative because form schema differs by tenant.
    - If the model returns valid JSON, we use it as the primary output.
    - Otherwise, we return a best-effort structure with a single 'text' field.
    """
    # DeepAgents typically returns a string or a LangChain message object.
    if raw_result is None:
        text = ""
    elif isinstance(raw_result, str):
        text = raw_result
    else:
        # Best effort: many LC objects have `content`
        text = getattr(raw_result, "content", None)
        if text is None:
            text = str(raw_result)

    parsed, cleaned = _try_parse_json(text)

    wrapped_result = {
        "form_id": form_id,
        "raw": text,
        "cleaned": cleaned,
        "json": parsed,
    }

    # Minimal report/slide split hooks (kept empty unless your downstream expects it)
    report_fields: dict = {}
    slide_fields: dict = {}

    if parsed is not None:
        pure_form_data = parsed
        original_wo_form = cleaned
        return pure_form_data, wrapped_result, original_wo_form, report_fields, slide_fields

    # Fallback: no JSON found.
    pure_form_data = {"text": cleaned}
    original_wo_form = cleaned
    return pure_form_data, wrapped_result, original_wo_form, report_fields, slide_fields
