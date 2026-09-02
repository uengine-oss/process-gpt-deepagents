from __future__ import annotations

import json

import pytest

from core.deterministic.replay import (
    _enabled_servers,
    _parameter_values,
    _render_form_output,
    _run_code,
)


def test_extracts_business_parameters_from_korean_workitem() -> None:
    """이름표는 생성 단계의 **관측**에서 온다 — 도메인별 별칭표를 코드에 두지 않는다.

    아래 spec 은 completion 이 표본 3건의 지시문을 대조해 남긴 모양이다. 값 앞에
    붙던 말은 `label_position="before"`, `Galaxy 상품의` 처럼 뒤에 붙던 말은
    `"after"` 로 적힌다.
    """
    spec = {
        "parameters": [
            {"name": "product_name", "type": "string", "example": "iPhone",
             "label": "상품", "label_position": "after"},
            {"name": "stock_increment", "type": "integer", "example": 60,
             "label": "수량", "label_position": "before"},
            {"name": "new_stock", "type": "integer", "example": 80,
             "label": "재고", "label_position": "before"},
            {"name": "reason", "type": "string", "example": "발주 입고",
             "label": "사유", "label_position": "before"},
        ]
    }
    query = (
        "Galaxy 상품의 발주 입고를 반영하라. 입고 수량은 215, "
        "반영 후 재고는 250, 변경 사유는 발주 입고."
    )
    assert _parameter_values(query, spec) == {
        "product_name": "Galaxy",
        "stock_increment": 215,
        "new_stock": 250,
        "reason": "발주 입고",
    }


def test_only_enabled_mcp_servers_are_forwarded() -> None:
    assert _enabled_servers(
        {
            "mcpServers": {
                "on": {"command": "uvx", "enabled": True},
                "off": {"command": "uvx", "enabled": False},
            }
        }
    ) == {"mcpServers": {"on": {"command": "uvx"}}}


@pytest.mark.asyncio
async def test_executes_generated_code_as_subprocess_without_llm() -> None:
    code = """
import json, sys
payload = json.loads(sys.argv[1])
print(json.dumps({"ok": True, "results": [{"tool": "fixture", "data": payload}]}))
"""
    out = await _run_code(code, {"value": 42}, {"mcpServers": {}})
    assert out == {"ok": True, "results": [{"tool": "fixture", "data": {"value": 42}}]}


# ---------------------------------------------------------------------------
# 작업 이력 해석 — 실행 런타임에 매이지 않는다
# ---------------------------------------------------------------------------

from core.deterministic import work_history as wh
from core.deterministic.replay import (  # noqa: E402
    _needs_workspace,
    _structured_fields,
    _parse_result,
    _previous_undo_plan,
    try_deterministic_execution,
)


def _event(tool, args, timestamp="t1", event_type="tool_usage_finished"):
    return {"event_type": event_type, "timestamp": timestamp,
            "data": {"tool_name": tool, "args": args}}


def test_runtime_reads_shell_and_file_work_not_only_tool_calls() -> None:
    """되돌릴 대상은 MCP 호출만이 아니다. 셸·파일 작업도 이력에서 읽힌다."""
    trace = wh.normalize_events([
        _event("read_file", {"file_path": "/skills/inv/SKILL.md"}, "t1"),
        _event("execute", {"command": "python3 load.py"}, "t2"),
        _event("write_file", {"file_path": "/workspace/out.txt", "content": "x"}, "t3"),
        _event("db_exec", {"sql": "SELECT 1"}, "t4"),
        _event("db_exec", {"sql": "UPDATE t SET a = 1"}, "t5"),
    ])
    assert [a.kind for a in trace if a.has_effect] == [wh.SHELL, wh.FILE_WRITE, wh.MCP_CALL]


def test_other_runtimes_event_types_are_not_filtered_out() -> None:
    """`tool_usage_finished` 로 못박으면 다른 러너의 이력이 통째로 사라진다."""
    trace = wh.normalize_events([
        _event("db_exec", {"sql": "UPDATE t SET a = 1"}, "t1", event_type="tool_end"),
    ])
    assert len(trace) == 1


class _Query:
    """Supabase 쿼리 빌더 스텁. 체이닝만 흉내 낸다."""

    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self._rows})()


