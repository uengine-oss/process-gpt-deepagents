"""Legacy: validated pipeline specs → TODO 텍스트 → 외부 워커(``run_todo_subagent_job``).

기본 실행 경로에서는 ``deepagents_executor``가 이 모듈을 호출하지 않습니다.
``DEEPAGENTS_LEGACY_EXTERNAL_EXECUTOR=1`` 일 때만 사용됩니다.

State contract (keys produced in ``state`` as steps complete):

- ``template_schema``, ``process_facts``, ``filled_template``: dict-like payloads
  with optional ``text``, ``data`` (parsed JSON), ``status``.
- ``artifact_file``: same shape; often holds render step output / path hints in ``text``.
- ``public_url``: final publish step stores ``public_url`` on the payload dict when available.

After all steps succeed, ``validate_pipeline_execution_state`` requires a non-empty ``public_url``
somewhere in state when the spec included ``render_artifact``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.executor.pipeline_validator import validate_pipeline_execution_state
from app.executor.step_executor_registry import StepExecutorRegistry, default_step_registry
from app.executor.todo_runner import run_todo_subagent_job

logger = logging.getLogger(__name__)


def _steps_list(spec: dict[str, Any]) -> list[dict[str, Any]]:
    raw = spec.get("steps")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def _all_output_keys(steps: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for s in steps:
        ok = s.get("output_key")
        if ok:
            out.add(str(ok))
    return out


def _state_deps(step: dict[str, Any], producible: set[str]) -> set[str]:
    d: set[str] = set()
    for k in step.get("inputs") or []:
        ks = str(k).strip()
        if ks in producible:
            d.add(ks)
    one = step.get("input")
    if one is not None:
        ks = str(one).strip()
        if ks in producible:
            d.add(ks)
    return d


def order_pipeline_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Topological order by state-key dependencies; None if unsatisfiable cycle."""
    producible = _all_output_keys(steps)
    remaining = list(steps)
    ordered: list[dict[str, Any]] = []
    satisfied: set[str] = set()

    while remaining:
        progressed = False
        for s in list(remaining):
            needs = _state_deps(s, producible)
            if not needs.issubset(satisfied):
                continue
            ordered.append(s)
            remaining.remove(s)
            ok = s.get("output_key")
            if ok:
                satisfied.add(str(ok))
            progressed = True
        if not progressed:
            return None
    return ordered


def _store_step_output(
    *,
    state: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
) -> None:
    ok = step.get("output_key")
    if not ok:
        return
    key = str(ok).strip()
    if not key:
        return
    payload: dict[str, Any] = {
        "text": result.get("output"),
        "status": result.get("status"),
    }
    if isinstance(result.get("public_url"), str) and result["public_url"].strip():
        payload["public_url"] = result["public_url"].strip()
    # Heuristic: parse JSON text into object for downstream STATE_SLICE
    txt = payload.get("text")
    if isinstance(txt, str) and txt.strip().startswith(("{", "[")):
        try:
            payload["data"] = json.loads(txt)
        except Exception:
            pass
    state[key] = payload


async def execute_pipeline_spec(
    *,
    pipeline_spec: dict[str, Any],
    invoke_user_text: str,
    model_name: str,
    subagents: list[dict[str, Any]],
    name_to_exec_ctx: dict[str, Any],
    event_queue: Any,
    proc_inst_id: str,
    task_id: str,
    registry: StepExecutorRegistry | None = None,
    name_to_subagent_job_id: dict[str, str] | None = None,
    shared_job_tool_log: dict[str, list[str]] | None = None,
    shared_job_skill_log: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run steps in dependency order; return (state, run_info)."""
    reg = registry or default_step_registry
    steps = _steps_list(pipeline_spec)
    if not steps:
        return {}, {"executed": 0, "results": {}, "error": "no_steps"}

    ordered = order_pipeline_steps(steps)
    if ordered is None:
        return {}, {"executed": 0, "results": {}, "error": "pipeline_cycle_or_missing_dependency"}

    state: dict[str, Any] = {}
    results: dict[str, Any] = {}
    step_results: dict[str, Any] = {}

    for step in ordered:
        sid = str(step.get("id") or uuid.uuid4())
        td = reg.build_todo_dict(step=step, pipeline_spec=pipeline_spec, state=state)
        td.setdefault("id", sid)
        step_results_map: dict[str, Any] = {}
        await run_todo_subagent_job(
            td=td,
            results=step_results_map,
            invoke_user_text=invoke_user_text,
            model_name=model_name,
            subagents=subagents,
            name_to_exec_ctx=name_to_exec_ctx,
            event_queue=event_queue,
            proc_inst_id=proc_inst_id,
            task_id=task_id,
            task_label=f"PIPELINE:{str(step.get('kind') or '')}",
            name_to_subagent_job_id=name_to_subagent_job_id,
            shared_job_tool_log=shared_job_tool_log,
            shared_job_skill_log=shared_job_skill_log,
        )
        tid = str(td.get("id") or sid)
        res = step_results_map.get(tid, {})
        step_results[str(sid)] = res
        results[str(sid)] = res
        if res.get("status") != "completed":
            return state, {
                "executed": len(step_results),
                "results": results,
                "failed_step": sid,
                "error": res.get("error") or "step_failed",
            }
        _store_step_output(state=state, step=step, result=res)

    ok_final, final_errs = validate_pipeline_execution_state(pipeline_spec, state)
    if not ok_final:
        return state, {
            "executed": len(ordered),
            "results": results,
            "state": state,
            "error": "execution_validation_failed",
            "execution_validation_errors": final_errs,
        }

    return state, {"executed": len(ordered), "results": results, "state": state}
