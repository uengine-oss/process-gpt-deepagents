"""Resolve generic semantic roles for input assets (no fixed domain pipelines)."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

# System prompt budget for 섹션 1a (compact JSON). The previous 12k char cut dropped late assets (e.g. PDFs after BPMNs).
_MAX_ASSET_ROLES_PROMPT_CHARS = 480_000
# If still over budget, shorten individual paths but keep every asset row.
_PATH_SOFT_CAP = 1800

AssetRole = Literal[
    "template_candidate",
    "source_of_truth",
    "reference_material",
    "output_hint",
]

_ROLE_ORDER: tuple[AssetRole, ...] = (
    "template_candidate",
    "source_of_truth",
    "reference_material",
    "output_hint",
)

# When user text matches these (substring) and a PDF input exists, every PDF is forced to template_candidate.
_TEMPLATE_FORM_USER_MARKERS: tuple[str, ...] = (
    "양식",
    "템플릿",
    "초안",
    "서식",
)


def _user_text_implies_template_form_intent(user_text: str) -> bool:
    u = user_text or ""
    if not u.strip():
        return False
    return any(m in u for m in _TEMPLATE_FORM_USER_MARKERS)


def _template_pdf_path_score(path: str) -> float:
    """Higher = more likely the office 'form template' PDF (vs RQ_BP reference packs)."""
    p = (path or "").strip().lower()
    bn = _basename(path).lower()
    s = 0.0
    weighted = (
        ("양식", 4.0),
        ("템플릿", 3.5),
        ("template", 3.0),
        ("초안", 3.0),
        ("서식", 3.0),
        ("업무flow", 2.5),
        ("flow 초안", 3.0),
        ("next bss", 2.5),
        ("doss", 2.0),
        ("wiki", 1.5),
        ("form", 2.0),
        ("layout", 1.0),
    )
    for kw, w in weighted:
        if kw in bn or kw in p:
            s += w
    # Reference / requirement PDFs: keep as reference, not primary template
    if "rq_bp" in bn or "rq-bp" in bn or bn.startswith("rq_bp"):
        s -= 3.0
    if "명세" in bn or "spec" in bn:
        s -= 0.5
    return s


def _refine_pdf_roles_for_template_form_intent(assets: list[dict[str, Any]], user_text: str) -> None:
    """When user asks for 양식/템플릿/초안: pick the strongest template PDF; others stay reference."""
    if not _user_text_implies_template_form_intent(user_text):
        return
    pdfs = [a for a in assets if isinstance(a, dict) and str(a.get("type") or "").strip().lower() == "pdf"]
    if not pdfs:
        return
    if len(pdfs) == 1:
        pdfs[0]["role"] = "template_candidate"
        pdfs[0]["confidence"] = 1.0
        return
    best = max(pdfs, key=lambda x: _template_pdf_path_score(str(x.get("path") or "")))
    for item in pdfs:
        path = str(item.get("path") or "")
        if item is best:
            item["role"] = "template_candidate"
            item["confidence"] = 1.0
        else:
            item["role"] = "reference_material"
            item["confidence"] = min(float(item.get("confidence") or 0.55), 0.82)

# Keyword families: language-agnostic tokens + common Korean/English office terms.
_KEYWORDS: dict[AssetRole, tuple[str, ...]] = {
    "template_candidate": (
        "template",
        "form",
        "layout",
        "양식",
        "템플릿",
        "서식",
        "sample",
        "샘플",
        "초안",
        "draft",
        "blank",
        "표지",
        "양식파일",
    ),
    "source_of_truth": (
        "bpmn",
        "process",
        "workflow",
        "xml",
        "diagram",
        "canonical",
        "master",
        "soa",
        "bpel",
        "프로세스",
        "업무흐름",
    ),
    "reference_material": (
        "reference",
        "guide",
        "manual",
        "readme",
        "doc",
        "문서",
        "가이드",
        "참고",
        "예시",
        "example",
        "spec",
        "명세",
    ),
    "output_hint": (
        "output",
        "deliverable",
        "산출",
        "결과물",
        "export",
        "xlsx",
        "excel",
        "spreadsheet",
        "download",
        "제출",
    ),
}


def _norm_token(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(text: str) -> set[str]:
    blob = _norm_token(text)
    blob = re.sub(r"[^a-z0-9가-힣_\-]+", " ", blob)
    return {p for p in blob.split() if len(p) >= 2}


def _basename(path: str) -> str:
    p = (path or "").strip().rstrip("/")
    if not p:
        return ""
    if "/" in p:
        p = p.split("/")[-1]
    if "\\" in p:
        p = p.split("\\")[-1]
    return p


def _type_scores(asset_type: str) -> dict[AssetRole, float]:
    t = (asset_type or "").strip().lower()
    base: dict[AssetRole, float] = {r: 0.0 for r in _ROLE_ORDER}
    if t in ("pdf", "docx", "html"):
        base["template_candidate"] += 1.2
        base["reference_material"] += 0.6
    if t in ("xlsx", "xls", "csv"):
        base["template_candidate"] += 0.9
        base["output_hint"] += 0.8
    if t in ("bpmn", "xml", "json"):
        base["source_of_truth"] += 1.5
        base["reference_material"] += 0.3
    if t in ("png", "jpg", "jpeg", "gif", "webp", "image"):
        base["reference_material"] += 0.8
    if t in ("txt", "md"):
        base["reference_material"] += 0.5
    return base


def _keyword_scores(name_and_context: str) -> dict[AssetRole, float]:
    tok = _tokens(name_and_context)
    scores: dict[AssetRole, float] = {r: 0.0 for r in _ROLE_ORDER}
    for role, kws in _KEYWORDS.items():
        for kw in kws:
            k = kw.lower()
            if k in tok:
                scores[role] += 1.0
            # substring match on basename/path fragments (still generic)
            if k in _norm_token(name_and_context):
                scores[role] += 0.35
    return scores


def _mention_scores(user_text: str, path: str) -> dict[AssetRole, float]:
    scores: dict[AssetRole, float] = {r: 0.0 for r in _ROLE_ORDER}
    ut = _norm_token(user_text)
    if not ut:
        return scores
    bn = _basename(path)
    if bn and bn.lower() in ut:
        for r in _ROLE_ORDER:
            scores[r] += 0.25
    # quoted or path-like mention
    if path and path in user_text:
        for r in _ROLE_ORDER:
            scores[r] += 0.15
    return scores


def _structural_scores(hints: dict[str, Any] | None) -> dict[AssetRole, float]:
    scores: dict[AssetRole, float] = {r: 0.0 for r in _ROLE_ORDER}
    if not isinstance(hints, dict):
        return scores
    if hints.get("has_tables") or hints.get("table_count", 0):
        scores["template_candidate"] += 1.0
    if hints.get("has_sections") or hints.get("section_count", 0):
        scores["template_candidate"] += 0.6
        scores["reference_material"] += 0.2
    if hints.get("is_process_graph") or hints.get("bpmn_like"):
        scores["source_of_truth"] += 1.2
    return scores


def _softmax_pick(raw: dict[AssetRole, float]) -> tuple[AssetRole, float]:
    vals = [max(0.0, raw[r]) for r in _ROLE_ORDER]
    m = max(vals) if vals else 0.0
    if m <= 0:
        # uniform low confidence default: treat as reference until planner refines
        return "reference_material", 0.35
    exps = [math.exp(v - m) for v in vals]
    s = sum(exps) or 1.0
    probs = [e / s for e in exps]
    best_i = max(range(len(_ROLE_ORDER)), key=lambda i: probs[i])
    return _ROLE_ORDER[best_i], float(round(probs[best_i], 4))


def resolve_asset_roles(
    *,
    assets: list[dict[str, Any]],
    user_text: str = "",
    structural_hints_by_asset_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``{"assets": [{path, type, role, confidence, id?}, ...]}``."""
    hints_map = structural_hints_by_asset_id or {}
    resolved: list[dict[str, Any]] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or a.get("url") or "").strip()
        ext_type = str(a.get("type") or "").strip().lower() or "file"
        aid = str(a.get("id") or "").strip()
        fname = _basename(path)
        label = f"{path} {fname}"
        combined = f"{label} {user_text}"
        raw: dict[AssetRole, float] = {r: 0.0 for r in _ROLE_ORDER}
        for role in _ROLE_ORDER:
            raw[role] += _type_scores(ext_type)[role]
            raw[role] += _keyword_scores(label)[role]
            raw[role] += _mention_scores(user_text, path)[role]
            raw[role] += _structural_scores(hints_map.get(aid) if aid else None)[role]
        role, conf = _softmax_pick(raw)
        item: dict[str, Any] = {
            "path": path,
            "type": ext_type,
            "role": role,
            "confidence": conf,
        }
        if fname:
            item["file_name"] = fname
        if aid:
            item["id"] = aid
        so = str(a.get("source_origin") or "").strip()
        if so:
            item["source_origin"] = so
        resolved.append(item)
    _refine_pdf_roles_for_template_form_intent(resolved, user_text)
    return {"assets": resolved}


