"""core/strategy/tools.py — 전략(Strategy) 마이크로서비스 호출 도구.

services/strategy (FastAPI, 인증 없음·tenant_id 쿼리 파라미터로만 테넌트 구분)를
HTTP 로 호출해 BSC 전략목표(Objective)/KPI/BMC 캔버스 블록을 생성하고,
KPI 를 프로세스 정의와 연결한다.

base URL 은 환경변수 STRATEGY_SERVICE_URL (기본값 "http://localhost:8014")에서 읽는다.
모든 도구는 HTTP 오류(4xx/5xx)나 커넥션 실패를 잡아 사람이 읽을 수 있는 에러 메시지를
담은 dict({"error": ...})를 반환한다 — 예외를 그대로 던지지 않아 에이전트 실행이
죽지 않는다.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_STRATEGY_SERVICE_URL = os.getenv("STRATEGY_SERVICE_URL", "http://localhost:8014")
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


def _err(msg: str) -> dict:
    logger.warning("[strategy tools] %s", msg)
    return {"error": msg}


async def _request(
    method: str,
    path: str,
    *,
    params: dict,
    json_body: Optional[dict] = None,
) -> dict:
    """전략 서비스에 HTTP 요청을 보내고 JSON 응답을 dict 로 반환한다.

    HTTP 오류(4xx/5xx)나 커넥션 실패는 예외를 던지지 않고 {"error": ...} 로 변환한다.
    """
    url = f"{_STRATEGY_SERVICE_URL.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, params=params, json=json_body)
            resp.raise_for_status()
            if not resp.content:
                return {}
            return resp.json()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.text
        except Exception:
            pass
        return _err(
            f"전략 서비스 호출 실패 ({e.response.status_code}): {method} {path} — {detail[:500]}"
        )
    except httpx.RequestError as e:
        return _err(f"전략 서비스 연결 실패: {method} {path} — {e}")
    except Exception as e:  # noqa: BLE001 — 에이전트 실행을 죽이지 않기 위해 전부 흡수
        return _err(f"전략 서비스 호출 중 알 수 없는 오류: {method} {path} — {e}")


# ---------------------------------------------------------------------------
# 1. save_strategy_objective
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def save_strategy_objective(
    name: str,
    description: str,
    perspective: str,
    parents: list[str],
    tenant_id: str,
) -> dict:
    """새 BSC 전략목표(Objective)를 전략 서비스에 저장한다.

    전략 서비스(POST /api/objectives)를 호출해 전략목표를 생성하고,
    생성된 objective id 를 포함한 결과를 반환한다.

    Args:
        name: 전략목표 이름.
        description: 전략목표 설명.
        perspective: BSC 관점. "financial" | "customer" | "internal_process" | "learning_growth" 중 하나.
        parents: 인과관계상 이 목표의 상위(원인) objective id 목록. 없으면 빈 리스트.
        tenant_id: 테넌트 ID.

    Returns:
        생성된 objective(id 포함) 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not name:
        return _err("name이 비어 있습니다.")
    body = {
        "name": name,
        "description": description or None,
        "perspective": perspective,
        "parents": parents or [],
    }
    return await _request(
        "POST", "/api/objectives", params={"tenant_id": tenant_id}, json_body=body
    )


