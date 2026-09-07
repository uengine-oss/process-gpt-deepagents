"""프로세스 정의 변환 헬퍼 (읽기 전용 · 순수 함수).

이 모듈은 **DB 에 접근하지 않는다.** 스킬/에이전트가 Supabase 에 직접 쓰는 것을
금지하는 원칙에 따라, 저장(proc_def/form_def/users/tenants.skills 등)은 전부
프론트가 사용자 자격증명으로 수행한다. 여기 남은 것은 그 저장 payload 를 만드는 데
쓰이는 순수 변환 함수뿐이다.

제공 함수:
- flatten(): elements[] 형식(02-generate-definition 규격) → ProcessGPT/완료엔진이
  소비하는 flattened 형식(activities/sequences/gateways/events/roles 분리 배열).
- html_to_fields_json(): 폼 HTML → [{key,text,type}] 필드 목록.
"""

from __future__ import annotations

import re
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bpmn.transform")

# ---------------------------------------------------------------------------
# elements[] → flattened 변환
# ---------------------------------------------------------------------------
_EVENT_TYPE_MAP = {
    "StartEvent": "startEvent",
    "EndEvent": "endEvent",
    "IntermediateCatchEvent": "intermediateCatchEvent",
    "IntermediateThrowEvent": "intermediateThrowEvent",
}
_GATEWAY_TYPE_MAP = {
    "ExclusiveGateway": "exclusiveGateway",
    "ParallelGateway": "parallelGateway",
    "InclusiveGateway": "inclusiveGateway",
}
_ACTIVITY_TYPE_MAP = {
    "UserActivity": "userTask",
    "ManualActivity": "manualTask",
    "ServiceActivity": "serviceTask",
    "ScriptActivity": "scriptTask",
}