def format_pipeline_asset_roles_for_system_prompt(
    payload: Mapping[str, Any] | dict[str, Any],
) -> str:
    """Serialize ``pipeline_asset_roles`` for the orchestrator system prompt.

    Uses compact JSON (no indent) so long URL lists stay within context while **all** assets
    remain included. The old prompt truncated a single JSON blob at 12k chars, which hid
    everything after the first ~10 BPMN files.
    """
    if not isinstance(payload, dict):
        try:
            blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            blob = str(payload)
        return blob if len(blob) <= _MAX_ASSET_ROLES_PROMPT_CHARS else blob[:_MAX_ASSET_ROLES_PROMPT_CHARS] + "\n…"

    def _compact(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    blob = _compact(payload)
    if len(blob) <= _MAX_ASSET_ROLES_PROMPT_CHARS:
        return blob

    assets = payload.get("assets")
    if not isinstance(assets, list):
        return blob[:_MAX_ASSET_ROLES_PROMPT_CHARS] + "\n…"

    trimmed: list[dict[str, Any]] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        d = dict(a)
        p = d.get("path")
        if isinstance(p, str) and len(p) > _PATH_SOFT_CAP:
            half = _PATH_SOFT_CAP // 2
            d["path"] = p[:half] + "…" + p[-half:]
        trimmed.append(d)
    blob2 = _compact({"assets": trimmed})
    if len(blob2) <= _MAX_ASSET_ROLES_PROMPT_CHARS:
        logger.warning(
            "pipeline_asset_roles: compact JSON exceeded %d chars; applied per-path soft cap (%d) — all %d assets kept",
            _MAX_ASSET_ROLES_PROMPT_CHARS,
            _PATH_SOFT_CAP,
            len(trimmed),
        )
        return blob2

    logger.warning(
        "pipeline_asset_roles: still over budget after path trim; hard truncating prompt blob (assets=%d)",
        len(trimmed),
    )
    return blob2[:_MAX_ASSET_ROLES_PROMPT_CHARS] + "\n…"
