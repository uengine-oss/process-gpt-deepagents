"""Derive ``pipeline_outputs`` updates from ``task()`` Command results (parent graph state)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

KNOWN_PIPELINE_OUTPUT_KEYS: frozenset[str] = frozenset(
    {
        "asset_inventory",
        "asset_roles",
        "template_schema",
        "process_facts",
        "reference_facts",
        "filled_template",
        "artifact_file",
        "public_url",
        "deliverable_files",
        "execution_trace",
    }
)

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _tool_message_texts(messages: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(messages, list):
        return out
    try:
        from langchain_core.messages import ToolMessage
    except Exception:
        ToolMessage = None  # type: ignore[misc, assignment]
    for m in messages:
        if ToolMessage is not None and isinstance(m, ToolMessage):
            c = m.content
            if isinstance(c, str) and c.strip():
                out.append(c.strip())
    return out


def _pick_json_objects_from_text(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not text:
        return found
    t = text.strip()
    if "```" in t:
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE | re.MULTILINE)
        t = re.sub(r"\s*```$", "", t, flags=re.MULTILINE)
        t = t.strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            o = json.loads(t)
            if isinstance(o, dict):
                return [o]
        except Exception:
            pass
    for m in _JSON_BLOCK_RE.findall(text):
        try:
            o = json.loads(m)
            if isinstance(o, dict):
                found.append(o)
        except Exception:
            continue
    return found


def extract_pipeline_outputs_patch(
    *,
    subagent_type: str,
    tool_messages_text: str,
) -> dict[str, Any]:
    """Return a flat patch dict for ``pipeline_outputs`` (maybe empty)."""
    patch: dict[str, Any] = {}
    for obj in _pick_json_objects_from_text(tool_messages_text):
        hits = {k: obj[k] for k in KNOWN_PIPELINE_OUTPUT_KEYS if k in obj}
        if hits:
            patch.update(hits)
    return patch


def merge_task_command_pipeline_outputs(result: Any, subagent_type: str) -> Any:
    """If ``result`` is a LangGraph ``Command`` from ``task``, merge parsed outputs into ``pipeline_outputs``."""
    try:
        from langgraph.types import Command
    except Exception:
        return result

    if not isinstance(result, Command):
        return result
    upd = getattr(result, "update", None)
    if not isinstance(upd, dict):
        return result
    new_upd = dict(upd)
    msgs = new_upd.get("messages")
    texts: list[str] = []
    if isinstance(msgs, list):
        texts = _tool_message_texts(msgs)
    blob = "\n".join(texts)
    patch = extract_pipeline_outputs_patch(subagent_type=subagent_type or "", tool_messages_text=blob)
    if not patch:
        return result
    cur = new_upd.get("pipeline_outputs")
    base: dict[str, Any] = dict(cur) if isinstance(cur, dict) else {}
    for k, v in patch.items():
        if v is not None:
            base[k] = v
    new_upd["pipeline_outputs"] = base
    try:
        return Command(update=new_upd)
    except Exception:
        logger.exception("merge_task_command_pipeline_outputs: Command rebuild failed")
        return result
