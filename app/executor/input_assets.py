"""Collect file-like inputs from work-item InputData (generic, no path-specific branching)."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"\]\)]+", flags=re.IGNORECASE)


def _ext_from_string(s: str) -> str:
    raw = (s or "").strip().lower()
    if not raw or "." not in raw:
        return ""
    return raw.rsplit(".", 1)[-1].split("?")[0].split("#")[0]


def _guess_type_from_ext(ext: str) -> str:
    e = (ext or "").strip().lower()
    if e in ("bpmn", "xml"):
        return "bpmn" if e == "bpmn" else "xml"
    if e in ("pdf", "xlsx", "xls", "csv", "docx", "html", "htm", "md", "txt", "png", "jpg", "jpeg", "gif", "webp"):
        if e in ("htm",):
            return "html"
        if e in ("jpeg",):
            return "jpg"
        if e in ("xls",):
            return "xlsx"
        return e
    return e or "file"


def _path_or_url_from_obj(obj: dict[str, Any]) -> str:
    # Order matters: prefer canonical storage fields (fullPath, url, path) then file_path (proc_inst sources).
    for k in (
        "fullPath",
        "full_path",
        "url",
        "href",
        "path",
        "file_path",
        "filePath",
        "uri",
        "src",
        "location",
    ):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def collect_input_assets(
    *,
    input_data: Mapping[str, Any] | dict[str, Any] | list[Any] | None,
    extra_text: str = "",
    extra_assets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Walk nested dict/list structures and collect assets with stable ids.

    Returns items: ``{"id": "...", "path": "<url or path>", "type": "<inferred>"}``.

    ``extra_assets`` (e.g. proc_inst ``sources``) is merged after main collection;
    nested dict/list trees are walked recursively for ``path`` / ``url`` (and peers
    handled by ``_path_or_url_from_obj``), reusing ``seen`` for deduplication.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add_path(p: str, *, source_origin: str) -> None:
        p = (p or "").strip()
        if not p or p in seen:
            return
        seen.add(p)
        ext = _ext_from_string(p)
        aid = f"asset_{len(out)}"
        out.append(
            {
                "id": aid,
                "path": p,
                "type": _guess_type_from_ext(ext),
                "source_origin": source_origin,
            }
        )

    def walk(node: Any, *, source_origin: str) -> None:
        if isinstance(node, dict):
            p = _path_or_url_from_obj(node)
            if p:
                add_path(p, source_origin=source_origin)
            for v in node.values():
                walk(v, source_origin=source_origin)
        elif isinstance(node, list):
            for v in node:
                walk(v, source_origin=source_origin)
        elif isinstance(node, str):
            s = node.strip()
            if s.startswith("http://") or s.startswith("https://"):
                add_path(s, source_origin=source_origin)

    if isinstance(input_data, dict):
        walk(input_data, source_origin="input_data")
    elif isinstance(input_data, list):
        walk(input_data, source_origin="input_data")
    elif input_data is None:
        pass

    blob = f"{extra_text}\n"
    if isinstance(input_data, dict):
        try:
            import json as _json

            blob += _json.dumps(input_data, ensure_ascii=False)
        except Exception:
            blob += str(input_data)
    for m in _HTTP_URL_RE.findall(blob):
        add_path(m, source_origin="embedded_url")

    if extra_assets:
        for root in extra_assets:
            # Top-level list items are often source rows { file_name, file_path, ... }; walk handles dict/list.
            walk(root, source_origin="proc_inst_source")

    # Renumber ids sequentially for stability
    for i, a in enumerate(out):
        a["id"] = f"asset_{i}"

    _log_collected_assets(out)
    return out


def build_asset_inventory(assets: list[dict[str, Any]]) -> dict[str, Any]:
    """Structured snapshot for orchestrator state / prompts (no asset rows dropped)."""
    by_type: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    for a in assets:
        if not isinstance(a, dict):
            continue
        t = str(a.get("type") or "file").strip() or "file"
        by_type[t] = by_type.get(t, 0) + 1
        o = str(a.get("source_origin") or "unknown").strip() or "unknown"
        by_origin[o] = by_origin.get(o, 0) + 1
    return {
        "assets": list(assets),
        "total_count": len(assets),
        "by_type": by_type,
        "by_source_origin": by_origin,
    }


def _log_collected_assets(assets: list[dict[str, Any]]) -> None:
    """Debug/audit: count and per-asset type + path (paths truncated in log only)."""
    n = len(assets)
    logger.info("collect_input_assets: total collected=%d", n)
    for a in assets:
        path = a.get("path")
        pstr = path if isinstance(path, str) else str(path)
        if len(pstr) > 800:
            pstr = pstr[:400] + "…" + pstr[-200:]
        logger.info(
            "collect_input_assets: asset id=%s type=%s path=%s",
            a.get("id"),
            a.get("type"),
            pstr,
        )


