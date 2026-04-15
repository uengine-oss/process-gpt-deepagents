"""Pipeline helpers: validated pipeline specs → ordered steps → (optional) worker-style execution.

같은 파일에 ``StepExecutorRegistry`` / ``default_step_registry`` 및 단계별 TODO 빌더가 포함되어 있습니다.

이 모듈의 일부 함수(``order_pipeline_steps`` 등)는 기본 실행 경로에서도 유틸로 사용됩니다.

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
from typing import Any, Protocol

from app.executor.pipeline_validator import validate_pipeline_execution_state
from app.executor.todo_runner import run_todo_subagent_job
from app.runtime_config import ModelRoutingConfig

logger = logging.getLogger(__name__)


# --- Step registry (legacy external executor TODO builders) ---

PIPELINE_KIND_TO_OUTPUT_KEY: dict[str, str] = {
    "extract_structure": "template_schema",
    "extract_facts": "process_facts",
    "extract_reference_facts": "reference_facts",
    "map_to_template": "filled_template",
    "render_artifact": "artifact_file",
    "publish_artifact": "public_url",
}


class StepTaskBuilder(Protocol):
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]: ...


def _assets_by_id(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ar = spec.get("asset_roles") if isinstance(spec.get("asset_roles"), dict) else {}
    raw = ar.get("assets") if isinstance(ar.get("assets"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for a in raw:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if aid:
            out[aid] = a
    return out


def _asset_paths_line(spec: dict[str, Any]) -> str:
    lines: list[str] = []
    ar = spec.get("asset_roles") if isinstance(spec.get("asset_roles"), dict) else {}
    assets = ar.get("assets") if isinstance(ar.get("assets"), list) else []
    for a in assets:
        if not isinstance(a, dict):
            continue
        p = str(a.get("path") or "").strip()
        role = str(a.get("role") or "").strip()
        ext = str(a.get("type") or "").strip()
        aid = str(a.get("id") or "").strip()
        if p:
            lines.append(f"- {aid or '?'} role={role} type={ext} path={p}")
    return "\n".join(lines) if lines else "(no paths in asset_roles)"


def _state_excerpt(state: dict[str, Any], keys: list[str], max_len: int = 6000) -> str:
    blob: dict[str, Any] = {}
    for k in keys:
        if k in state:
            blob[k] = state[k]
    try:
        s = json.dumps(blob, ensure_ascii=False)
    except Exception:
        s = str(blob)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


class ExtractStructureBuilder:
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        aids = step.get("input_assets")
        if not isinstance(aids, list):
            aids = []
        by_id = _assets_by_id(pipeline_spec)
        chosen: list[dict[str, Any]] = []
        for x in aids:
            aid = str(x).strip()
            if aid in by_id:
                chosen.append(by_id[aid])
        if not chosen:
            ar = pipeline_spec.get("asset_roles") if isinstance(pipeline_spec.get("asset_roles"), dict) else {}
            assets = [a for a in (ar.get("assets") or []) if isinstance(a, dict)]
            tmpl = [a for a in assets if str(a.get("role")) == "template_candidate"]
            chosen = tmpl[:1] or assets[:1]
        path_hint = ""
        ext = "pdf"
        if chosen:
            path_hint = str(chosen[0].get("path") or "")
            ext = str(chosen[0].get("type") or "file").lower() or "file"
        content = (
            f"[IN={ext}][OUT=json] 입력 자산에서 문서·표·섹션 등 "
            f"구조적 스키마를 추출하여 JSON(template_schema)으로 정리한다. "
            f"우선 대상: {path_hint or 'asset_roles의 template/문서 자산'}\n\n"
            f"[ASSET_INDEX]\n{_asset_paths_line(pipeline_spec)}"
        )
        sid = str(step.get("id") or "extract_structure").strip()
        return {"id": sid, "status": "pending", "content": content, "input_type": ext, "output_type": "json"}


class ExtractFactsBuilder:
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        aids = step.get("input_assets")
        if not isinstance(aids, list):
            aids = []
        by_id = _assets_by_id(pipeline_spec)
        chosen: list[dict[str, Any]] = []
        for x in aids:
            aid = str(x).strip()
            if aid in by_id:
                chosen.append(by_id[aid])
        if not chosen:
            ar = pipeline_spec.get("asset_roles") if isinstance(pipeline_spec.get("asset_roles"), dict) else {}
            assets = [a for a in (ar.get("assets") or []) if isinstance(a, dict)]
            truth = [a for a in assets if str(a.get("role")) == "source_of_truth"]
            chosen = truth[:1] or assets[:1]
        path_hint = ""
        ext = "bpmn"
        if chosen:
            path_hint = str(chosen[0].get("path") or "")
            ext = str(chosen[0].get("type") or "bpmn").lower() or "bpmn"
        content = (
            f"[IN={ext}][OUT=json] 입력 자산에서 사실·프로세스 정보를 추출하여 "
            f"JSON(process_facts)으로 정리한다. 우선 대상: {path_hint or 'source_of_truth 자산'}\n\n"
            f"[ASSET_INDEX]\n{_asset_paths_line(pipeline_spec)}"
        )
        sid = str(step.get("id") or "extract_facts").strip()
        return {"id": sid, "status": "pending", "content": content, "input_type": ext, "output_type": "json"}


class MapToTemplateBuilder:
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        keys: list[str] = []
        raw = step.get("inputs")
        if isinstance(raw, list):
            keys = [str(x).strip() for x in raw if str(x).strip()]
        excerpt = _state_excerpt(state, keys)
        content = (
            "[IN=json][OUT=json] 아래 STATE 슬라이스의 template_schema·process_facts 등을 사용해 "
            "filled_template(JSON)을 생성한다(매핑·정합성·누락 필드 명시).\n\n"
            f"[STATE_SLICE keys={keys}]\n{excerpt}"
        )
        sid = str(step.get("id") or "map_to_template").strip()
        return {"id": sid, "status": "pending", "content": content, "input_type": "json", "output_type": "json"}


class RenderArtifactBuilder:
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        fmt = str(step.get("format") or "xlsx").strip().lower() or "xlsx"
        src_key = str(step.get("input") or "filled_template").strip() or "filled_template"
        excerpt = _state_excerpt(state, [src_key])
        content = (
            f"[IN=json][OUT={fmt}] STATE의 filled_template 등을 바탕으로 최종 산출 파일을 생성한다.\n\n"
            f"[STATE_SLICE key={src_key}]\n{excerpt}"
        )
        sid = str(step.get("id") or "render_artifact").strip()
        return {"id": sid, "status": "pending", "content": content, "input_type": "json", "output_type": fmt}


class PublishArtifactBuilder:
    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        src_key = str(step.get("input") or "artifact_file").strip() or "artifact_file"
        excerpt = _state_excerpt(state, [src_key])
        content = (
            "[IN=file][OUT=artifact] STATE에 기록된 산출 파일을 공개 스토리지에 업로드하고 "
            "`sandbox_upload_file_to_public_storage`로 public_url을 확보한다.\n\n"
            f"[STATE_SLICE key={src_key}]\n{excerpt}"
        )
        sid = str(step.get("id") or "publish_artifact").strip()
        return {"id": sid, "status": "pending", "content": content, "input_type": "file", "output_type": "artifact"}


class StepExecutorRegistry:
    """Registry of step kind → todo-shaped task builder."""

    def __init__(self) -> None:
        self._builders: dict[str, StepTaskBuilder] = {
            "extract_structure": ExtractStructureBuilder(),
            "extract_facts": ExtractFactsBuilder(),
            "map_to_template": MapToTemplateBuilder(),
            "render_artifact": RenderArtifactBuilder(),
            "publish_artifact": PublishArtifactBuilder(),
        }

    def build_todo_dict(
        self,
        *,
        step: dict[str, Any],
        pipeline_spec: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        kind = str(step.get("kind") or "").strip()
        b = self._builders.get(kind)
        if b is None:
            sid = str(step.get("id") or "unknown")
            return {
                "id": sid,
                "status": "pending",
                "content": f"[IN=text][OUT=text] 알 수 없는 step kind={kind!r}",
                "input_type": "text",
                "output_type": "text",
            }
        return b.build_todo_dict(step=step, pipeline_spec=pipeline_spec, state=state)


default_step_registry = StepExecutorRegistry()


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
    model_routing: ModelRoutingConfig | None = None,
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
            model_routing=model_routing,
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
