"""Tests for deliverable form field sanitization."""

from __future__ import annotations

import json

from app.output_utils import (
    ensure_trailing_newline,
    sanitize_deliverable_form_fields,
    unwrap_output_wrapper_result_text,
)


def test_sanitize_replaces_bpmn_path_in_draft_file_with_xlsx_url() -> None:
    manifest = [
        {
            "name": "out.xlsx",
            "public_url": "https://cdn.example/deliverable.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    ]
    data = {"draft_file": "https://storage/in.bpmn", "title": "x"}
    out = sanitize_deliverable_form_fields(data, file_manifest=manifest)
    assert out["draft_file"] == "https://cdn.example/deliverable.xlsx"
    assert out["title"] == "x"


def test_unwrap_output_suffix_keeps_prefix_and_appends_result() -> None:
    prose = "모든 실행 기록을 종합하여 분석하겠습니다.\n\n"
    inner = "다시 조회한 결과입니다.\n줄2"
    blob = '{"output": {"status": "SUCCESS", "result": ' + json.dumps(inner) + "}}"
    out = unwrap_output_wrapper_result_text(prose + blob)
    assert out == "모든 실행 기록을 종합하여 분석하겠습니다.\n\n" + inner


def test_unwrap_output_whole_json_returns_result() -> None:
    inner = "plain 답변"
    raw = '{"output": {"status": "SUCCESS", "result": "plain 답변"}}'
    assert unwrap_output_wrapper_result_text(raw) == inner


def test_unwrap_output_plain_text_unchanged() -> None:
    s = "그냥 텍스트만 있습니다."
    assert unwrap_output_wrapper_result_text(s) == s


def test_ensure_trailing_newline() -> None:
    assert ensure_trailing_newline("a") == "a\n"
    assert ensure_trailing_newline("a\n") == "a\n"
    assert ensure_trailing_newline("") == "\n"


def test_sanitize_fills_empty_draft_file_with_xlsx_url() -> None:
    manifest = [
        {
            "name": "out.xlsx",
            "public_url": "https://cdn.example/deliverable.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    ]
    data = {"draft_file": "", "title": "x"}
    out = sanitize_deliverable_form_fields(data, file_manifest=manifest)
    assert out["draft_file"] == "https://cdn.example/deliverable.xlsx"
    assert out["title"] == "x"
