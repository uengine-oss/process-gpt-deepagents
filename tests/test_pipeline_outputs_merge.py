"""Tests for merging task() Command results into pipeline_outputs."""

from __future__ import annotations

import pytest


def test_merge_task_command_pipeline_outputs_from_tool_message_json() -> None:
    try:
        from langchain_core.messages import ToolMessage
        from langgraph.types import Command
    except ImportError:
        pytest.skip("langgraph/langchain_core not available")

    from app.executor.pipeline_outputs_extract import merge_task_command_pipeline_outputs

    tm = ToolMessage(
        content='{"template_schema": {"cells": [1]}, "extra": "x"}',
        tool_call_id="tc1",
        name="x",
    )
    cmd = Command(update={"messages": [tm]})
    out = merge_task_command_pipeline_outputs(cmd, "any_subagent")
    assert isinstance(out, Command)
    upd = out.update
    assert isinstance(upd, dict)
    po = upd.get("pipeline_outputs")
    assert isinstance(po, dict)
    assert po.get("template_schema") == {"cells": [1]}