# ---------------------------------------------------------------------------
# 2. save_strategy_kpi
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def save_strategy_kpi(
    objective_id: str,
    name: str,
    description: str,
    kpi_role: str,
    unit: str,
    measure_type: str,
    direction: str,
    baseline_value: Optional[float],
    target_value: Optional[float],
    tenant_id: str,
) -> dict:
    """전략목표에 속한 KPI 를 전략 서비스에 저장한다.

    전략 서비스(POST /api/kpis)를 호출해 KPI 를 생성하고,
    생성된 kpi id 를 포함한 결과를 반환한다.

    Args:
        objective_id: 이 KPI가 속한 전략목표(objective) id.
        name: KPI 이름.
        description: KPI 설명.
        kpi_role: 선행/후행 구분. "leading" 또는 "lagging".
        unit: 측정 단위(예: "%", "건", "원").
        measure_type: 측정 방식(예: "manual", "process", "survey", "external_source").
        direction: 목표 방향. "increase" 또는 "decrease".
        baseline_value: 기준값. 없으면 None.
        target_value: 목표값. 없으면 None.
        tenant_id: 테넌트 ID.

    Returns:
        생성된 kpi(id 포함) 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not objective_id or not name:
        return _err("objective_id와 name은 필수입니다.")
    body = {
        "objective_id": objective_id,
        "name": name,
        "description": description or None,
        "unit": unit or None,
        "measure_type": measure_type,
        "kpi_role": kpi_role or None,
        "direction": direction,
        "baseline_value": baseline_value,
        "target_value": target_value,
    }
    return await _request(
        "POST", "/api/kpis", params={"tenant_id": tenant_id}, json_body=body
    )


# ---------------------------------------------------------------------------
# 3. save_canvas_block
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def save_canvas_block(
    block_type: str,
    items: list[str],
    notes: Optional[str],
    tenant_id: str,
) -> dict:
    """비즈니스 모델 캔버스(BMC)의 블록 1개를 upsert 한다.

    전략 서비스(POST /api/canvas/blocks)를 호출한다. 같은 block_type 으로
    다시 호출하면 기존 블록 내용을 덮어쓴다(upsert).

    Args:
        block_type: BMC 블록 종류(예: key_partners, key_activities, value_propositions,
            customer_relationships, customer_segments, key_resources, channels,
            cost_structure, revenue_streams 등 9블록 중 하나).
        items: 이 블록에 담을 항목 목록.
        notes: 블록에 대한 추가 메모. 없으면 None.
        tenant_id: 테넌트 ID.

    Returns:
        upsert 된 canvas block 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not block_type:
        return _err("block_type은 필수입니다.")
    body = {
        "block_type": block_type,
        "items": items or [],
        "notes": notes or None,
    }
    return await _request(
        "POST", "/api/canvas/blocks", params={"tenant_id": tenant_id}, json_body=body
    )


# ---------------------------------------------------------------------------
# 4. get_strategy_map / get_strategy_canvas — 기존 전략 존재 여부 확인용(읽기 전용)
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def get_strategy_map(tenant_id: str) -> dict:
    """이 테넌트에 이미 저장된 BSC 전략맵(전략목표+KPI 전체)을 조회한다.

    새 전략 인터뷰를 시작하기 전에 반드시 먼저 호출해서 기존 전략이 있는지
    확인해야 한다 — 이미 있는데도 처음부터 다시 인터뷰를 진행하면 objective가
    중복 생성된다. 결과가 비어 있지 않으면(objectives 배열에 항목이 있으면)
    사용자에게 기존 전략을 보여주고 "수정" 또는 "새로 시작"(기존 대체) 중
    선택하게 한 뒤 진행해야 한다.

    Args:
        tenant_id: 테넌트 ID.

    Returns:
        {"objectives": [{"id","name","description","perspective","parents",
        "kpis":[...]}]} 형태, 또는 {"error": ...}. objectives가 빈 리스트면
        기존 전략이 없는 것이다.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    return await _request("GET", "/api/map", params={"tenant_id": tenant_id})


@tool(parse_docstring=True)
async def get_strategy_canvas(tenant_id: str) -> dict:
    """이 테넌트에 이미 저장된 비즈니스 모델 캔버스(BMC) 9블록을 조회한다.

    get_strategy_map 과 함께, 기존 전략 존재 여부를 확인할 때 같이 호출한다.

    Args:
        tenant_id: 테넌트 ID.

    Returns:
        블록별 내용 dict 또는 {"error": ...}. 비어 있으면 기존 캔버스가 없는 것이다.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    return await _request("GET", "/api/canvas", params={"tenant_id": tenant_id})