def test_the_undo_plan_comes_from_observation() -> None:
    """되돌릴 내용은 관측이 정한다 — INSERT 한 행은 넣은 값 전부를 조건으로 지운다."""
    tables = {
        "todolist": [{"id": "prev", "rework_count": 0, "updated_at": "t"}],
        "events": [
            _event("read_file", {"file_path": "/skills/inv/SKILL.md"}, "t1"),
            _event("db_exec", {"sql": "INSERT INTO ledger (applicant, amount) VALUES (\'김철수\', 100)"}, "t2"),
        ],
    }
    db = type("DB", (), {"table": lambda self, name: _Query(tables[name])})()

    plan = _previous_undo_plan(db, {"proc_inst_id": "pi", "activity_id": "act", "rework_count": 1})
    assert plan is not None
    steps = plan["undo_steps"]
    assert len(steps) == 1                       # 읽기는 되돌릴 대상이 아니다
    assert steps[0]["args"]["sql"] == (
        "DELETE FROM ledger WHERE applicant = \'김철수\' AND amount = 100"
    )
    assert "ledger" in steps[0]["describes"]     # 무엇을 되돌렸는지 남는다


def test_an_irreversible_action_yields_no_plan() -> None:
    """셸 실행은 되돌릴 방법이 이력에 없다. 절반만 되돌리느니 계획을 만들지 않는다."""
    tables = {
        "todolist": [{"id": "prev", "rework_count": 0, "updated_at": "t"}],
        "events": [
            _event("db_exec", {"sql": "INSERT INTO ledger (a) VALUES (1)"}, "t1"),
            _event("execute", {"command": "python3 load.py --qty 20"}, "t2"),
        ],
    }
    db = type("DB", (), {"table": lambda self, name: _Query(tables[name])})()
    assert _previous_undo_plan(db, {"proc_inst_id": "pi", "activity_id": "act", "rework_count": 1}) is None


def test_activity_without_effects_yields_no_undo_input() -> None:
    tables = {
        "todolist": [{"id": "prev", "rework_count": 0, "updated_at": "t"}],
        "events": [_event("db_exec", {"sql": "SELECT 1"}, "t1")],
    }
    db = type("DB", (), {"table": lambda self, name: _Query(tables[name])})()
    assert _previous_undo_plan(db, {"proc_inst_id": "pi", "activity_id": "act", "rework_count": 1}) is None


# ---------------------------------------------------------------------------
# 실행 위치와 폴백
# ---------------------------------------------------------------------------

# 생성 코드의 실행 골격. completion 이 만드는 것과 같은 모양이어야 판정이 성립한다.
_SKELETON = '''
import asyncio


async def run_shell(command, cwd="", timeout_s=300):
    return {"command": command}


async def write_file(path, content):
    return {"path": path}


async def call_tool(server_key, tool_name, args, timeout_s=60):
    return {"tool": tool_name}


async def run(inputs, timeout_s=60):
    results = []
{steps}
    return results
'''


def test_shell_and_file_code_requires_the_workspace() -> None:
    """셸·파일 작업이 든 코드는 에이전트가 일했던 워크스페이스에서 돌아야 한다."""
    shelly = _SKELETON.replace("{steps}", '    results.append(await run_shell("ls", ""))')
    filey = _SKELETON.replace("{steps}", '    results.append(await write_file("a", "b"))')
    assert _needs_workspace(shelly) is True
    assert _needs_workspace(filey) is True


def test_mcp_only_code_runs_without_the_sandbox() -> None:
    """골격은 셸·파일 헬퍼를 늘 **정의**한다. 정의만 보고 샌드박스를 요구하면
    Docker 가 없는 환경에서 MCP 전용 코드까지 통째로 폴백된다."""
    mcp_only = _SKELETON.replace(
        "{steps}", '    results.append(await call_tool("pg", "x", {}))'
    )
    assert "async def run_shell" in mcp_only   # 정의는 들어 있다
    assert _needs_workspace(mcp_only) is False  # 그래도 호출은 없다


def test_unparsable_code_is_pushed_to_the_sandbox() -> None:
    assert _needs_workspace("def run(:\n") is True


