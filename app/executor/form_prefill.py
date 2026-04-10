from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def loose_json_loads(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object parser for LLM outputs.

    - Strips a single markdown code-fence block if present.
    - If the whole string is not JSON, tries to parse the first {...} span.
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    s = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n", "", s)
    s = re.sub(r"\n```$", "", s)
    s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = _JSON_OBJECT_RE.search(s)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _iter_nested_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nested_dicts(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_nested_dicts(it)


@dataclass(frozen=True)
class _Candidate:
    score: int
    data: dict[str, Any]


def prefill_form_data_from_structured_outputs(
    current: dict[str, Any] | None,
    *,
    field_keys: list[str],
    structured_domain_outputs_by_job: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill missing form fields using structured subagent tool outputs."""
    out: dict[str, Any] = dict(current or {})
    if not field_keys or not structured_domain_outputs_by_job:
        return out

    aliases: dict[str, list[str]] = {
        "record_date": ["record_date", "trade_date", "date", "bizdate", "as_of"],
        "stock_code": ["stock_code", "stockCode", "itemCode", "code", "symbol"],
        "stock_name": ["stock_name", "stockName", "name"],
        "closing_price": ["closing_price", "close_price", "closePrice", "lastClosePrice", "value"],
    }

    candidates: list[_Candidate] = []
    for d in _iter_nested_dicts(structured_domain_outputs_by_job):
        picked = d.get("parsed_stdout") if isinstance(d.get("parsed_stdout"), dict) else None
        if picked is None:
            picked = d
        if not isinstance(picked, dict):
            continue
        score = 0
        for fk in field_keys:
            if fk in picked and picked.get(fk) not in (None, "", []):
                score += 3
            for sk in aliases.get(fk, []):
                if sk in picked and picked.get(sk) not in (None, "", []):
                    score += 1
        if score:
            candidates.append(_Candidate(score=score, data=picked))

    if not candidates:
        return out

    best = sorted(candidates, key=lambda c: c.score, reverse=True)[0].data
    for fk in field_keys:
        if out.get(fk) not in (None, "", []):
            continue
        v = best.get(fk)
        if v not in (None, "", []):
            out[fk] = v
            continue
        for sk in aliases.get(fk, []):
            vv = best.get(sk)
            if vv not in (None, "", []):
                out[fk] = vv
                break
    return out