# ---------------------------------------------------------------------------
# 5. update_strategy_objective / update_strategy_kpi — 기존 전략 수정용
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def update_strategy_objective(
    objective_id: str,
    tenant_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    perspective: Optional[str] = None,
    parents: Optional[list[str]] = None,
) -> dict:
    """기존 BSC 전략목표(Objective)를 수정한다(전체 교체 방식 PUT).

    name/description/perspective/parents 중 값을 준 필드만 바뀌고, None으로 둔
    필드는 기존 값을 그대로 유지한다(내부적으로 get_strategy_map 으로 현재 값을
    먼저 읽어 병합한 뒤 저장) — 그래서 이름만 고치고 싶을 때 parents 같은 다른
    필드를 몰라도 안전하게 호출할 수 있다.

    Args:
        objective_id: 수정할 전략목표 id.
        tenant_id: 테넌트 ID.
        name: 새 이름. 유지하려면 None.
        description: 새 설명. 유지하려면 None.
        perspective: 새 BSC 관점("financial"|"customer"|"internal_process"|"learning_growth"). 유지하려면 None.
        parents: 새 상위(원인) objective id 목록. 유지하려면 None.

    Returns:
        수정된 objective 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not objective_id:
        return _err("objective_id가 비어 있습니다.")

    needs_lookup = name is None or description is None or perspective is None or parents is None
    existing: Optional[dict] = None
    if needs_lookup:
        map_result = await _request("GET", "/api/map", params={"tenant_id": tenant_id})
        if isinstance(map_result, dict) and map_result.get("error"):
            return map_result
        for objective in map_result.get("objectives") or []:
            if objective.get("id") == objective_id:
                existing = objective
                break
        if existing is None:
            return _err(f"objective_id '{objective_id}'를 전략맵에서 찾을 수 없습니다.")

    body = {
        "name": name if name is not None else (existing or {}).get("name"),
        "description": description if description is not None else (existing or {}).get("description"),
        "perspective": perspective if perspective is not None else (existing or {}).get("perspective"),
        "parents": parents if parents is not None else ((existing or {}).get("parents") or []),
    }
    return await _request(
        "PUT", f"/api/objectives/{objective_id}", params={"tenant_id": tenant_id}, json_body=body
    )


@tool(parse_docstring=True)
async def update_strategy_kpi(
    kpi_id: str,
    tenant_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    kpi_role: Optional[str] = None,
    unit: Optional[str] = None,
    measure_type: Optional[str] = None,
    direction: Optional[str] = None,
    baseline_value: Optional[float] = None,
    target_value: Optional[float] = None,
) -> dict:
    """기존 KPI를 수정한다(전체 교체 방식 PUT, 안 넘긴 필드는 기존 값 유지).

    objective_id/proc_def_id/survey_questions 등 여기 파라미터에 없는 필드는
    항상 기존 값을 그대로 보존한다(먼저 get_strategy_map 으로 현재 값을 읽어
    병합) — 그래서 target_value 하나만 고치고 싶을 때도 이미 연결된
    proc_def_id 같은 값을 실수로 지우지 않는다.

    Args:
        kpi_id: 수정할 KPI id.
        tenant_id: 테넌트 ID.
        name: 새 이름. 유지하려면 None.
        description: 새 설명. 유지하려면 None.
        kpi_role: "leading" 또는 "lagging". 유지하려면 None.
        unit: 측정 단위. 유지하려면 None.
        measure_type: 측정 방식. 유지하려면 None.
        direction: "increase" 또는 "decrease". 유지하려면 None.
        baseline_value: 기준값. 유지하려면 None.
        target_value: 목표값. 유지하려면 None.

    Returns:
        수정된 kpi 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not kpi_id:
        return _err("kpi_id가 비어 있습니다.")

    map_result = await _request("GET", "/api/map", params={"tenant_id": tenant_id})
    if isinstance(map_result, dict) and map_result.get("error"):
        return map_result

    existing: Optional[dict] = None
    for objective in map_result.get("objectives") or []:
        for kpi in objective.get("kpis") or []:
            if kpi.get("id") == kpi_id:
                existing = kpi
                break
        if existing is not None:
            break
    if existing is None:
        return _err(f"kpi_id '{kpi_id}'를 전략맵에서 찾을 수 없습니다.")

    def _pick(new_value, key):
        return new_value if new_value is not None else existing.get(key)

    body = {
        "objective_id": existing.get("objective_id"),
        "name": _pick(name, "name"),
        "description": _pick(description, "description"),
        "unit": _pick(unit, "unit"),
        "measure_type": _pick(measure_type, "measure_type"),
        "form_field": existing.get("form_field"),
        "proc_def_id": existing.get("proc_def_id"),
        "direction": _pick(direction, "direction"),
        "baseline_value": _pick(baseline_value, "baseline_value"),
        "target_value": _pick(target_value, "target_value"),
        "period_start": existing.get("period_start"),
        "period_end": existing.get("period_end"),
        "survey_questions": existing.get("survey_questions") or [],
        "source_url": existing.get("source_url"),
        "source_field": existing.get("source_field"),
        "kpi_role": _pick(kpi_role, "kpi_role"),
    }
    return await _request(
        "PUT", f"/api/kpis/{kpi_id}", params={"tenant_id": tenant_id}, json_body=body
    )


