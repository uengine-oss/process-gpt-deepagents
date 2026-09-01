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

from .work_history import normalize_events, to_log_entries

logger = logging.getLogger(__name__)

# 셸·파일 조작이 들어간 코드는 워크스페이스 안에서 돌아야 한다. 생성 코드가 쓰는
# 원시 동작으로 판별한다 — 저장된 이력이 없는 옛 레코드에도 통한다.
_WORKSPACE_PRIMITIVES = {"run_shell", "write_file", "edit_file", "read_file"}
_CONTAINER_SCRATCH = "/workspace/.deterministic"


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

    for match in re.finditer(r"\{.*\}", query or "", re.DOTALL):
        try:
            _walk(json.loads(match.group()))
        except (ValueError, TypeError):
            continue
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


def _parameter_values(
    query: str, specification: Any, row: dict[str, Any] | None = None
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


def _previous_event_logs(db: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
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
        return []
    # event_type으로 미리 거르지 않는다. 런타임마다 "도구 사용이 끝났다"를 뜻하는
    # 타입 이름이 다르고, 하나로 못박으면 다른 러너의 이력이 통째로 사라진다.
    events = (
        db.table("events")
        .select("timestamp,event_type,data")
        .eq("todo_id", previous[0]["id"])
        .order("timestamp")
        .execute()
        .data
        or []
    )
    # 맥락 행위(읽은 스킬·파일, 조회)도 함께 넘긴다. 무엇을 되돌릴지는 부수효과가
    # 정하지만, 어떤 순서로 어떻게 되돌릴지는 그 절차를 알아야 정할 수 있다.
    trace = normalize_events(events)
    if not any(action.has_effect for action in trace):
        return []
    return to_log_entries(trace)


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


async def _run_code(
    code: str, inputs: dict[str, Any], mcp_config: dict[str, Any], tenant_id: str = ""
) -> dict[str, Any]:
    if _needs_workspace(code):
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
    if rework_count > 0 and record.get("compensation"):
        event_logs = await asyncio.to_thread(_previous_event_logs, db, row)
        if event_logs:
            try:
                undo = await _run_code(
                    record["compensation"], {"event_logs": event_logs}, config, tenant_id
                )
            except Exception:
                # 되돌리기에 실패했다면 이전 실행의 흔적이 어디까지 남았는지 알 수 없다.
                # 그 위에 고착화 코드를 덧씌우면 중복 반영이 된다. 재작업 전체를
                # 에이전트에게 넘긴다 — 고착화 이전과 같은 동작이다.
                logger.warning(
                    "보상 코드 실행 실패 | todo=%s — 재작업 전체를 DeepAgents가 담당",
                    row.get("id"), exc_info=True,
                )
                return None
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
        inputs = _parameter_values(
            str(row.get("query") or ""), record.get("parameters") or {}, row
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
