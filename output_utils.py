import json
import re
from typing import Any, Dict, Tuple


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


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
