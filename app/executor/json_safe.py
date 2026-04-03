"""JSON-serialisable coercion for event payloads."""

from __future__ import annotations

import json
from typing import Any


def _json_safe(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable structures.

    - dict/list/tuple: recurse into elements
    - other non-serialisable objects: fall back to str()
    """
    try:
        json.dumps(value)
        return value
    except TypeError:
        pass

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    # Fallback: string representation
    return str(value)