def test_result_envelope_must_report_success() -> None:
    assert _parse_result('{"ok": true, "results": []}', "boom")["ok"] is True
    with pytest.raises(RuntimeError):
        _parse_result("", "결과 없음")
    with pytest.raises(RuntimeError):
        _parse_result("not json", "깨진 출력")
    with pytest.raises(RuntimeError):
        _parse_result('{"ok": false}', "실패 보고")


class _Recorder:
    """mcp_python_code 한 행만 돌려주는 최소 스텁."""

    def __init__(self, record):
        self._record = record

    def table(self, _name):
        return _Query([self._record])


@pytest.mark.asyncio
async def test_execution_failure_falls_back_to_the_agent(monkeypatch) -> None:
    """고착화는 최적화다. 실패하면 워크아이템을 멈추지 않고 에이전트에게 넘긴다."""
    import core.deterministic.replay as replay

    record = {"code": "raise SystemExit(1)", "parameters": {"parameters": []},
              "deactivated_at": None, "compensation": None}
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: _Recorder(record))

    async def _boom(*args, **kwargs):
        raise RuntimeError("실행 실패")

    monkeypatch.setattr(replay, "_run_code", _boom)
    row = {"proc_def_id": "pd", "activity_id": "act", "tenant_id": "tn", "query": "", "id": "w1"}
    assert await try_deterministic_execution(row, {"mcpServers": {}}) is None


@pytest.mark.asyncio
async def test_undo_failure_hands_the_whole_rework_to_the_agent(monkeypatch) -> None:
    """되돌리기가 실패했다면 어디까지 남았는지 모른다. 그 위에 재실행을 얹지 않는다."""
    import core.deterministic.replay as replay

    record = {"code": "print(1)", "parameters": {"parameters": []},
              "deactivated_at": None, "compensation": "print(1)"}
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: _Recorder(record))
    monkeypatch.setattr(replay, "_previous_undo_plan", lambda db, row: {"undo_steps": [{"kind": "file_delete", "path": "/x"}]})

    async def _boom(*args, **kwargs):
        raise RuntimeError("보상 실패")

    monkeypatch.setattr(replay, "_run_code", _boom)
    row = {"proc_def_id": "pd", "activity_id": "act", "tenant_id": "tn",
           "query": "", "id": "w1", "rework_count": 1}
    assert await try_deterministic_execution(row, {"mcpServers": {}}) is None


@pytest.mark.asyncio
async def test_undo_succeeds_then_forward_fails_keeps_the_undo(monkeypatch) -> None:
    """되돌리기까지는 끝났다면 그 사실을 남기고 재실행만 에이전트가 맡는다."""
    import core.deterministic.replay as replay

    record = {"code": "print(1)", "parameters": {"parameters": []},
              "deactivated_at": None, "compensation": "print(1)"}
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: _Recorder(record))
    monkeypatch.setattr(replay, "_previous_undo_plan", lambda db, row: {"undo_steps": [{"kind": "file_delete", "path": "/x"}]})

    calls = {"n": 0}

    async def _first_ok_then_fail(code, inputs, config, tenant_id=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "results": [{"kind": "shell", "undone": True}]}
        raise RuntimeError("재실행 실패")

    monkeypatch.setattr(replay, "_run_code", _first_ok_then_fail)
    row = {"proc_def_id": "pd", "activity_id": "act", "tenant_id": "tn",
           "query": "", "id": "w1", "rework_count": 1}
    result = await try_deterministic_execution(row, {"mcpServers": {}})
    assert result is not None
    assert result.mode == "deterministic-undo-only"
    assert result.continue_to_llm is True
    assert result.output["undo_results"] == [{"kind": "shell", "undone": True}]


