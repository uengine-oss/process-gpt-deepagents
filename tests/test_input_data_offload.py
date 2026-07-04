"""executor._offload_input_data / _save_step_output 유닛 테스트."""
from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# executor 모듈에서 필요한 함수만 직접 임포트하기 위해
# 무거운 의존성을 모킹한다.
_HEAVY = [
    "langgraph", "langchain_core", "langchain", "anthropic",
    "a2a", "processgpt_agent_sdk", "litellm", "supabase",
]
for _mod in _HEAVY:
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# executor를 직접 import하지 않고 함수만 추출
import importlib.util, os

_EXEC_PATH = Path(__file__).parent.parent / "executor.py"


def _load_helpers():
    """executor.py에서 _offload_input_data / _save_step_output만 추출."""
    src = _EXEC_PATH.read_text(encoding="utf-8")
    # 필요한 함수 정의만 포함하는 최소 모듈 생성
    ns: dict = {"re": re, "json": json, "Path": Path}

    # logger mock
    import logging
    ns["logger"] = logging.getLogger("test_offload")

    # WORKSPACE_ROOT를 tmp로 override
    exec(compile(src, str(_EXEC_PATH), "exec"), ns)  # noqa: S102
    return ns


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_workspace(tmp_path, monkeypatch):
    """WORKSPACE 환경변수를 tmp_path로 설정."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    return tmp_path


# ─── _offload_input_data ─────────────────────────────────────────────────────

SHORT_VALUE = "짧은값"  # 500B 미만
LONG_VALUE = "긴" * 300  # ~900B


def _make_query(input_data: dict) -> str:
    return f"[Description]\n업무 지침.\n\n[InputData]\n{json.dumps(input_data, ensure_ascii=False)}"


def offload(text: str, instance_id: str, tmp_path: Path):
    """_offload_input_data를 tmp_path workspace와 함께 실행."""
    from core.skills.tools import WORKSPACE_ROOT  # noqa: F401 — mocked by monkeypatch
    with patch("core.skills.tools.WORKSPACE_ROOT", tmp_path):
        # executor 모듈 내 함수를 직접 테스트하기 위해 import
        import executor as ex
        with patch.object(
            type(ex),  # class patch 대신 모듈 속성 패치
            "_instance_ws_dir",
            side_effect=lambda iid, sub: tmp_path / (iid or "_flat") / sub,
            create=True,
        ):
            pass
        # 직접 함수 호출 (WORKSPACE_ROOT를 tmp_path로 monkeypatch)
        import importlib
        import executor
        _orig = None
        try:
            import core.skills.tools as cst
            _orig = cst.WORKSPACE_ROOT
            cst.WORKSPACE_ROOT = tmp_path
            return executor._offload_input_data(text, instance_id)
        finally:
            if _orig is not None:
                cst.WORKSPACE_ROOT = _orig


# ─── 5.1 짧은 [InputData]: 인라인 유지, 파일 미생성 ────────────────────────────

def test_small_fields_stay_inline(tmp_workspace):
    data = {"form-A": {"field1": SHORT_VALUE}}
    text = _make_query(data)
    result = offload(text, "inst-001", tmp_workspace)

    assert "[이전 단계 입력 데이터]" in result
    assert SHORT_VALUE in result
    # 파일이 생성되지 않아야 함
    inputs_dir = tmp_workspace / "inst-001" / "inputs"
    assert not inputs_dir.exists() or not list(inputs_dir.iterdir())


# ─── 5.2 긴 [InputData]: 파일 생성 + 참조 텍스트 ─────────────────────────────

def test_large_fields_offloaded_to_file(tmp_workspace):
    data = {"form-B": {"free_input": LONG_VALUE}}
    text = _make_query(data)
    result = offload(text, "inst-002", tmp_workspace)

    assert "[이전 단계 입력 데이터]" in result
    assert "free_input" in result
    assert "/workspace/inst-002/inputs/" in result
    assert LONG_VALUE not in result  # 원본 값이 인라인에 없어야 함

    # 파일이 실제로 생성됐는지 확인
    inputs_dir = tmp_workspace / "inst-002" / "inputs"
    files = list(inputs_dir.glob("*.md"))
    assert len(files) == 1
    assert LONG_VALUE in files[0].read_text(encoding="utf-8")


# ─── 5.3 [InputData] 섹션 없으면 원본 무변경 ─────────────────────────────────

def test_no_input_data_section_unchanged(tmp_workspace):
    text = "[Description]\n업무 지침만 있고 InputData 없음."
    result = offload(text, "inst-003", tmp_workspace)
    assert result == text


# ─── 5.4 JSON 파싱 불가: 원본 무변경 ────────────────────────────────────────

def test_invalid_json_returns_original(tmp_workspace):
    text = "[Description]\n업무.\n\n[InputData]\nnot-valid-json!!!"
    result = offload(text, "inst-004", tmp_workspace)
    assert result == text


# ─── 5.5 다수 form + 다수 필드 혼합 ──────────────────────────────────────────

def test_multiple_forms_and_fields(tmp_workspace):
    data = {
        "form-X": {"short_field": SHORT_VALUE, "long_field": LONG_VALUE},
        "form-Y": {"another_long": LONG_VALUE},
    }
    text = _make_query(data)
    result = offload(text, "inst-005", tmp_workspace)

    assert "[form-X]" in result
    assert "[form-Y]" in result
    assert SHORT_VALUE in result  # 짧은 값은 인라인
    assert "long_field" in result
    assert "another_long" in result

    inputs_dir = tmp_workspace / "inst-005" / "inputs"
    files = list(inputs_dir.glob("*.md"))
    assert len(files) == 2  # 긴 필드 2개만 파일화


# ─── 5.7 instance_id 없으면 flat 경로 fallback ───────────────────────────────

def test_no_instance_id_uses_flat_path(tmp_workspace):
    data = {"form-Z": {"long_field": LONG_VALUE}}
    text = _make_query(data)
    result = offload(text, "", tmp_workspace)

    assert "/workspace/inputs/" in result
    inputs_dir = tmp_workspace / "inputs"
    assert inputs_dir.exists()
    files = list(inputs_dir.glob("*.md"))
    assert len(files) == 1


# ─── 5.6 _save_step_output: 파일 생성 ────────────────────────────────────────

def test_save_step_output_creates_file(tmp_workspace):
    import core.skills.tools as cst
    _orig = cst.WORKSPACE_ROOT
    try:
        cst.WORKSPACE_ROOT = tmp_workspace
        import executor
        executor._save_step_output("에이전트 최종 응답 텍스트", "inst-006", "activity_abc123")
    finally:
        cst.WORKSPACE_ROOT = _orig

    outputs_dir = tmp_workspace / "inst-006" / "outputs"
    files = list(outputs_dir.glob("*.md"))
    assert len(files) == 1
    assert "에이전트 최종 응답 텍스트" in files[0].read_text(encoding="utf-8")


def test_save_step_output_no_activity_id_noop(tmp_workspace):
    import core.skills.tools as cst
    _orig = cst.WORKSPACE_ROOT
    try:
        cst.WORKSPACE_ROOT = tmp_workspace
        import executor
        executor._save_step_output("텍스트", "inst-007", "")  # activity_id 없음
    finally:
        cst.WORKSPACE_ROOT = _orig

    outputs_dir = tmp_workspace / "inst-007" / "outputs"
    assert not outputs_dir.exists()