# ---------------------------------------------------------------------------
# 6. link_kpi_to_process
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def link_kpi_to_process(
    kpi_id: str,
    proc_def_id: str,
    tenant_id: str,
) -> dict:
    """기존 KPI 를 프로세스 정의와 연결한다(측정 자동화용).

    전략 서비스에는 KPI 단건 조회 엔드포인트가 없고 PUT /api/kpis/{id} 는
    KpiIn 전체를 재전송하는 전체 교체(overwrite) 방식이다. 따라서 먼저
    GET /api/map 으로 전략맵 전체를 가져와 대상 KPI의 기존 필드값을 찾아낸 뒤,
    proc_def_id 만 바꿔 KpiIn 전체를 다시 PUT 한다.

    Args:
        kpi_id: 연결할 KPI id.
        proc_def_id: 연결할 프로세스 정의 id.
        tenant_id: 테넌트 ID.

    Returns:
        갱신된 kpi 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not kpi_id or not proc_def_id:
        return _err("kpi_id와 proc_def_id는 필수입니다.")

    map_result = await _request("GET", "/api/map", params={"tenant_id": tenant_id})
    if isinstance(map_result, dict) and map_result.get("error"):
        return map_result

    existing: Optional[dict] = None
    for objective in map_result.get("objectives") or []:
        for kpi in objective.get("kpis") or []:
            if kpi.get("id") == kpi_id:
                existing = kpi
                break
        if existing is not None:
            break

    if existing is None:
        return _err(f"kpi_id '{kpi_id}'를 전략맵에서 찾을 수 없습니다.")

    body = {
        "objective_id": existing.get("objective_id"),
        "name": existing.get("name"),
        "description": existing.get("description"),
        "unit": existing.get("unit"),
        "measure_type": existing.get("measure_type"),
        "form_field": existing.get("form_field"),
        "proc_def_id": proc_def_id,
        "direction": existing.get("direction"),
        "baseline_value": existing.get("baseline_value"),
        "target_value": existing.get("target_value"),
        "period_start": existing.get("period_start"),
        "period_end": existing.get("period_end"),
        "survey_questions": existing.get("survey_questions") or [],
        "source_url": existing.get("source_url"),
        "source_field": existing.get("source_field"),
        "kpi_role": existing.get("kpi_role"),
    }
    return await _request(
        "PUT", f"/api/kpis/{kpi_id}", params={"tenant_id": tenant_id}, json_body=body
    )


# ---------------------------------------------------------------------------
# 7. check_strategy_alignment
# ---------------------------------------------------------------------------
@tool(parse_docstring=True)
async def check_strategy_alignment(
    description: str,
    tenant_id: str,
    proc_def_id: Optional[str] = None,
) -> dict:
    """프로세스 정의를 확정하기 전에 전략목표·KPI 정합성을 반드시 확인한다.

    후보는 제안일 뿐 자동 연결하면 안 된다. 후보가 있으면 사용자에게 선택을 받고,
    없거나 조회 오류이면 그 사실과 계속 진행할지를 사용자에게 확인해야 한다.

    Args:
        description: 프로세스 초안 또는 개선 내용의 구체적인 자연어 설명.
        tenant_id: 테넌트 ID.
        proc_def_id: 기존 프로세스 개선인 경우 프로세스 정의 ID. 신규이면 None.

    Returns:
        관련 후보와 기존 연결 또는 {"error": ...}.
    """
    if not tenant_id:
        return _err("tenant_id가 비어 있습니다.")
    if not description.strip():
        return _err("description이 비어 있습니다.")
    return await _request(
        "POST", "/api/ai/alignment", params={"tenant_id": tenant_id},
        json_body={"description": description, "proc_def_id": proc_def_id},
    )


# ---------------------------------------------------------------------------
# 도구 목록 export
# ---------------------------------------------------------------------------
def get_strategy_save_tools() -> list:
    """bsc-strategy-interview 스킬이 확정한 전략을 저장하는 도구(그 스킬의 include_tools 와 같다)."""
    return [save_strategy_objective, save_strategy_kpi, save_canvas_block, link_kpi_to_process]


def get_strategy_tools() -> list:
    return [
        get_strategy_map,
        get_strategy_canvas,
        save_strategy_objective,
        update_strategy_objective,
        save_strategy_kpi,
        update_strategy_kpi,
        save_canvas_block,
        link_kpi_to_process,
        check_strategy_alignment,
    ]