# ---------------------------------------------------------------------------
# 분류 규칙 — completion 의 생성기와 같은 결론을 내야 한다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args,expected", [
    ("execute", {"command": "python3 import.py"}, wh.SHELL),
    ("bash", {"command": "ls"}, wh.SHELL),
    ("run_it", {"command": "make build", "cwd": "/workspace"}, wh.SHELL),
    ("write_file", {"file_path": "/workspace/out.csv", "content": "a,b"}, wh.FILE_WRITE),
    ("edit_file", {"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"}, wh.FILE_WRITE),
    ("read_file", {"file_path": "/skills/inventory/SKILL.md"}, wh.SKILL_READ),
    ("read_file", {"file_path": "/workspace/data.json"}, wh.FILE_READ),
    ("ls", {"path": "/workspace"}, wh.INSPECT),
    ("task", {"subagent_type": "researcher", "description": "조사"}, wh.DELEGATE),
    ("write_todos", {"todos": []}, wh.PLAN),
    ("mem0", {"query": "x"}, wh.INTERNAL),
    ("db_exec", {"sql": "SELECT 1"}, wh.INSPECT),
    ("db_exec", {"sql": "UPDATE t SET a = 1"}, wh.MCP_CALL),
    ("send_email_tool", {"to": "a@b.com"}, wh.MCP_CALL),
    ("mcp__fs__read_file", {"path": "/skills/x/SKILL.md"}, wh.SKILL_READ),
    ("sandbox.execute", {"command": "echo hi"}, wh.SHELL),
    ("printer", {"command": "print", "copies": 3, "tray": "A4"}, wh.MCP_CALL),
])
def test_classification_matches_the_generator_side(tool, args, expected) -> None:
    """생성기(completion)와 실행기(여기)의 분류가 어긋나면, 만든 코드와 되돌릴 입력이
    서로 다른 세계를 가리킨다. 같은 표를 양쪽에 고정한다."""
    assert wh.classify(tool, args) == expected


# ---------------------------------------------------------------------------
# 지시문의 구조화 입력([InputData] JSON)에서 값 뽑기
# ---------------------------------------------------------------------------

_INPUTDATA_QUERY = """[Description]
생성된 RFQ의 품목, 수량, 공급사 등의 내용을 정리하여 구매 승인을 요청하는 작업이다.

[InputData]
{"mcp_replen_proc_task_lead_form": {"supplier_name": "", "quote_item_name": "SKU-A-003",
 "purchase_quantity": 0, "replenish_item_name": "SKU-A-003"}}"""


def test_json_keys_are_never_mistaken_for_values() -> None:
    """지시문의 따옴표를 그대로 긁으면 JSON **키 이름**이 먼저 잡힌다.

    그대로 두면 `request_approval(po_id="mcp_replen_proc_task_lead_form")` 처럼
    엉뚱한 값으로 실제 도구를 호출한다 — 조용히 틀리는 것이 아니라 확신 있게 틀린다.
    """
    spec = {"parameters": [{"name": "po_id_1", "type": "string", "example": "SKU-A-001"}]}
    assert _parameter_values(_INPUTDATA_QUERY, spec) == {"po_id_1": "SKU-A-003"}


def test_structured_field_is_matched_by_name() -> None:
    """구조화 입력에 같은 이름의 칸이 있으면 그 값이 가장 확실하다."""
    spec = {"parameters": [{"name": "purchase_quantity", "type": "integer", "example": 5}]}
    assert _parameter_values(_INPUTDATA_QUERY, spec) == {"purchase_quantity": 0}


def test_trailing_slot_number_is_stripped_when_matching_fields() -> None:
    """생성기가 붙인 자리 번호(`_1`)는 지시문의 칸 이름에 없다."""
    spec = {"parameters": [{"name": "supplier_name_1", "type": "string", "example": "A사"}]}
    # supplier_name 은 비어 있으므로 이름 일치로는 못 찾고, 유일한 문자열 값으로 떨어진다.
    assert _parameter_values(_INPUTDATA_QUERY, spec) == {"supplier_name_1": "SKU-A-003"}


def test_structured_fields_flattens_nested_json() -> None:
    fields = _structured_fields(_INPUTDATA_QUERY)
    assert fields["quote_item_name"] == "SKU-A-003"
    assert fields["purchase_quantity"] == 0
    assert "mcp_replen_proc_task_lead_form" not in fields  # 컨테이너 키는 값이 아니다


# ---------------------------------------------------------------------------
# 실행 식별자 — 지시문이 아니라 워크아이템 행에서 읽는다
# ---------------------------------------------------------------------------

