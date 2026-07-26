from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EXCLUDED_TOOLS = {"mem0", "memento", "human_asked", "dmn_rule"}


@dataclass(frozen=True)
class DeterministicExecution:
    mode: str
    output: dict[str, Any]

    @property
    def result_text(self) -> str:
        return json.dumps(self.output, ensure_ascii=False)


def _enabled_servers(tenant_mcp: dict[str, Any]) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    for name, config in (tenant_mcp.get("mcpServers") or {}).items():
        if not isinstance(config, dict) or config.get("enabled", True) is False:
            continue
        servers[name] = {k: v for k, v in config.items() if k != "enabled"}
    return {"mcpServers": servers}


def _parameter_values(query: str, specification: Any) -> dict[str, Any]:
    """Extract replay values without an LLM.

    Generated parameter specs contain names, types, and examples. Korean workitem
    instructions conventionally express values as ``label은 value``; the fallbacks
    below also cover quoted strings and numbers in their original order.
    """
    params = specification.get("parameters", []) if isinstance(specification, dict) else []
    values: dict[str, Any] = {}
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", query)
    numbers = re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", query)
    quoted_i = number_i = 0

    aliases = {
        "product_name": ("product name", "product", "상품명", "상품", "제품명", "제품"),
        "stock_increment": ("stock increment", "입고 수량", "증가량", "수량"),
        "stock_quantity": ("stock quantity", "재고 수량", "수량"),
        "new_stock": ("new stock", "반영 후 재고", "새 재고", "재고"),
        "reason": ("reason", "변경 사유", "사유"),
    }
    for param in params:
        name = str(param.get("name") or "")
        if not name:
            continue
        ptype = str(param.get("type") or "string").lower()
        labels = aliases.get(name, ()) + (name.replace("_", " "), name)
        raw: str | None = None
        for label in labels:
            match = re.search(
                rf"(?:{re.escape(label)})\s*(?:은|는|이|가|:|=)\s*"
                r"['\"]?([^,'\".;\n]+)",
                query,
                flags=re.IGNORECASE,
            )
            if match:
                raw = match.group(1).strip()
                break
        if raw is None and name == "product_name":
            match = re.search(r"^\s*['\"]?([^,'\"\s]+)['\"]?\s*(?:상품|제품)", query)
            if match:
                raw = match.group(1).strip()
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
    events = (
        db.table("events")
        .select("timestamp,data")
        .eq("todo_id", previous[0]["id"])
        .eq("event_type", "tool_usage_finished")
        .order("timestamp")
        .execute()
        .data
        or []
    )
    logs = []
    for event in events:
        data = event.get("data") or {}
        if isinstance(data, str):
            data = json.loads(data)
        tool = data.get("tool_name") if isinstance(data, dict) else None
        if not tool or tool in _EXCLUDED_TOOLS:
            continue
        args = data.get("args") or {}
        sql = args.get("sql") or args.get("query") or ""
        if tool == "execute_sql" and str(sql).lstrip().upper().startswith("SELECT"):
            continue
        logs.append({"timestamp": event.get("timestamp"), "log_data": data})
    return logs


async def _run_code(code: str, inputs: dict[str, Any], mcp_config: dict[str, Any]) -> dict[str, Any]:
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
        lines = stdout.decode("utf-8", errors="replace").strip().splitlines()
        if proc.returncode or not lines:
            detail = stderr.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"결정론적 코드 실행 실패({proc.returncode}): {detail}")
        result = json.loads(lines[-1])
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(f"결정론적 코드가 실패 결과를 반환했습니다: {result}")
        return result
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


async def try_deterministic_execution(
    row: dict[str, Any], tenant_mcp: dict[str, Any]
) -> DeterministicExecution | None:
    """Execute a stored route for a worklist item, or return None for LLM fallback."""
    if not all(row.get(key) for key in ("proc_def_id", "activity_id", "tenant_id")):
        return None
    from core.storage.db import get_supabase

    db = get_supabase()
    try:
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
    if not records or not records[0].get("code"):
        return None

    record = records[0]
    config = _enabled_servers(tenant_mcp)
    undo_results: list[Any] = []
    is_rework = int(row.get("rework_count") or 0) > 0
    if is_rework and record.get("compensation"):
        event_logs = await asyncio.to_thread(_previous_event_logs, db, row)
        if event_logs:
            undo = await _run_code(record["compensation"], {"event_logs": event_logs}, config)
            undo_results = undo.get("results") or []

    inputs = _parameter_values(str(row.get("query") or ""), record.get("parameters") or {})
    forward = await _run_code(record["code"], inputs, config)
    mode = "deterministic-undo" if undo_results else "deterministic"
    output = {
        "ok": True,
        "execution_mode": mode,
        "llm_calls": 0,
        "undo_results": undo_results,
        "results": forward.get("results") or [],
    }
    logger.info(
        "결정론적 실행 완료 | todo=%s mode=%s undo=%d forward=%d",
        row.get("id"), mode, len(undo_results), len(output["results"]),
    )
    return DeterministicExecution(mode=mode, output=output)
