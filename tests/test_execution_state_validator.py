"""Tests for post-run native execution contract heuristics."""

from __future__ import annotations

from app.executor.execution_state_validator import validate_native_execution_contract


def test_warns_when_template_candidate_but_no_template_schema_in_blob() -> None:
    roles = {
        "assets": [
            {"id": "a0", "role": "template_candidate", "type": "pdf", "path": "http://x/t.pdf"},
        ]
    }
    w = validate_native_execution_contract(
        pipeline_asset_roles=roles,
        messages_blob="some assistant text without the keyword",
    )
    assert len(w) == 1
    assert "template_schema" in w[0]


def test_no_warning_when_template_schema_present() -> None:
    roles = {"assets": [{"role": "template_candidate", "type": "pdf", "path": "x"}]}
    w = validate_native_execution_contract(
        pipeline_asset_roles=roles,
        messages_blob='{"template_schema": {}}',
    )
    assert w == []


def test_no_warning_when_template_schema_only_in_pipeline_outputs() -> None:
    roles = {"assets": [{"role": "template_candidate", "type": "pdf", "path": "x"}]}
    w = validate_native_execution_contract(
        pipeline_asset_roles=roles,
        messages_blob="",
        pipeline_outputs={"template_schema": {"ok": True}},
    )
    assert w == []