_LEDGER_QUERY = """[Description]
경비 대장에 지출 내역을 1건 등록한다.

[InputData]
{"expense_resolution_process_apply_expense_form": {"applicant": "최민석",
 "account_code": "TRV", "amount": 38000, "used_at": "2026-09-05",
 "purpose": "본사 회의 택시비"}}"""

_LEDGER_ROW = {
    "id": "020e927a-6ed8-4c3b-9d47-1c2929e67f31",
    "proc_inst_id": "expense_resolution_process.0118b1ab",
    "query": _LEDGER_QUERY,
}


def test_runtime_identifiers_come_from_the_workitem_row() -> None:
    """proc_inst_id·todo_id 는 지시문에 없다. 행에서 읽지 않으면 되찾을 길이 없다."""
    spec = {
        "parameters": [
            {"name": "applicant", "type": "string", "example": "박지은"},
            {"name": "proc_inst_id", "type": "string", "example": "expense…5ee72ce5",
             "runtime": "proc_inst_id"},
            {"name": "todo_id", "type": "string", "example": "", "runtime": "id"},
        ]
    }
    assert _parameter_values(_LEDGER_QUERY, spec, _LEDGER_ROW) == {
        "applicant": "최민석",
        "proc_inst_id": "expense_resolution_process.0118b1ab",
        "todo_id": "020e927a-6ed8-4c3b-9d47-1c2929e67f31",
    }


def test_identifier_slots_no_longer_swallow_business_values() -> None:
    """`runtime` 이 없던 시절 이 자리들은 위치 폴백으로 떨어졌다.

    그 결과 proc_inst_id 컬럼에 신청자 이름이, todo_id 컬럼에 계정과목이 들어갔다.
    조용히 틀리는 것이 아니라 확신 있게 틀렸다.
    """
    without_binding = {
        "parameters": [
            {"name": "applicant", "type": "string", "example": "박지은"},
            {"name": "query_7", "type": "string", "example": "expense…5ee72ce5"},
            {"name": "query_8", "type": "string", "example": ""},
        ]
    }
    wrong = _parameter_values(_LEDGER_QUERY, without_binding, _LEDGER_ROW)
    assert wrong["query_7"] == "최민석"  # 예전 동작을 고정해 둔다

    with_binding = {
        "parameters": [
            {"name": "applicant", "type": "string", "example": "박지은"},
            {"name": "proc_inst_id", "type": "string", "example": "…", "runtime": "proc_inst_id"},
            {"name": "todo_id", "type": "string", "example": "", "runtime": "id"},
        ]
    }
    fixed = _parameter_values(_LEDGER_QUERY, with_binding, _LEDGER_ROW)
    assert fixed["proc_inst_id"] == _LEDGER_ROW["proc_inst_id"]
    assert fixed["todo_id"] == _LEDGER_ROW["id"]


def test_missing_identity_falls_back_to_the_agent() -> None:
    """행에 그 식별자가 없으면 굳힌 코드를 쓰지 않는다.

    호출부가 이 예외를 폴백 신호로 받아 DeepAgents 실행으로 되돌린다 — 빈 값으로
    실제 도구를 부르는 것보다 낫다.
    """
    spec = {"parameters": [{"name": "todo_id", "type": "string", "example": "x",
                            "runtime": "id"}]}
    with pytest.raises(ValueError):
        _parameter_values(_LEDGER_QUERY, spec, {"proc_inst_id": "pi"})


# ---------------------------------------------------------------------------
# 폼 산출물 — 도구 호출만 재현하면 워크아이템이 빈 산출물로 완료된다
# ---------------------------------------------------------------------------

_RESULTS = [{"kind": "mcp_call", "tool": "execute_sql", "server": "supabase", "data": None}]


def test_constant_field_is_reproduced_as_is() -> None:
    template = {"form_id": "f", "fields": {"status": {"const": "등록 완료"}}}
    assert _render_form_output(template, {"applicant": "정수연"}, _RESULTS) == {
        "f": {"status": "등록 완료"}
    }


