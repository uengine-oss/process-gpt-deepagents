from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

INPUTDATA_MARKER = "[InputData]"


def try_parse_json_object_from_text(text: str) -> tuple[dict[str, Any] | None, str]:
    """Best-effort parse a single JSON object from the start of `text`.

    Returns (obj, remainder). Never raises.
    """
    s = (text or "").lstrip()
    if not s:
        return None, ""
    # Common wrappers from markdown/code blocks.
    if s.startswith("```"):
        parts = s.split("\n", 1)
        if len(parts) == 2:
            s = parts[1].lstrip()
    try:
        dec = json.JSONDecoder()
        obj, idx = dec.raw_decode(s)
        if isinstance(obj, dict):
            return obj, s[idx:].lstrip()
        return None, s
    except Exception:
        return None, s


def extract_inputdata_from_message(message: str) -> tuple[str, dict[str, Any] | None]:
    """Extract `[InputData]` JSON embedded in a user message.

    - If found and parsed: returns (cleaned_message, inputdata_dict)
    - If not found / parse fails: returns (original_message, None)

    Defensive by design: **never** raise; callers must treat parsing as optional.
    """
    msg = message or ""
    i = msg.find(INPUTDATA_MARKER)
    if i < 0:
        return msg, None
    before = msg[:i].rstrip()
    after = msg[i + len(INPUTDATA_MARKER) :].strip()
    if not after:
        return before or msg, None
    obj, _ = try_parse_json_object_from_text(after)
    if obj is None:
        return msg, None
    return before, obj


def merge_inputdata_into_chat_payload(payload: dict[str, Any], inputdata: dict[str, Any]) -> dict[str, Any]:
    """Merge parsed InputData into a chat payload dict (best-effort).

    Policy:
    - `payload` is treated as the authoritative API body.
    - `inputdata` fills missing `file/files/file_count` if absent/empty in `payload`.
    - `metadata` is merged, with `payload["metadata"]` winning on key conflicts.

    Never raises.
    """
    try:
        out = dict(payload or {})
        if not isinstance(inputdata, dict):
            return out

        file_obj = inputdata.get("file")
        files_obj = inputdata.get("files")
        md_obj = inputdata.get("metadata")
        file_count_obj = inputdata.get("fileCount")

        if isinstance(file_obj, dict) and not isinstance(out.get("file"), dict):
            out["file"] = file_obj
        if isinstance(files_obj, list) and (not isinstance(out.get("files"), list) or len(out.get("files") or []) == 0):
            out["files"] = [x for x in files_obj if isinstance(x, dict)]
        if isinstance(file_count_obj, int) and int(out.get("file_count") or 0) <= 0:
            out["file_count"] = int(file_count_obj)

        if isinstance(md_obj, dict):
            merged = dict(md_obj)
            if isinstance(out.get("metadata"), dict):
                merged.update(out["metadata"])
            out["metadata"] = merged

        return out
    except Exception:
        logger.exception("InputData merge failed (ignored)", exc_info=True)
        return dict(payload or {})

