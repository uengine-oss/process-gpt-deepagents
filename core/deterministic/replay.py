from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shlex
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from .compensation import UNDO_DELETE_FILE, UNDO_RESTORE_FILE, UNDO_TOOL, invert
from .work_history import normalize_events

logger = logging.getLogger(__name__)

# 셸·파일 조작이 들어간 코드는 워크스페이스 안에서 돌아야 한다. 생성 코드가 쓰는
# 원시 동작으로 판별한다 — 저장된 이력이 없는 옛 레코드에도 통한다.
_WORKSPACE_PRIMITIVES = {"run_shell", "write_file", "edit_file", "read_file"}
_CONTAINER_SCRATCH = "/workspace/.deterministic"
# fastmcp 설치는 네트워크를 타므로 넉넉히 준다. 컨테이너당 한 번이다.
_MCP_INSTALL_TIMEOUT = 180


@dataclass(frozen=True)
class DeterministicExecution:
    mode: str
    output: dict[str, Any]
    # 되돌리기만 수행하고 재실행은 LLM에 맡길 때 True. 호출자는 결과 카드를 띄운 뒤
    # DeepAgents 그래프 실행을 계속해야 한다.
    continue_to_llm: bool = False
    # 워크아이템의 폼 산출물 `{폼아이디: {필드: 값}}`. 에이전트 경로의 `최종 결과 반환`
    # 이벤트가 싣는 것과 같은 모양이다. 이것이 없으면 워크아이템이 산출물 없이 완료되어
    # 다음 활동이 입력을 못 받는다.
    form_output: dict[str, Any] | None = None
    # 되돌리기가 아직 **끝나지 않은 채** 에이전트에게 넘어갈 때, 에이전트가 재작업보다
    # 먼저 수행해야 할 되돌리기 지시문. 호출자는 이것을 에이전트 입력 맨 앞에 붙여야
    # 한다. 비어 있지 않다는 것은 곧 "보상이 아직 안 됐다"는 뜻이다.
    undo_directive: str = ""

    @property
    def result_text(self) -> str:
        return json.dumps(self.output, ensure_ascii=False)

    @property
    def form_output_text(self) -> str | None:
        if self.form_output is None:
            return None
        return json.dumps(self.form_output, ensure_ascii=False)