def test_templated_field_is_rendered_with_this_run_inputs() -> None:
    """표본의 값이 아니라 **이번 실행의** 입력으로 렌더해야 한다."""
    template = {
        "form_id": "f",
        "fields": {"ledger_result": {"template": "신청자 ${applicant}, 금액 ${amount} 등록"}},
    }
    assert _render_form_output(
        template, {"applicant": "정수연", "amount": 42000}, _RESULTS
    ) == {"f": {"ledger_result": "신청자 정수연, 금액 42000 등록"}}


def test_unreproducible_field_falls_back_to_what_actually_happened() -> None:
    """표본마다 문구가 달랐던 필드는 굳히지 않는다.

    표본 하나의 문장을 굳히면 그 표본의 사실(그때의 신청자)이 다음 실행에 남는다.
    지어내지 않고 이번 실행에서 관측된 것만 적는다.
    """
    template = {"form_id": "f", "fields": {"ledger_result": None}}
    rendered = _render_form_output(template, {"applicant": "정수연"}, _RESULTS)
    text = rendered["f"]["ledger_result"]
    assert "execute_sql(supabase)" in text
    assert "정수연" in text
    assert "LLM 추론 없이" in text


def test_a_broken_field_does_not_sink_the_whole_form() -> None:
    """렌더가 깨지는 필드 하나 때문에 폼 전체를 포기하지 않는다."""
    template = {
        "form_id": "f",
        "fields": {
            "ok_field": {"template": "신청자 ${applicant}"},
            "broken": {"template": "${사라진_파라미터}"},
        },
    }
    rendered = _render_form_output(template, {"applicant": "정수연"}, _RESULTS)
    assert rendered["f"]["ok_field"] == "신청자 정수연"
    assert "LLM 추론 없이" in rendered["f"]["broken"]


def test_old_records_without_a_template_yield_no_form_output() -> None:
    """이 변경 이전에 굳은 레코드에는 템플릿이 없다. 억지로 지어내지 않는다."""
    assert _render_form_output(None, {"a": "b"}, _RESULTS) is None
    assert _render_form_output({"fields": {}}, {}, _RESULTS) is None


def test_the_execution_card_is_not_a_submittable_result() -> None:
    """실행 카드의 crew_type 은 `result` 가 아니어야 한다.

    프론트는 `result` 를 "제출할 수 있는 산출물"로 읽어 채택 버튼을 붙이고
    (`EventTimeline.isSubmittableTask`), 생성기는 표본의 폼 산출물을 그 값으로 찾는다.
    이 카드가 싣는 것은 폼 값이 아니라 실행 방식과 도구 호출 결과다. `result` 로 두면
    채택 버튼이 둘이 되고, 그중 하나는 누르는 순간 폼에 실행 로그 JSON 을 밀어 넣는다.
    """
    from executor import _DETERMINISTIC_CARD_CREW_TYPE

    assert _DETERMINISTIC_CARD_CREW_TYPE != "result"


# --------------------------------------------------------------------------
# 앞 액티비티의 산출물에서 읽는 자리
# --------------------------------------------------------------------------
#
# 값은 액티비티 경계를 넘어서도 흐른다. 에이전트는 그것을
# `get_related_workitem_outputs` 로 읽어 썼다. 앞 워크아이템의 완료 시각처럼 지시문에
# 적히지 않는 값이 여기 해당하고, 그런 자리가 하나라도 있으면 지금까지는 고착화 자체가
# 보류됐다.

_RELATED = [
    {
        "workitemId": "b221404c",
        "procInstId": "expense_resolution_process.7203b5a3",
        "activityId": "register_expense_ledger",
        "activityName": "경비 대장 등록",
        "endDate": "2026-09-01T11:55:47.027549",
        "output": {
            "expense_resolution_process_register_expense_ledger_form": {
                "ledger_result": "expense_ledger INSERT 1건 실행 완료",
            }
        },
    },
    {
        "workitemId": "a0000000",
        "procInstId": "expense_resolution_process.7203b5a3",
        "activityId": "apply_expense",
        "activityName": "경비 신청",
        "endDate": "2026-09-01T11:40:00.000000",
        "output": {"expense_resolution_process_apply_expense_form": {"applicant": "최민석"}},
    },
]