def _as_props_string(value: Any) -> str:
    """properties 는 ProcessGPT 규격상 JSON 문자열로 저장한다."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value or "{}"
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return "{}"


def _as_int_duration(value: Any) -> Any:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value if value is not None else None


def flatten(process_definition: Dict[str, Any]) -> Dict[str, Any]:
    """elements[] 형식 processDefinition → flattened proc_def.definition.

    pdf2bpmn output/generated_procdef_from_procid.json 의 형태를 그대로 따른다.
    """
    pd = process_definition or {}
    proc_id = pd.get("processDefinitionId") or pd.get("processDefinitionName") or ""
    elements = pd.get("elements") or []

    activities: List[Dict[str, Any]] = []
    gateways: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    sequences: List[Dict[str, Any]] = []

    for el in elements:
        if not isinstance(el, dict):
            continue
        et = el.get("elementType")
        if et == "Activity":
            activities.append({
                "id": el.get("id"),
                "name": el.get("name"),
                "role": el.get("role"),
                "tool": el.get("tool"),
                "type": _ACTIVITY_TYPE_MAP.get(el.get("type"), "userTask"),
                "agent": el.get("agent"),
                "process": proc_id,
                "duration": _as_int_duration(el.get("duration")),
                "agentMode": el.get("agentMode") or "none",
                "skills": el.get("skills") or [],
                "inputData": el.get("inputData") or [],
                "outputData": el.get("outputData") or [],
                "properties": _as_props_string(el.get("properties")),
                "attachments": el.get("attachments") or [],
                "checkpoints": el.get("checkpoints") or [],
                "description": el.get("description") or "",
                "instruction": el.get("instruction") or "",
                "orchestration": el.get("orchestration"),
                "attachedEvents": el.get("attachedEvents"),
                "customProperties": el.get("customProperties") or [],
            })
        elif et == "Gateway":
            gateways.append({
                "id": el.get("id"),
                "name": el.get("name"),
                "role": el.get("role"),
                "type": _GATEWAY_TYPE_MAP.get(el.get("type"), "exclusiveGateway"),
                "process": proc_id,
                "conditionData": el.get("conditionData") or [],
                "properties": _as_props_string(el.get("properties")),
                "description": el.get("description") or "",
            })
        elif et == "Event":
            events.append({
                "id": el.get("id"),
                "name": el.get("name"),
                "role": el.get("role"),
                "type": _EVENT_TYPE_MAP.get(el.get("type"), "startEvent"),
                "process": proc_id,
                "trigger": el.get("trigger") or "",
                "properties": _as_props_string(el.get("properties")),
                "description": el.get("description") or "",
            })
        elif et == "Sequence":
            sequences.append({
                "id": el.get("id"),
                "name": el.get("name") or "",
                "source": el.get("source"),
                "target": el.get("target"),
                "condition": el.get("condition") or "",
                "properties": _as_props_string(el.get("properties")),
            })

    flat: Dict[str, Any] = {
        "data": pd.get("data") or [],
        "roles": pd.get("roles") or [],
        "events": events,
        "gateways": gateways,
        "sequences": sequences,
        "activities": activities,
        "description": pd.get("description") or "",
        "isHorizontal": pd.get("isHorizontal", True),
        "participants": pd.get("participants") or [],
        "subProcesses": pd.get("subProcesses") or [],
        "processDefinitionId": proc_id,
        "processDefinitionName": pd.get("processDefinitionName") or "",
    }
    # DMN(있으면) 그대로 보존
    if pd.get("dmn_decisions") is not None:
        flat["dmn_decisions"] = pd.get("dmn_decisions")
    if pd.get("dmn_rules") is not None:
        flat["dmn_rules"] = pd.get("dmn_rules")
    if pd.get("megaProcessId"):
        flat["megaProcessId"] = pd.get("megaProcessId")
    if pd.get("majorProcessId"):
        flat["majorProcessId"] = pd.get("majorProcessId")
    return flat


# ---------------------------------------------------------------------------
# form HTML → fields_json
# ---------------------------------------------------------------------------
_FIELD_TAG_TYPE = {
    "text-field": "text",
    "textarea-field": "textarea",
    "boolean-field": "boolean",
    "select-field": "select",
    "checkbox-field": "checkbox",
    "radio-field": "radio",
    "user-select-field": "user",
    "file-field": "file",
    "report-field": "report",
    "slide-field": "slide",
    "label-field": "label",
    "date-field": "date",
    "number-field": "number",
}


def html_to_fields_json(html: str) -> List[Dict[str, Any]]:
    """폼 HTML 에서 컴포넌트 필드를 추출해 [{key,text,type}] 로 만든다."""
    if not html:
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    tag_pattern = "|".join(re.escape(t) for t in _FIELD_TAG_TYPE)
    for m in re.finditer(rf"<({tag_pattern})\b([^>]*)>", html, re.IGNORECASE):
        tag = m.group(1).lower()
        attrs = m.group(2)
        name_m = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", attrs)
        if not name_m:
            continue
        key = name_m.group(1).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        alias_m = re.search(r"alias\s*=\s*['\"]([^'\"]+)['\"]", attrs)
        label_m = re.search(r"label\s*=\s*['\"]([^'\"]+)['\"]", attrs)
        text = (alias_m.group(1) if alias_m else (label_m.group(1) if label_m else key)).strip()
        ftype_m = re.search(r"\btype\s*=\s*['\"]([^'\"]+)['\"]", attrs)
        ftype = ftype_m.group(1).strip() if ftype_m else _FIELD_TAG_TYPE.get(tag, "text")
        field = {"key": key, "text": text, "type": ftype}
        if tag == "select-field":
            options: List[str] = []
            options_m = re.search(r"\boptions\s*=\s*['\"]([^'\"]+)['\"]", attrs)
            if options_m:
                options = [value.strip() for value in options_m.group(1).split("/") if value.strip()]
            else:
                close_at = html.lower().find("</select-field>", m.end())
                body = html[m.end():close_at] if close_at >= 0 else ""
                options = [
                    value.strip() for value in re.findall(
                        r"<option\b[^>]*\bvalue\s*=\s*['\"]([^'\"]+)['\"]", body, re.IGNORECASE
                    ) if value.strip()
                ]
            if options:
                field["options"] = options
        out.append(field)
    return out
