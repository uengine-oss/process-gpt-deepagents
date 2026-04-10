"""Ensure pipeline_asset_roles is not truncated in a way that drops late assets."""

from __future__ import annotations

import json

from app.executor.asset_role_resolver import (
    format_pipeline_asset_roles_for_system_prompt,
    resolve_asset_roles,
)


def test_format_includes_all_assets_many_long_urls() -> None:
    """Regression: old 12k system-prompt cut kept only leading BPMNs; PDFs must remain."""
    long = "http://example.com/" + "x" * 400 + "/file.bpmn"
    assets = []
    for i in range(10):
        assets.append({"id": f"asset_{i}", "path": f"{long}_{i}.bpmn", "type": "bpmn"})
    for i in range(10, 19):
        assets.append({"id": f"asset_{i}", "path": f"{long}_{i}.pdf", "type": "pdf"})
    payload = resolve_asset_roles(assets=assets, user_text="양식")
    blob = format_pipeline_asset_roles_for_system_prompt(payload)
    assert "asset_18" in blob
    assert "asset_10" in blob
    assert ".pdf" in blob
    parsed = json.loads(blob)
    assert len(parsed["assets"]) == 19


def test_compact_smaller_than_indented() -> None:
    payload = {
        "assets": [
            {"id": "asset_0", "path": "https://a.com/x.pdf", "type": "pdf", "role": "x", "confidence": 0.9}
        ]
    }
    compact = format_pipeline_asset_roles_for_system_prompt(payload)
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(compact) < len(pretty)
