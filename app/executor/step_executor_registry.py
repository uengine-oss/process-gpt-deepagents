"""Pipeline step contracts and (legacy) natural-language TODO builders.

기본 실행 경로는 DeepAgents ``task()`` 디스패치입니다. ``build_todo_dict`` 계열은
``DEEPAGENTS_LEGACY_EXTERNAL_EXECUTOR`` 모드에서만 파이프라인 러너가 사용합니다.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

# kind → 그래프 state에 기대되는 산출 키(계약). 검증/프롬프트 정렬용.
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
        # Fallback: first template_candidate or first asset
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
        # note: fmt may be xlsx, csv, etc.


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
