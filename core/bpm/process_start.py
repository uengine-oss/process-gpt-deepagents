"""채팅에서 프로세스를 시작할 때 지켜야 하는 두 가지.

※ process-gpt-mcp(work-assistant-agent/process-gpt-mcp/src/process_gpt_mcp/process_start.py)와
   **같은 내용의 사본**이다. 딥 에이전트는 운영에서 그 MCP 를 쓰지 않고 내장 도구로 실행하므로
   같은 규칙을 여기에도 둔다(패키지를 의존성으로 들이면 fastmcp 등이 딸려 온다). 한쪽을 고치면 다른 쪽도 고친다.

1. **시작은 첫 단계에서만.** 인스턴스는 시작 이벤트 바로 뒤의 액티비티에서 태어나고, 그 뒤
   단계(시스템 작업 · 검토 등)는 엔진이 이어서 진행한다. 에이전트가 뒤 단계의 activity_id 로
   execute_process 를 또 부르면 그 단계에서 시작하는 **별개의 인스턴스**가 생긴다 — 실제로
   '역할은 모두 나로, 바로 실행해줘' 한 번에 인스턴스 셋이 생겼다(첫 단계 · 문서 생성 · 검사).

2. **같은 실행을 두 번 하지 않는다.** 에이전트가 결과를 확인하려고, 혹은 응답이 늦어 다시
   부르면 같은 값으로 인스턴스가 하나 더 생긴다. 방금 같은 사람이 같은 프로세스를 같은 첫 폼
   값으로 시작했다면 새로 만들지 않고 그 인스턴스를 돌려준다.

MCP 서버 한 곳에서 막으면 이 서버를 쓰는 에이전트(딥 에이전트 · work-assistant · cli-agent)가
모두 같은 규칙을 따른다. 프롬프트만으로는 모델이 어기는 것을 막을 수 없다.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: 같은 실행으로 보는 시간. 에이전트의 재호출은 초~분 단위로 일어난다.
DUPLICATE_WINDOW_MINUTES = 10

_START_TYPES = {"startEvent", "start_event", "StartEvent"}


def _items(definition: dict, *keys: str) -> list[dict]:
    out: list[dict] = []
    for key in keys:
        value = definition.get(key)
        if isinstance(value, list):
            out.extend(v for v in value if isinstance(v, dict))
    return out


def _is_start_event(node: dict) -> bool:
    return str(node.get("type") or "") in _START_TYPES


def start_activities(definition: Any) -> list[dict]:
    """시작 이벤트 바로 뒤의 액티비티들(보통 하나).

    시작 이벤트는 정의에 따라 events 에도, gateways 에도 담겨 온다. 시작 이벤트 뒤에
    게이트웨이가 있으면 그 게이트웨이를 건너 첫 액티비티를 찾는다. 흐름으로 찾지 못하면
    activities 의 첫 항목을 쓴다(흐름이 끊긴 정의라도 실행은 막지 않는다).
    """
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError:
            return []
    if not isinstance(definition, dict):
        return []

    activities = _items(definition, "activities", "subProcesses")
    by_id = {str(a.get("id")): a for a in activities if a.get("id")}
    nodes = _items(definition, "events", "gateways")
    starts = [str(n.get("id")) for n in nodes if _is_start_event(n) and n.get("id")]
    gateway_ids = {str(n.get("id")) for n in nodes if not _is_start_event(n) and n.get("id")}

    next_of: dict[str, list[str]] = {}
    for seq in _items(definition, "sequences"):
        src, tgt = str(seq.get("source") or ""), str(seq.get("target") or "")
        if src and tgt:
            next_of.setdefault(src, []).append(tgt)

    found: list[dict] = []
    seen: set[str] = set()
    stack = [t for s in starts for t in next_of.get(s, [])]
    while stack:
        node = stack.pop(0)
        if node in seen:
            continue
        seen.add(node)
        if node in by_id:
            found.append(by_id[node])
        elif node in gateway_ids:
            stack.extend(next_of.get(node, []))

    if not found and activities:
        found = [activities[0]]
    return found


def form_key_of(activity: dict) -> str:
    """액티비티가 쓰는 폼 id. tool 이 formHandler:<id> 로 가리킨다."""
    tool = str((activity or {}).get("tool") or "")
    return tool.split("formHandler:", 1)[1] if tool.startswith("formHandler:") else ""


def describe_start(definition: Any) -> list[dict]:
    """get_process_detail 이 함께 돌려줄 시작 단계 요약."""
    return [
        {
            "activity_id": str(a.get("id") or ""),
            "name": a.get("name") or "",
            "role": a.get("role") or "",
            "form_key": form_key_of(a),
        }
        for a in start_activities(definition)
    ]


def check_start_activity(definition: Any, activity_id: str) -> dict | None:
    """activity_id 가 시작 단계가 아니면 거절 응답을, 맞으면 None 을.

    정의를 읽지 못해 시작 단계를 모르면 막지 않는다 — 판단할 근거 없이 실행을 막으면
    멀쩡한 요청까지 실패한다.
    """
    starts = describe_start(definition)
    if not starts:
        return None
    ids = {s["activity_id"] for s in starts}
    if str(activity_id or "") in ids:
        return None
    first = starts[0]
    return {
        "error": "시작 단계가 아닌 액티비티로는 프로세스를 실행할 수 없습니다.",
        "requested_activity_id": activity_id,
        "start_activity_id": first["activity_id"],
        "start_form_key": first["form_key"],
        "hint": (
            "execute_process 는 프로세스를 새로 시작하는 도구이며 첫 단계로만 호출합니다. "
            "이후 단계(시스템 작업·검토 등)는 엔진이 이어서 진행하므로 execute_process 로 "
            "이어서 실행하지 마세요. 이미 이 요청으로 실행한 인스턴스가 있다면 다시 호출하지 말고 "
            "그 결과로 답을 마치세요."
        ),
    }


def normalize_form_key(form_key: Any) -> str:
    """에이전트가 액티비티의 tool 값('formHandler:<id>')을 그대로 넘겨도 폼 id 로."""
    key = str(form_key or "").strip()
    return key.split("formHandler:", 1)[1] if key.startswith("formHandler:") else key


_ROLE_NAME_KEYS = ("name", "role", "role_name", "roleName")
_ROLE_WHO_KEYS = ("endpoint", "assignees", "assignee", "user_ids", "user_id", "uid", "users", "value")


def _who(mapping: dict) -> list[str]:
    for key in _ROLE_WHO_KEYS:
        if key not in mapping:
            continue
        value = mapping[key]
        items = value if isinstance(value, list) else [value]
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                item = item.get("id") or item.get("uid") or item.get("user_id") or item.get("email") or ""
            text = str(item or "").strip()
            if text:
                out.append(text)
        if out:
            return out
    return []


def _as_endpoint(ids: list[str]) -> Any:
    # 엔진은 문자열 하나와 목록 모두 받는다. 한 사람이면 문자열로 둔다(정의 실행 화면과 같은 꼴).
    if not ids:
        return ""
    return ids[0] if len(ids) == 1 else ids


def normalize_role_mappings(
    role_mappings: Any,
    definition: Any,
    *,
    user_uid: str = "",
    start_role: str = "",
) -> list[dict]:
    """역할 배정을 엔진이 읽는 꼴({name, endpoint, default, resolutionRule})로 맞춘다.

    에이전트는 대화에서 모은 배정을 제멋대로의 모양으로 넘긴다 — 실제로
    `{"role": "사용자", "assignees": ["<uid>"]}` 로 넘겨, 엔진이 name · endpoint 를 찾지 못하고
    참여자를 None 으로 저장했다. 그 인스턴스는 첫 단계부터 검증 오류로 멈춘다.

    정의의 역할을 기준으로 채운다: 넘겨받은 배정 → (시작 단계의 역할이면) 실행한 사용자 →
    정의의 기본값 순. 정의에 없는 역할 이름으로 넘어온 배정도 버리지 않고 뒤에 붙인다.
    """
    provided: dict[str, list[str]] = {}
    order: list[str] = []
    for mapping in role_mappings if isinstance(role_mappings, list) else []:
        if not isinstance(mapping, dict):
            continue
        name = next((str(mapping[k]).strip() for k in _ROLE_NAME_KEYS if mapping.get(k)), "")
        if not name:
            continue
        provided[name] = _who(mapping)
        order.append(name)

    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except json.JSONDecodeError:
            definition = None
    roles = [r for r in (definition or {}).get("roles") or [] if isinstance(r, dict) and r.get("name")] if isinstance(definition, dict) else []

    out: list[dict] = []
    seen: set[str] = set()
    for role in roles:
        name = str(role["name"])
        seen.add(name)
        default = role.get("default") or ""
        ids = provided.get(name) or []
        if not ids and start_role and name == start_role and user_uid:
            ids = [user_uid]
        if not ids:
            ids = _who({"endpoint": role.get("endpoint")}) or _who({"endpoint": default})
        out.append(
            {
                "name": name,
                "endpoint": _as_endpoint(ids),
                "resolutionRule": role.get("resolutionRule"),
                "default": default,
            }
        )
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "endpoint": _as_endpoint(provided[name]), "resolutionRule": None, "default": ""})
    return out


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str):
        return value.strip()
    return value


def same_form_values(stored: Any, submitted: Any) -> bool:
    """첫 폼 값이 같은가. 빈 값 · 앞뒤 공백 · 키 순서 차이는 같은 것으로 본다."""
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except json.JSONDecodeError:
            return False
    return _normalize(stored or {}) == _normalize(submitted or {})


def find_duplicate(rows: Iterable[dict], form_key: str, form_values: Any) -> str | None:
    """방금 같은 값으로 시작한 인스턴스 id. rows 는 같은 사용자·정의·시작 단계의 최근 업무."""
    for row in rows or []:
        output = row.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                continue
        if not isinstance(output, dict):
            continue
        stored = output.get(form_key) if form_key in output else None
        if stored is None:
            continue
        if same_form_values(stored, form_values) and row.get("proc_inst_id"):
            return str(row["proc_inst_id"])
    return None
