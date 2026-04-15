"""Tests for strict pipeline_outputs invariants (spec §6.2)."""

from __future__ import annotations

from app.executor.execution_state_validator import strict_pipeline_state_violations


def test_no_violation_when_outputs_present() -> None:
    roles = {
        "assets": [
            {"role": "template_candidate", "type": "pdf", "path": "x"},
            {"type": "bpmn", "path": "y.bpmn"},
            {"role": "reference_material", "type": "pdf", "path": "z.pdf"},
        ]
    }
    po = {
        "template_schema": {"cells": []},
        "process_facts": {"nodes": []},
        "reference_facts": {"sections": []},
    }
    assert strict_pipeline_state_violations(
        pipeline_asset_roles=roles,
        pipeline_outputs=po,
    ) == []


def test_violations_when_keys_missing() -> None:
    roles = {
        "assets": [
            {"role": "template_candidate", "type": "pdf"},
            {"type": "bpmn"},
            {"role": "reference_material", "type": "pdf"},
        ]
    }
    v = strict_pipeline_state_violations(pipeline_asset_roles=roles, pipeline_outputs={})
    assert len(v) == 3
    assert any("template_schema" in x for x in v)
    assert any("process_facts" in x for x in v)
    assert any("reference_facts" in x for x in v)