def _enabled_servers(tenant_mcp: dict[str, Any]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, config in (tenant_mcp.get("mcpServers") or {}).items():
        if not isinstance(config, dict) or config.get("enabled", True) is False:
            continue
        servers[name] = {k: v for k, v in config.items() if k != "enabled"}
    return {"mcpServers": servers}


def _json_objects(text: str):
    """텍스트에 박힌 JSON 객체들을 찾는다.

    첫 `{` 부터 마지막 `}` 까지를 한 덩어리로 집으면 안 된다. 지시문의 [Instruction]
    에는 `expense/{used_at}_{applicant}.md` 같은 자리표시자가 흔히 들어 있어, 그것까지
    한 덩어리에 들어가면 JSON 파싱이 통째로 실패한다. 그러면 구조화 입력이 빈 것으로
    보이고, 실행기는 지시문의 따옴표를 순서대로 긁어 **칸 이름**을 값으로 집는다.

    중괄호 균형을 세어 객체 하나씩 자른다. 파싱되지 않는 덩어리는 건너뛰고 다음 `{`
    부터 다시 본다.
    """
    index, length = 0, len(text or "")
    while index < length:
        if text[index] != "{":
            index += 1
            continue
        depth, cursor, in_string, escaped = 0, index, False, False
        while cursor < length:
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        try:
            parsed = json.loads(text[index:cursor])
        except (ValueError, TypeError):
            index += 1
            continue
        yield parsed
        index = cursor


def _structured_fields(query: str) -> dict[str, Any]:
    """워크아이템 지시문에 실린 구조화 입력(`[InputData]` JSON)을 키→값으로 편다.

    이 시스템의 지시문에는 이전 단계의 폼 값이 JSON으로 붙는다. 값이 실제로 있는
    곳이 여기이므로, 자유 텍스트를 훑기 전에 이쪽을 먼저 본다. 중첩은 잎(스칼라)만
    남기고, 같은 키가 겹치면 먼저 나온 것을 쓴다.
    """
    fields: dict[str, Any] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    _walk(value)
                elif key not in fields:
                    fields[key] = value
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for parsed in _json_objects(query or ""):
        _walk(parsed)
    return fields


def _normalize_key(text: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", str(text or "").lower())


def _field_lookup(fields: dict[str, Any], names: tuple[str, ...]) -> str | None:
    """구조화 입력에서 파라미터 이름과 맞는 칸의 값을 찾는다.

    이름은 자리 번호가 붙어 오기도 하므로(`po_id_1`) 꼬리 숫자를 떼고도 본다.
    부분 일치까지 허용하되, 후보가 여럿이면 포기한다 — 어느 칸인지 정해지지 않은
    값을 확신 있게 넣는 것보다 고착화를 포기하는 편이 안전하다.
    """
    normalized = {_normalize_key(k): v for k, v in fields.items()}
    for name in (n for n in names if n):
        for candidate in (name, re.sub(r"_\d+$", "", name)):
            key = _normalize_key(candidate)
            if not key:
                continue
            value = normalized.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value)
            hits = [
                v for k, v in normalized.items()
                if key and (key in k or k in key)
                and isinstance(v, (str, int, float)) and str(v).strip()
            ]
            if len(hits) == 1:
                return str(hits[0])
    return None


def related_workitem_outputs(db: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
    """같은 루트 프로세스에서 이미 완료된 다른 워크아이템의 산출물.

    에이전트가 `get_related_workitem_outputs` 로 읽던 것과 **같은 자료를 같은 모양으로**
    돌려준다. 고착화 생성기는 표본을 모을 때 같은 조회로 "이 값이 앞 액티비티의
    산출물에서 왔는가"를 관측했다. 모양이 갈리면 관측한 자리와 읽는 자리가 달라져
    조용히 틀린다.
    """
    query = db.table("todolist").select(
        "id, proc_inst_id, activity_id, activity_name, end_date, output"
    ).eq("tenant_id", row.get("tenant_id")).not_.is_("output", "null")
    root_id = row.get("root_proc_inst_id")
    if root_id:
        query = query.eq("root_proc_inst_id", root_id)
    elif row.get("proc_inst_id"):
        query = query.eq("proc_inst_id", row.get("proc_inst_id"))
    else:
        return []
    if row.get("id"):
        query = query.neq("id", row["id"])
    rows = query.order("end_date", desc=True).execute().data or []
    return [
        {
            "workitemId": item.get("id"),
            "procInstId": item.get("proc_inst_id"),
            "activityId": item.get("activity_id"),
            "activityName": item.get("activity_name"),
            "endDate": item.get("end_date"),
            "output": item.get("output"),
        }
        for item in rows
    ]


def _descend(node: Any, key: Any) -> Any:
    """산출물 한 겹을 들어간다. JSON 문자열로 저장된 칸은 풀어서 들어간다."""
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except ValueError:
            return None
    if isinstance(node, dict):
        return node.get(key)
    if isinstance(node, list) and isinstance(key, int) and -len(node) <= key < len(node):
        return node[key]
    return None


def _upstream_value(binding: dict[str, Any], related: list[dict[str, Any]]) -> Any:
    """앞 액티비티의 산출물에서 값을 읽는다.

    같은 액티비티가 여러 번 돌았으면(재작업) 가장 최근에 끝난 것을 쓴다 — 조회가
    이미 최근순이므로 먼저 맞는 것이 그것이다. 자리가 비어 있으면 값을 지어내지 않고
    None을 돌려준다. 호출자가 실패로 보고 에이전트에게 넘긴다.
    """
    activity_id = str(binding.get("activity_id") or "")
    for record in related or []:
        if str(record.get("activityId") or "") != activity_id:
            continue
        node: Any = record
        for key in binding.get("path") or []:
            node = _descend(node, key)
            if node is None:
                break
        if node is not None and str(node).strip():
            return node
    return None


def _parameter_values(
    query: str,
    specification: Any,
    row: dict[str, Any] | None = None,
    related: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract replay values without an LLM.

    생성기가 파라미터마다 남긴 ``label``이 1차 열쇠다. 그 이름표는 값이 관측될 때
    **그 워크아이템의 지시문에서 값 바로 앞에 적혀 있던 말**이고, 표본 전부에서
    같았을 때만 저장된다. 그래서 같은 활동의 다음 지시문에서도 같은 자리를 가리킨다.

    도메인별 별칭표를 코드에 박지 않는 이유가 여기 있다. 재고 프로세스의 "상품명"을
    적어 두면 계약·채용·정산 프로세스에서는 아무 쓸모가 없고, 늘어날수록 어느
    프로세스를 위한 것인지 알 수 없게 된다. 이름표는 관측에서 나와야 한다.

    이름표가 없으면 파라미터 이름 자체를 이름표로 삼고, 그마저 없으면 타입에 맞는
    값을 지시문에 나온 순서대로 집는다.

    `upstream` 이 붙은 파라미터는 앞 액티비티의 산출물에서 읽는다. 값이 액티비티
    경계를 넘어 흐르는 자리다 — 앞 워크아이템의 완료 시각처럼 지시문에 적히지 않는
    값이 여기 해당한다. 어느 액티비티의 어느 자리인지는 생성 단계에서 표본 전부를
    대조해 정해 둔 것이므로, 여기서 다시 찾아 헤매지 않는다.

    `runtime` 이 붙은 파라미터만은 지시문을 아예 보지 않는다. `proc_inst_id`·`todo_id`
    처럼 실행 때 정해지는 값이라 지시문에 적혀 있지 않고, 그런데도 이름표가 없어 위치
    폴백까지 흘러가 **엉뚱한 값을 확신 있게** 채운다. 실제로 경비 대장의 proc_inst_id
    자리에 신청자 이름이, todo_id 자리에 계정과목이 들어간 적이 있다. 이 값들의 출처는
    지금 실행 중인 워크아이템 행 하나뿐이다.
    """
    params = specification.get("parameters", []) if isinstance(specification, dict) else []
    values: dict[str, Any] = {}
    fields = _structured_fields(query)
    # 후보 문자열은 구조화 입력이 있으면 그 **값**만 쓴다. 지시문의 따옴표를 그대로
    # 긁으면 `[InputData]` JSON의 **키 이름**(`..._form`, `supplier_name` 등)이 먼저
    # 잡혀, 엉뚱한 값을 확신 있게 넣은 채 실제 도구를 호출하게 된다.
    quoted = (
        [str(v) for v in fields.values() if isinstance(v, str) and v.strip()]
        if fields
        else re.findall(r"['\"]([^'\"]+)['\"]", query)
    )
    numbers = re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", query)
    quoted_i = number_i = 0

    for param in params:
        name = str(param.get("name") or "")
        if not name:
            continue
        upstream = param.get("upstream")
        if isinstance(upstream, dict):
            value = _upstream_value(upstream, related or [])
            if value is None:
                raise ValueError(
                    f"앞 단계 산출물에서 파라미터를 찾을 수 없습니다: "
                    f"{name}({upstream.get('activity_id')})"
                )
            values[name] = value
            continue
        runtime_key = str(param.get("runtime") or "").strip()
        if runtime_key:
            identity = (row or {}).get(runtime_key)
            if identity is None or not str(identity).strip():
                raise ValueError(
                    f"워크아이템에서 실행 식별자를 찾을 수 없습니다: {name}({runtime_key})"
                )
            values[name] = str(identity)
            continue
        ptype = str(param.get("type") or "string").lower()
        observed = str(param.get("label") or "").strip()
        position = str(param.get("label_position") or "before")
        raw: str | None = None
        if observed and position == "after":
            # `Galaxy 상품의` 처럼 이름표가 값 뒤에 붙는 표현.
            match = re.search(
                rf"['\"]?([^,'\".;\s]+)['\"]?\s*{re.escape(observed)}",
                query,
                flags=re.IGNORECASE,
            )
            if match:
                raw = match.group(1).strip()
        if raw is None and fields:
            # 구조화 입력에 같은 이름의 칸이 있으면 그 값이 가장 확실하다.
            raw = _field_lookup(fields, (observed, name))
        if raw is None:
            for label in (label for label in (observed, name.replace("_", " "), name) if label):
                match = re.search(
                    rf"(?:{re.escape(label)})\s*(?:은|는|이|가|:|=)\s*"
                    r"['\"]?([^,'\".;\n]+)",
                    query,
                    flags=re.IGNORECASE,
                )
                if match:
                    raw = match.group(1).strip()
                    break
        if raw is None:
            if ptype in {"integer", "int", "number", "float"} and number_i < len(numbers):
                raw, number_i = numbers[number_i], number_i + 1
            elif quoted_i < len(quoted):
                raw, quoted_i = quoted[quoted_i], quoted_i + 1
        if raw is None:
            example = param.get("example")
            if isinstance(example, str) and example and example in query:
                raw = example
        if raw is None:
            raise ValueError(f"워크아이템 입력에서 파라미터를 찾을 수 없습니다: {name}")
        if ptype in {"integer", "int"}:
            num = re.search(r"-?\d+", raw)
            if not num:
                raise ValueError(f"정수 파라미터를 해석할 수 없습니다: {name}={raw}")
            values[name] = int(num.group())
        elif ptype in {"number", "float"}:
            num = re.search(r"-?\d+(?:\.\d+)?", raw)
            if not num:
                raise ValueError(f"숫자 파라미터를 해석할 수 없습니다: {name}={raw}")
            values[name] = float(num.group())
        elif ptype in {"boolean", "bool"}:
            values[name] = raw.lower() in {"true", "1", "yes", "예", "참"}
        else:
            values[name] = raw.strip()
    return values


def _needs_upstream(specification: Any) -> bool:
    params = specification.get("parameters", []) if isinstance(specification, dict) else []
    return any(isinstance(param.get("upstream"), dict) for param in params)


# 재작업 직전 실행을 되돌릴 수 있는가에 대한 판정. 네 갈래를 구분한다 — 특히 **되돌릴
# 것이 없다**와 **되돌릴 수 없다**를 뭉뚱그리면 안 된다. 앞의 것은 그대로 재실행해도
# 되지만, 뒤의 것을 같은 취급하면 되돌리기를 건너뛴 채 순방향 코드가 덧씌워져 같은
# 값이 두 번 반영된다.
UNDO_NONE = "nothing"           # 이전 실행에 되돌릴 부수효과가 없었다
UNDO_PLAN = "plan"              # 관측에서 되돌리기 계획이 나왔다
UNDO_IMPOSSIBLE = "impossible"  # 되돌릴 수 없는 행위가 섞여 있다
UNDO_UNKNOWN = "unknown"        # 이전 실행이 무엇을 했는지 읽을 수 없다


@dataclass(frozen=True)
class UndoDecision:
    """이전 실행을 되돌릴 수 있는가. 사유까지 함께 든다 — 기록에 남겨야 한다."""

    status: str
    plan: dict[str, Any] | None = None
    reasons: tuple[str, ...] = ()

    @property
    def blocks_replay(self) -> bool:
        """재작업 전체를 에이전트에게 넘겨야 하는가.

        되돌릴 근거가 없으면 다시 실행하지 않는다. 근거 없이 재실행하는 것이 곧
        중복 반영이다.
        """
        return self.status in (UNDO_IMPOSSIBLE, UNDO_UNKNOWN)

    @property
    def summary(self) -> str:
        if self.status == UNDO_IMPOSSIBLE:
            return "이전 실행에 되돌릴 수 없는 행위가 있습니다."
        if self.status == UNDO_UNKNOWN:
            return "이전 실행이 무엇을 했는지 읽을 수 없습니다."
        if self.status == UNDO_NONE:
            return "이전 실행에 되돌릴 부수효과가 없습니다."
        return "이전 실행을 되돌릴 수 있습니다."


def _previous_run_events(db: Any, row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """재작업 직전 회차의 이력. 그 회차를 찾지 못하면 None."""
    query = db.table("todolist").select("id,rework_count,updated_at")
    root_id = row.get("root_proc_inst_id")
    if root_id:
        query = query.eq("root_proc_inst_id", root_id)
    else:
        query = query.eq("proc_inst_id", row.get("proc_inst_id"))
    previous = (
        query.eq("activity_id", row.get("activity_id"))
        .lt("rework_count", int(row.get("rework_count") or 0))
        .order("rework_count", desc=True).limit(1).execute().data or []
    )
    if not previous:
        return None
    # event_type으로 미리 거르지 않는다. 런타임마다 "도구 사용이 끝났다"를 뜻하는
    # 타입 이름이 다르고, 하나로 못박으면 다른 러너의 이력이 통째로 사라진다.
    return (
        db.table("events")
        .select("timestamp,event_type,data")
        .eq("todo_id", previous[0]["id"])
        .order("timestamp")
        .execute()
        .data
        or []
    )


def _previous_undo_decision(db: Any, row: dict[str, Any]) -> UndoDecision:
    """직전 실행을 되돌릴 수 있는지 관측으로 판정한다.

    되돌릴 수 없는 행위가 하나라도 섞여 있으면 계획을 만들지 않는다 — 절반만 되돌린
    세상은 되돌리지 않은 세상보다 나쁘다.
    """
    events = _previous_run_events(db, row)
    if events is None:
        return UndoDecision(UNDO_UNKNOWN, reasons=("이전 회차의 워크아이템을 찾지 못했습니다.",))
    if not events:
        return UndoDecision(UNDO_UNKNOWN, reasons=("이전 실행의 이력이 남아 있지 않습니다.",))
    trace = normalize_events(events)
    if not trace:
        return UndoDecision(UNDO_UNKNOWN, reasons=("이전 실행의 이력을 행위로 읽지 못했습니다.",))
    if not any(action.has_effect for action in trace):
        # 조회와 읽기만 한 활동. 되돌릴 것이 없으므로 그대로 다시 수행해도 된다.
        return UndoDecision(UNDO_NONE)
    steps, reasons = invert(trace)
    if reasons:
        return UndoDecision(UNDO_IMPOSSIBLE, reasons=tuple(reasons))
    if not steps:
        return UndoDecision(UNDO_UNKNOWN, reasons=("되돌릴 단계를 만들지 못했습니다.",))
    return UndoDecision(UNDO_PLAN, plan={"undo_steps": [step.as_dict() for step in steps]})


# 되돌리기가 끝나지 않은 채 재작업이 에이전트에게 넘어간 상태. "되돌릴 게 없어서 넘겼다"
# (deterministic-skipped)와 반드시 구분해야 한다 — 이쪽은 되돌릴 것이 **남아 있다**.
MODE_UNDO_PENDING = "deterministic-undo-pending"

# 되돌리기 지시문에 실을 파일 내용의 상한. 이보다 길면 지시문에 그대로 싣지 않는다 —
# 잘린 내용으로 복원하면 되돌리지 않은 것보다 나쁘다.
_MAX_RESTORE_CONTENT = 4000


def _describe_undo_step(index: int, step: dict[str, Any]) -> str:
    """되돌리기 한 단계를 에이전트가 그대로 수행할 수 있게 적는다."""
    kind = str(step.get("kind") or "")
    if kind == UNDO_TOOL:
        args = json.dumps(step.get("args") or {}, ensure_ascii=False)
        return f"{index}. MCP 도구 `{step.get('tool')}` 를 다음 인자로 호출: {args}"
    if kind == UNDO_DELETE_FILE:
        return f"{index}. 파일 삭제: `{step.get('path')}`"
    if kind == UNDO_RESTORE_FILE:
        path = step.get("path")
        content = str(step.get("content") or "")
        if len(content) > _MAX_RESTORE_CONTENT:
            # 잘린 내용으로 복원하면 되돌리지 않은 것보다 나쁘다. 짐작을 시키지 않는다.
            return (
                f"{index}. 파일 `{path}` 을 덮어쓰기 전 내용으로 복원해야 하는데, 그 내용이"
                " 너무 길어 여기에 싣지 못했습니다. 짐작해서 복원하지 말고, 이 단계를"
                " 되돌리지 못했다는 사실을 결과에 적고 작업을 중단하십시오."
            )
        return f"{index}. 파일 `{path}` 을 아래 내용으로 되돌려 쓰기:\n```\n{content}\n```"
    return f"{index}. {step.get('describes') or kind}: {json.dumps(step, ensure_ascii=False)}"


def _undo_directive(plan: dict[str, Any] | None, reason: str, failure: str = "") -> str:
    """되돌리기를 에이전트가 이어받게 하는 지시문. 계획이 없으면 빈 문자열.

    보상이 끝나기 전에 재작업을 시작하면 같은 값이 두 번 반영된다. 실행기가 보상 코드를
    돌리지 못했다면 그 일은 사라지는 것이 아니라 **에이전트에게 넘어가는 것**이다.
    지시문 없이 넘기면 에이전트는 되돌릴 것이 있었다는 사실 자체를 모른 채 재작업만 한다.
    """
    steps = (plan or {}).get("undo_steps") or []
    if not steps:
        return ""
    lines = [
        "[필수 선행 작업 — 이전 실행 되돌리기]",
        "이 워크아이템은 재작업입니다. 이전 회차가 남긴 아래 부수효과를 **먼저 되돌린 뒤에만**"
        " 이번 회차의 작업을 시작하십시오.",
        f"사유: {reason}",
    ]
    if failure:
        lines.append(f"실행기 오류: {failure}")
    lines.append("")
    lines.append("되돌릴 단계(적힌 순서대로 수행):")
    lines.extend(
        _describe_undo_step(index, step)
        for index, step in enumerate(steps, start=1)
        if isinstance(step, dict)
    )
    lines.extend(
        [
            "",
            "규칙:",
            "- 위 되돌리기를 모두 끝내기 전에는 이번 회차의 작업(파일 생성·수정, 도구 호출,"
            " 산출물 작성)을 절대 시작하지 마십시오.",
            "- 이미 되돌려져 있는 단계는 건너뛰되, 건너뛴 이유를 결과에 적으십시오.",
            "- 한 단계라도 수행할 수 없으면 재작업을 진행하지 마십시오. 무엇을 왜 되돌리지"
            " 못했는지 결과에 적고 거기서 멈추십시오 — 되돌려지지 않은 것 위에 다시 작업하는"
            " 것보다, 멈춰서 사람이 보게 하는 편이 낫습니다.",
            "- 되돌리기를 마친 뒤에야 아래 원래 작업 지시를 수행하십시오.",
        ]
    )
    return "\n".join(lines)


def _handed_to_agent(
    reason: str,
    details: tuple[str, ...] = (),
    note: str = "이전 실행을 되돌릴 근거가 없어 재작업 전체를 에이전트가 수행합니다.",
    undo_directive: str = "",
    undo_plan: dict[str, Any] | None = None,
) -> DeterministicExecution:
    """재작업을 통째로 에이전트가 맡는다는 사실을 결과 카드로 남긴다.

    조용히 넘기면 "되돌릴 것이 없었다"와 "되돌릴 수 없었다"가 기록에서 구분되지 않는다.
    구분이 없으면 다음에 같은 일이 생겨도 무엇을 고쳐야 할지 알 수 없다.

    되돌릴 것이 **남아 있는** 채로 넘기는 경우(`undo_directive`)는 모드까지 갈라 놓는다.
    호출자가 그 지시문을 에이전트 입력 앞에 붙여야 보상이 재작업보다 먼저 일어난다.
    """
    mode = MODE_UNDO_PENDING if undo_directive else "deterministic-skipped"
    output: dict[str, Any] = {
        "ok": True,
        "execution_mode": mode,
        "llm_calls": 0,
        "undo_results": [],
        "results": [],
        "reason": reason,
        "details": list(details),
        "note": note,
    }
    if undo_directive:
        output["undo_pending"] = True
        output["undo_steps"] = list((undo_plan or {}).get("undo_steps") or [])
    return DeterministicExecution(
        mode=mode,
        output=output,
        continue_to_llm=True,
        undo_directive=undo_directive,
    )


def _parse_result(stdout: str, failure: str) -> dict[str, Any]:
    """생성 코드의 마지막 출력 줄에서 실행 결과를 읽는다."""
    lines = stdout.strip().splitlines()
    if not lines:
        raise RuntimeError(failure)
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{failure} (결과 JSON 파싱 실패: {exc})") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError(f"결정론적 코드가 실패 결과를 반환했습니다: {result}")
    return result


def _needs_workspace(code: str) -> bool:
    """이 코드가 셸이나 파일을 건드리는가.

    스킬 절차를 셸 스크립트로 수행한 활동은 고착화 코드에도 셸이 남는다. 그런 코드는
    에이전트가 일했던 것과 같은 워크스페이스 안에서 돌아야 경로와 도구가 맞는다.

    문자열 검색으로는 판정할 수 없다 — 골격이 `run_shell`/`write_file` 을 **정의**
    하므로 MCP 호출만 하는 코드에도 그 이름이 들어 있다. 실제 호출부만 봐야 한다.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 파싱이 안 되면 어차피 실행도 안 된다. 더 제약이 큰 쪽(샌드박스)으로 민다.
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in _WORKSPACE_PRIMITIVES:
            return True
    return False


def _ensure_sandbox_mcp_runtime(backend: Any) -> None:
    """샌드박스 안에 fastmcp 를 깔아 둔다. MCP 호출이 섞인 코드에 한해 한 번만.

    샌드박스 이미지는 python:3.13-slim 이라 fastmcp 가 없다. 그래서 파일 조작과 MCP
    호출이 **함께** 있는 코드 — 파일을 되돌리면서 DB 도 되돌리는 보상 코드가 딱 이
    모양이다 — 는 `ModuleNotFoundError: No module named 'fastmcp'` 로 통째로 죽는다.
    호스트에서 대신 돌릴 수는 없다. `/workspace` 가 다른 것을 가리켜 엉뚱한 파일을
    건드리기 때문이다. 그러니 샌드박스 쪽에 런타임을 맞춘다.

    실패해도 예외를 올리지 않는다. 여기서 막는 대신 실제 실행이 실패하게 두어야,
    호출자의 폴백(되돌리기를 에이전트에게 넘기기)이 그대로 동작한다.
    """
    if getattr(backend, "_deterministic_mcp_ready", False):
        return
    try:
        response = backend.execute(
            "python3 -c 'import fastmcp' 2>/dev/null || "
            "pip install --quiet --disable-pip-version-check fastmcp",
            timeout=_MCP_INSTALL_TIMEOUT,
        )
        if response.exit_code:
            logger.warning(
                "샌드박스에 fastmcp 설치 실패(%s) — MCP 단계가 있으면 실행이 실패합니다: %s",
                response.exit_code, (response.output or "")[-300:],
            )
            return
    except Exception:
        logger.warning("샌드박스 fastmcp 준비 실패 — 실행을 그대로 시도합니다", exc_info=True)
        return
    backend._deterministic_mcp_ready = True


async def _run_in_sandbox(
    tenant_id: str, code: str, inputs: dict[str, Any], mcp_config: dict[str, Any]
) -> dict[str, Any]:
    """셸·파일 작업이 있는 코드를 에이전트와 같은 샌드박스에서 실행한다.

    샌드박스를 못 얻으면 예외를 던진다 — 호출자가 그것을 폴백 신호로 받아 에이전트
    실행으로 되돌린다. 호스트에서 대신 돌리지 않는다. `/workspace` 경로가 없거나
    다른 것을 가리켜, 조용히 엉뚱한 파일을 건드릴 수 있기 때문이다.
    """
    from pathlib import Path as _Path

    from core.sandbox.docker_sandbox import get_or_create_sandbox

    base = _Path(__file__).parent.parent.parent
    skills_host = _Path(os.getenv("SKILLS_HOST", str(base / "skills")))
    backend = get_or_create_sandbox(
        tenant_id, workspace_host=base / "workspace", skills_host=skills_host
    )

    if "call_tool(" in code and (mcp_config.get("mcpServers") or {}):
        await asyncio.to_thread(_ensure_sandbox_mcp_runtime, backend)

    script_path = f"{_CONTAINER_SCRATCH}/run_{uuid.uuid4().hex}.py"
    inputs_path = f"{script_path[:-3]}.json"
    uploads = await asyncio.to_thread(
        backend.upload_files,
        [
            (script_path, code.encode("utf-8")),
            (inputs_path, json.dumps(inputs, ensure_ascii=False).encode("utf-8")),
        ],
    )
    failed = [u for u in uploads if getattr(u, "error", None)]
    if failed:
        raise RuntimeError(f"샌드박스에 결정론적 코드를 올리지 못했습니다: {failed[0].error}")

    env = shlex.quote(json.dumps(mcp_config, ensure_ascii=False))
    command = (
        f"MCP_CONFIG={env} DETERMINISTIC_WORKDIR=/workspace "
        f"python3 {shlex.quote(script_path)} {shlex.quote(inputs_path)}"
    )
    try:
        response = await asyncio.to_thread(backend.execute, command, timeout=300)
        if response.exit_code:
            raise RuntimeError(
                f"결정론적 코드 실행 실패({response.exit_code}): {(response.output or '')[-1000:]}"
            )
        return _parse_result(response.output or "", "결정론적 코드가 결과를 출력하지 않았습니다")
    finally:
        await asyncio.to_thread(
            backend.execute, f"rm -f {shlex.quote(script_path)} {shlex.quote(inputs_path)}"
        )


async def _run_locally(code: str, inputs: dict[str, Any], mcp_config: dict[str, Any]) -> dict[str, Any]:
    """MCP 호출만 하는 코드는 이 프로세스 옆에서 바로 돌린다."""
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(code)
            path = handle.name
        env = os.environ.copy()
        env["MCP_CONFIG"] = json.dumps(mcp_config, ensure_ascii=False)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            path,
            json.dumps(inputs, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode:
            detail = stderr.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"결정론적 코드 실행 실패({proc.returncode}): {detail}")
        return _parse_result(
            stdout.decode("utf-8", errors="replace"),
            "결정론적 코드가 결과를 출력하지 않았습니다",
        )
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


# 옛 실행 골격은 도구를 부르기 전에 `await client.ping()` 으로 연결을 확인했다. ping 은 MCP
# 스펙의 선택 기능이라 구현하지 않은 서버가 "Method not found" 를 돌려주는데, 그러면 도구를
# 불러 보지도 못하고 실행이 통째로 죽는다(같은 서버를 에이전트는 잘 쓴다 — 에이전트 쪽
# 클라이언트는 ping 을 하지 않는다). 골격은 고쳤지만 **이미 저장된 코드**에는 그 줄이 남아
# 있고, 그 코드는 활동을 다시 굳히기 전에는 갱신되지 않는다. 실행 직전에 무력화한다.
_LEGACY_PING = re.compile(r"^([ \t]*)await client\.ping\(\)[ \t]*$", re.MULTILINE)


def _drop_legacy_ping(code: str) -> str:
    patched, count = _LEGACY_PING.subn(r"\1pass  # ping 제거(선택 기능 — 미구현 서버에서 실패)", code)
    if count:
        logger.info("저장된 코드의 ping 확인 %d줄을 건너뜁니다 — 도구 호출로 바로 갑니다", count)
    return patched


# 되돌리기 단계 중 워크스페이스가 실제로 필요한 것들.
_WORKSPACE_UNDO_STEPS = {"file_delete", "file_restore"}


def _plan_needs_workspace(inputs: dict[str, Any]) -> bool | None:
    """이번에 **실행할 단계**가 워크스페이스를 쓰는가. 단계 목록이 없으면 None.

    보상 골격은 파일 단계를 처리하는 디스패처를 늘 품고 있다 — `write_file(...)` 호출이
    코드 어딘가에 있다는 뜻이다. 그래서 코드 텍스트로만 판정하면 **모든 보상 코드가**
    실제로는 MCP 호출 하나만 할 때에도 샌드박스로 간다. 샌드박스는 순방향 코드가 한 번도
    가 본 적 없는 길이라(순방향은 파일을 안 만지면 로컬에서 돈다), 보상만 골라서 험한
    길로 보내는 셈이었다. 돌릴 단계가 파일을 안 건드리면 로컬에서 돈다.
    """
    steps = (inputs or {}).get("undo_steps")
    if not isinstance(steps, list):
        return None
    return any(
        str(step.get("kind") or "") in _WORKSPACE_UNDO_STEPS
        for step in steps
        if isinstance(step, dict)
    )


async def _run_code(
    code: str, inputs: dict[str, Any], mcp_config: dict[str, Any], tenant_id: str = ""
) -> dict[str, Any]:
    code = _drop_legacy_ping(code)
    needs_workspace = _plan_needs_workspace(inputs)
    if needs_workspace is None:
        needs_workspace = _needs_workspace(code)
    if needs_workspace:
        return await _run_in_sandbox(tenant_id, code, inputs, mcp_config)
    return await _run_locally(code, inputs, mcp_config)



def _execution_summary(inputs: dict[str, Any], results: list[Any]) -> str:
    """이번 실행에서 실제로 일어난 일을 한 문단으로 적는다.

    표본마다 문구가 달랐던 필드는 템플릿으로 접히지 않는다. 그렇다고 표본 하나의
    문장을 굳히면 그 표본의 사실(그때의 신청자, 그때의 시각)이 다음 실행에 그대로
    남아 조용히 틀린다. 지어내지 않고 관측된 것만 적는다 — 무엇을 불렀고 무엇이
    성공했으며 어떤 입력으로 했는지.
    """
    steps: list[str] = []
    for result in results or []:
        if not isinstance(result, dict):
            continue
        kind = str(result.get("kind") or "")
        if kind == "mcp_call":
            steps.append(f"{result.get('tool')}({result.get('server')})")
        elif kind == "shell":
            steps.append("셸 명령")
        elif kind:
            steps.append(kind)
    lines = ["고착화된 코드로 LLM 추론 없이 실행했습니다."]
    if steps:
        lines.append(f"- 실행: {', '.join(steps)} — {len(steps)}건 성공")
    if inputs:
        joined = ", ".join(f"{key}={value}" for key, value in inputs.items())
        lines.append(f"- 입력: {joined}")
    return "\n".join(lines)


def _render_form_output(
    template: Any, inputs: dict[str, Any], results: list[Any]
) -> dict[str, Any] | None:
    """저장된 산출물 템플릿을 이번 실행의 입력으로 렌더한다.

    필드마다 생성 단계가 정해 둔 셋 중 하나를 따른다. `const` 는 그대로, `template` 은
    렌더, `None` 은 실행 요약으로 채운다. 렌더가 깨지는 필드 하나 때문에 폼 전체를
    포기하지는 않는다 — 그 필드만 실행 요약으로 떨어뜨린다.
    """
    if not isinstance(template, dict):
        return None
    form_id = str(template.get("form_id") or "")
    fields = template.get("fields")
    if not form_id or not isinstance(fields, dict):
        return None

    summary = _execution_summary(inputs, results)
    body: dict[str, Any] = {}
    for key, spec in fields.items():
        if isinstance(spec, dict) and "const" in spec:
            body[key] = spec["const"]
            continue
        if isinstance(spec, dict) and isinstance(spec.get("template"), str):
            try:
                body[key] = Template(spec["template"]).substitute(inputs)
                continue
            except (KeyError, ValueError):
                logger.warning(
                    "산출물 필드 렌더 실패 | field=%s — 실행 요약으로 대체", key
                )
        body[key] = summary
    return {form_id: body}


async def try_deterministic_execution(
    row: dict[str, Any], tenant_mcp: dict[str, Any]
) -> DeterministicExecution | None:
    """Execute a stored route for a worklist item, or return None for LLM fallback."""
    if not all(row.get(key) for key in ("proc_def_id", "activity_id", "tenant_id")):
        return None
    from core.storage.db import get_supabase

    db = get_supabase()
    config = _enabled_servers(tenant_mcp)
    rework_count = int(row.get("rework_count") or 0)
    try:
        # 비활성 여부와 무관하게 최신 행을 가져온다. 코드가 비활성화된 뒤에도 보상
        # (undo)은 실행되어야 하기 때문이다. 고착화 여부는 deactivated_at으로 가린다.
        records = (
            db.table("mcp_python_code")
            .select("*")
            .eq("proc_def_id", row["proc_def_id"])
            .eq("activity_id", row["activity_id"])
            .eq("tenant_id", row["tenant_id"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        # Older installations may not have the table yet. Preserve the existing
        # DeepAgents path until the deterministic-replay migration is applied.
        logger.warning("mcp_python_code 조회 실패 — DeepAgents 실행으로 폴백", exc_info=True)
        return None
    if not records:
        return None

    record = records[0]
    # 고착화 판정과 무효화는 completion(워크아이템 제출·재작업 훅)이 담당한다.
    # 실행 런타임은 활성 여부를 읽기만 한다.
    is_active = record.get("deactivated_at") is None and bool(record.get("code"))

    tenant_id = str(row.get("tenant_id") or "")

    undo_results: list[Any] = []
    undo_status = ""
    if rework_count > 0:
        # 보상 코드가 저장돼 있는지부터 보지 않는다. 없으면 되돌리기를 통째로 건너뛰고
        # 순방향만 도는데, 그것이 곧 중복 반영이다. 되돌릴 것이 있었는지는 저장된
        # 코드가 아니라 **이전 실행의 관측**이 정한다.
        decision = await asyncio.to_thread(_previous_undo_decision, db, row)
        undo_status = decision.status
        if decision.blocks_replay:
            logger.info(
                "되돌리기 불가 | todo=%s status=%s 사유: %s — 재작업 전체를 DeepAgents가 담당",
                row.get("id"), decision.status, "; ".join(decision.reasons) or "(없음)",
            )
            return _handed_to_agent(decision.summary, decision.reasons)
        if decision.status == UNDO_PLAN:
            if not record.get("compensation"):
                # 되돌릴 수는 있는데 되돌릴 코드가 없다. 이 활동이 굳기 전에 보상이
                # 만들어지지 않은 경우다. 되돌리기를 건너뛰고 재작업만 하면 같은 값이
                # 두 번 반영된다 — 관측으로 만든 되돌리기 계획을 그대로 에이전트에게
                # 넘겨, 재작업보다 **먼저** 수행하게 한다.
                reason = "이전 실행을 되돌릴 코드가 저장돼 있지 않습니다."
                logger.info(
                    "보상 코드 없음 | todo=%s undo_steps=%d — 되돌리기부터 DeepAgents가 담당",
                    row.get("id"), len((decision.plan or {}).get("undo_steps") or []),
                )
                return _handed_to_agent(
                    reason,
                    ("이 활동에 보상 코드가 없습니다.",),
                    note="되돌리기를 에이전트가 먼저 수행한 뒤에 재작업합니다.",
                    undo_directive=_undo_directive(decision.plan, reason),
                    undo_plan=decision.plan,
                )
            try:
                undo = await _run_code(
                    record["compensation"], decision.plan, config, tenant_id
                )
            except Exception as error:
                # 보상 코드가 죽었다고 해서 되돌릴 일이 사라지는 것은 아니다. 여기서
                # 그냥 재작업으로 넘어가면 되돌려지지 않은 세상 위에 같은 값이 두 번
                # 반영된다. 되돌리기는 **없어지지 않고 에이전트에게 넘어간다** — 관측으로
                # 만든 계획을 지시문으로 실어, 재작업보다 먼저 수행하게 한다.
                failure = str(error)[-500:]
                reason = "이전 실행을 되돌리는 코드가 실패했습니다."
                logger.warning(
                    "보상 코드 실행 실패 | todo=%s undo_steps=%d — 되돌리기부터 DeepAgents가 담당",
                    row.get("id"), len((decision.plan or {}).get("undo_steps") or []),
                    exc_info=True,
                )
                return _handed_to_agent(
                    reason,
                    (
                        "되돌리기가 어디까지 적용됐는지 알 수 없습니다.",
                        f"실행기 오류: {failure}",
                    ),
                    note="되돌리기를 에이전트가 먼저 마친 뒤에 재작업합니다.",
                    undo_directive=_undo_directive(decision.plan, reason, failure),
                    undo_plan=decision.plan,
                )
            undo_results = undo.get("results") or []

    if not is_active:
        # 재작업이 반복되어 고착화가 해제된 상태. 되돌리기까지만 결정론적으로 하고
        # 재실행은 에이전트에게 넘긴다.
        if not undo_results:
            return None
        logger.info(
            "되돌리기만 수행 | todo=%s undo=%d — 재실행은 DeepAgents가 담당",
            row.get("id"), len(undo_results),
        )
        return DeterministicExecution(
            mode="deterministic-undo-only",
            output={
                "ok": True,
                "execution_mode": "deterministic-undo-only",
                "llm_calls": 0,
                "undo_results": undo_results,
                "results": [],
                "note": "고착화가 해제되어 이전 실행만 되돌렸습니다. 이번 실행은 에이전트가 수행합니다.",
            },
            continue_to_llm=True,
        )

    # 입력 해석이나 실행이 실패하면 고착화를 포기하고 에이전트에게 넘긴다. 고착화는
    # 같은 일을 더 싸게 하는 최적화이지 업무 처리의 전제가 아니다 — 여기서 예외를
    # 올리면 워크아이템이 통째로 멈춘다.
    try:
        specification = record.get("parameters") or {}
        # 앞 액티비티의 산출물은 필요할 때만 읽는다. 대부분의 활동은 지시문과 워크아이템
        # 행만으로 입력이 채워진다.
        related = (
            await asyncio.to_thread(related_workitem_outputs, db, row)
            if _needs_upstream(specification)
            else []
        )
        inputs = _parameter_values(
            str(row.get("query") or ""), specification, row, related
        )
        forward = await _run_code(record["code"], inputs, config, tenant_id)
    except Exception:
        logger.warning(
            "결정론적 실행 실패 | todo=%s — DeepAgents 실행으로 폴백",
            row.get("id"), exc_info=True,
        )
        if not undo_results:
            return None
        # 되돌리기는 이미 끝났다. 그 사실을 결과 카드로 남기고 재실행만 에이전트가 맡는다.
        return DeterministicExecution(
            mode="deterministic-undo-only",
            output={
                "ok": True,
                "execution_mode": "deterministic-undo-only",
                "llm_calls": 0,
                "undo_results": undo_results,
                "results": [],
                "note": "이전 실행은 되돌렸지만 고착화 코드 재실행에 실패했습니다. 이번 실행은 에이전트가 수행합니다.",
            },
            continue_to_llm=True,
        )

    mode = "deterministic-undo" if undo_results else "deterministic"
    output = {
        "ok": True,
        "execution_mode": mode,
        "llm_calls": 0,
        "undo_results": undo_results,
        "results": forward.get("results") or [],
    }
    if undo_status:
        # 되돌릴 것이 없었는지, 되돌린 것인지를 기록에 남긴다. 결과만 보면 둘 다
        # "그냥 다시 실행됨"으로 보인다.
        output["undo_status"] = undo_status
    form_output = _render_form_output(
        record.get("output_template"), inputs, output["results"]
    )
    if form_output is None:
        # 옛 레코드에는 템플릿이 없다. 폼을 비운 채 완료시키면 다음 활동이 굶으므로,
        # 최소한 이번 실행의 사실이라도 남긴다 — 폼 아이디를 모르니 실행기가 워크아이템
        # 정의에서 찾도록 필드 없는 산출물로 넘긴다.
        logger.info(
            "산출물 템플릿 없음 | todo=%s — 실행 요약만 남긴다", row.get("id"),
        )
    logger.info(
        "결정론적 실행 완료 | todo=%s mode=%s undo=%d forward=%d",
        row.get("id"), mode, len(undo_results), len(output["results"]),
    )
    return DeterministicExecution(mode=mode, output=output, form_output=form_output)