_UPSTREAM_SPEC = {
    "parameters": [
        {"name": "applicant", "type": "string", "example": "박지은"},
        {
            "name": "ledger_time",
            "type": "string",
            "example": "2026-09-01T02:46:44Z",
            "upstream": {"activity_id": "register_expense_ledger", "path": ["endDate"]},
        },
    ]
}


def test_upstream_parameters_are_read_from_the_previous_activity() -> None:
    values = _parameter_values(_LEDGER_QUERY, _UPSTREAM_SPEC, _LEDGER_ROW, _RELATED)
    assert values["ledger_time"] == "2026-09-01T11:55:47.027549"
    assert values["applicant"] == "최민석"


def test_upstream_parameters_reach_into_the_previous_form_output() -> None:
    spec = {
        "parameters": [{
            "name": "ledger_result",
            "type": "string",
            "example": "…",
            "upstream": {
                "activity_id": "register_expense_ledger",
                "path": [
                    "output",
                    "expense_resolution_process_register_expense_ledger_form",
                    "ledger_result",
                ],
            },
        }]
    }
    values = _parameter_values(_LEDGER_QUERY, spec, _LEDGER_ROW, _RELATED)
    assert values["ledger_result"] == "expense_ledger INSERT 1건 실행 완료"


def test_a_missing_upstream_output_falls_back_to_the_agent() -> None:
    """앞 액티비티가 아직 산출물을 내지 않았으면 굳힌 코드를 쓰지 않는다.

    지시문을 뒤져 비슷한 값을 대신 채우면, 그 값으로 실제 도구를 부른다. 호출부가 이
    예외를 폴백 신호로 받아 DeepAgents 실행으로 되돌린다.
    """
    with pytest.raises(ValueError):
        _parameter_values(_LEDGER_QUERY, _UPSTREAM_SPEC, _LEDGER_ROW, [])
    with pytest.raises(ValueError):
        _parameter_values(_LEDGER_QUERY, _UPSTREAM_SPEC, _LEDGER_ROW, [_RELATED[1]])


def test_upstream_binding_does_not_scan_the_instruction() -> None:
    """이 자리는 지시문을 보지 않는다 — 위치 폴백이 엉뚱한 값을 집을 여지를 없앤다."""
    spec = {
        "parameters": [{
            "name": "ledger_time",
            "type": "string",
            "example": "…",
            "upstream": {"activity_id": "register_expense_ledger", "path": ["endDate"]},
        }]
    }
    values = _parameter_values(_LEDGER_QUERY, spec, _LEDGER_ROW, _RELATED)
    # 지시문의 첫 문자열 값(신청자)이 아니라 앞 액티비티의 완료 시각이다.
    assert values["ledger_time"] == "2026-09-01T11:55:47.027549"


def test_related_outputs_are_only_fetched_when_a_parameter_needs_them() -> None:
    """대부분의 활동은 지시문과 워크아이템 행만으로 채워진다. 매번 조회하지 않는다."""
    from core.deterministic.replay import _needs_upstream

    assert _needs_upstream(_UPSTREAM_SPEC) is True
    assert _needs_upstream({"parameters": [{"name": "applicant", "type": "string"}]}) is False
    assert _needs_upstream(None) is False


def test_instruction_placeholders_do_not_hide_the_structured_input() -> None:
    """지시문의 [Instruction] 에는 `{used_at}` 같은 자리표시자가 흔히 들어 있다.

    첫 `{` 부터 마지막 `}` 까지를 한 덩어리로 집으면 JSON 파싱이 통째로 실패해 구조화
    입력이 빈 것으로 보인다. 그러면 후보 문자열이 지시문의 따옴표를 순서대로 긁는 쪽으로
    떨어져 **칸 이름**(`..._form`)을 값으로 집는다.
    """
    from core.deterministic.replay import _structured_fields

    query = (
        "[Instruction]\n출력 파일 경로를 expense/{used_at}_{applicant}.md 로 고정한다.\n\n"
        '[InputData]\n{"form": {"applicant": "최민석", "used_at": "2026-09-05"}}'
    )
    assert _structured_fields(query) == {"applicant": "최민석", "used_at": "2026-09-05"}
    spec = {"parameters": [{"name": "applicant", "type": "string", "example": "박지은"}]}
    assert _parameter_values(query, spec)["applicant"] == "최민석"
