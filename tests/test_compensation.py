# -*- coding: utf-8 -*-
"""되돌리기 판정. completion 의 `tests/test_compensation.py` 와 같은 표본을 쓴다.

두 서비스는 저장소가 갈려 모듈을 공유할 수 없어 규칙이 사본으로 존재한다. 파일 단위
동일성은 못박을 수 없으므로 **동작**을 같은 표본으로 못박는다.

어긋났을 때 어느 쪽이 위험한지도 적어 둔다.

- completion 이 더 엄격하면 → 보상이 저장되지 않고 재작업 전체를 에이전트가 맡는다(안전).
- 실행기(여기)가 더 엄격하면 → 계획을 만들지 않고 에이전트가 맡는다(안전).
- **실행기가 절대적으로 틀리면** → 엉뚱한 것을 되돌린다. 그것을 막는 것이 이 파일이다.
"""

from __future__ import annotations

import json as _json

import pytest

from core.deterministic.compensation import (
    UNDO_DELETE_FILE,
    UNDO_RESTORE_FILE,
    UNDO_TOOL,
    invert,
    undo_plan,
)
from core.deterministic.sql_undo import invert_sql
from core.deterministic.work_history import normalize_events, to_action


def _trace(*calls):
    return [to_action(tool, args, result=result) for tool, args, result in calls]


def test_insert_is_reversed_by_deleting_exactly_what_was_inserted():
    """일부 컬럼만 조건에 넣으면 남의 행까지 지운다."""
    assert invert_sql(
        "insert into expense_ledger (tenant_id, applicant, amount) "
        "values ('localhost', '한지민', 29000);"
    ) == (
        "DELETE FROM expense_ledger WHERE tenant_id = 'localhost' "
        "AND applicant = '한지민' AND amount = 29000"
    )


def test_in_place_increment_is_reversed_by_flipping_the_sign():
    assert invert_sql("UPDATE product SET stock = stock + 20 WHERE name = 'x'") == (
        "UPDATE product SET stock = stock - 20 WHERE name = 'x'"
    )
    assert invert_sql("UPDATE product SET stock = stock - 3") == (
        "UPDATE product SET stock = stock + 3"
    )


@pytest.mark.parametrize("sql", [
    "UPDATE product SET stock = 5 WHERE id = 1",
    "DELETE FROM t WHERE id = 1",
    "INSERT INTO t (a, b) VALUES (now(), 'x')",
    "DROP TABLE t",
    "SELECT * FROM t",
])
def test_statements_without_a_known_previous_state_are_not_reversed(sql):
    assert invert_sql(sql) is None


def test_undo_runs_in_reverse_order():
    steps, reasons = invert(_trace(
        ("db_exec", {"sql": "INSERT INTO a (x) VALUES (1)"}, "ok"),
        ("db_exec", {"sql": "INSERT INTO b (y) VALUES (2)"}, "ok"),
    ))
    assert reasons == []
    assert [step.args["sql"] for step in steps] == [
        "DELETE FROM b WHERE y = 2",
        "DELETE FROM a WHERE x = 1",
    ]


def test_a_file_this_run_created_is_removed():
    steps, reasons = invert(_trace(
        ("write_file", {"file_path": "/out/a.md", "content": "문서"}, "ok"),
    ))
    assert reasons == []
    assert steps[0].kind == UNDO_DELETE_FILE and steps[0].path == "/out/a.md"


def test_a_file_read_before_writing_is_restored_not_removed():
    steps, reasons = invert(_trace(
        ("read_file", {"file_path": "/out/a.md"}, {"content": "예전 내용"}),
        ("write_file", {"file_path": "/out/a.md", "content": "새 내용"}, "ok"),
    ))
    assert reasons == []
    assert steps[0].kind == UNDO_RESTORE_FILE and steps[0].content == "예전 내용"


def test_reads_are_never_undone():
    steps, reasons = invert(_trace(
        ("read_file", {"file_path": "/skills/x/SKILL.md"}, "내용"),
        ("db_exec", {"sql": "SELECT 1"}, "[]"),
        ("db_exec", {"sql": "INSERT INTO a (x) VALUES (1)"}, "ok"),
    ))
    assert reasons == [] and len(steps) == 1


@pytest.mark.parametrize("call,fragment", [
    (("send_email", {"to": "a@b.c", "body": "본문"}, "ok"), "send_email"),
    (("execute", {"command": "python3 load.py"}, "ok"), "셸 실행"),
    (("db_exec", {"sql": "DELETE FROM t WHERE id = 1"}, "ok"), "되돌릴 수 없는 SQL"),
    (("delete_file", {"file_path": "/out/a.md"}, "ok"), "파일 조작"),
])
def test_irreversible_actions_are_named_not_guessed(call, fragment):
    steps, reasons = invert(_trace(call))
    assert steps == []
    assert any(fragment in reason for reason in reasons)


def test_one_irreversible_action_cancels_the_whole_plan():
    """절반만 되돌린 세상은 되돌리지 않은 세상보다 나쁘다."""
    assert undo_plan(_trace(
        ("db_exec", {"sql": "INSERT INTO a (x) VALUES (1)"}, "ok"),
        ("send_email", {"to": "a@b.c"}, "ok"),
    )) is None


def test_an_activity_with_nothing_to_undo_has_no_plan():
    assert undo_plan(_trace(("db_exec", {"sql": "SELECT 1"}, "[]"))) is None


def test_the_plan_is_shaped_for_the_stored_executor():
    """실행기는 `inputs["undo_steps"]` 를 읽는다. 키 이름이 어긋나면 조용히 아무것도 안 한다."""
    plan = undo_plan(_trace(("db_exec", {"sql": "INSERT INTO a (x) VALUES (1)"}, "ok")))
    assert set(plan) == {"undo_steps"}
    step = plan["undo_steps"][0]
    assert step["kind"] == "mcp_call" and step["tool"] == "db_exec"
    assert step["describes"]          # 무엇을 되돌렸는지 결과에 남길 문구


# ---------------------------------------------------------------------------
# 고착화 실행이 남긴 이력 — 두 사본이 같은 결론을 내야 한다
# ---------------------------------------------------------------------------

# 굳은 활동을 재작업할 때의 표본. 실행 골격이 단계마다 돌려준 결과가 완료 이벤트 하나에
# 통째로 실린다. 이것을 못 읽으면 "되돌릴 것이 없다"로 읽혀 순방향 코드가 덧씌워진다.
_FROZEN_RUN = {
    "event_type": "task_completed",
    "timestamp": "2026-09-03T00:00:01",
    "data": _json.dumps({
        "ok": True, "execution_mode": "deterministic", "llm_calls": 0, "undo_results": [],
        "results": [
            {"kind": "shell", "command": "date '+%Y-%m-%d'", "cwd": "/w", "output": "2026-09-03"},
            {"kind": "mcp_call", "tool": "db_exec", "server": "pg",
             "args": {"sql": "INSERT INTO ledger (applicant, amount) VALUES ('한지민', 29000)"},
             "data": None},
            {"kind": "file_write", "op": "write", "path": "/w/a.md", "bytes": 3},
        ],
    }, ensure_ascii=False),
}


def test_a_frozen_runs_history_undoes_to_the_same_plan_on_both_sides():
    """두 사본이 갈리면 굳힐 때 "되돌릴 수 있다"고 저장한 것을 실행기가 못 되돌린다."""
    steps, reasons = invert(normalize_events([_FROZEN_RUN]))
    assert reasons == []
    # 되돌리기는 역순. 값을 찍기만 한 셸은 되돌릴 대상이 아니다.
    assert [step.kind for step in steps] == [UNDO_DELETE_FILE, UNDO_TOOL]
    assert steps[0].path == "/w/a.md"
    assert steps[1].args["sql"] == (
        "DELETE FROM ledger WHERE applicant = '한지민' AND amount = 29000"
    )
