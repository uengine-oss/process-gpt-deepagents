"""core/skills/regression_client.py — 회귀 검증 실행을 completion 에 위임한다.

## 왜 여기서 돌리지 않는가

프로세스 라우팅의 진실 원천은 실행 엔진(폴링 서비스)이고, DMN 규칙 표와 프로세스 정의도
completion 의 데이터다. 같은 판정 규칙의 사본을 이 서비스에 두면 엔진이 합류·분기 규칙을
고칠 때 **회귀 테스트만 옛 규칙으로 통과**시킨다 — 회귀 테스트로서 최악이다.

그래서 이 모듈은 "무엇을 검증할지"(시나리오)와 "결과를 어떻게 기록할지"(회차)만 들고,
"이 정의에서 이 시나리오가 어떤 결과인가" 는 묻기만 한다.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 실행 엔진(completion) 베이스 URL. 배포 환경에서는 서비스 주소로 주입한다.
_COMPLETION_URL = os.getenv("COMPLETION_SERVICE_URL", "http://localhost:8000").rstrip("/")

# 재생은 계산이라 금방 끝나지만, 정의가 크면 직렬화·조회에 시간이 걸린다.
_TIMEOUT = float(os.getenv("REGRESSION_RUN_TIMEOUT", "120"))


class RegressionUnavailable(Exception):
    """completion 에 닿지 못했다. 회차를 실패로 남기고 사유를 그대로 보여 준다."""


async def _post(path: str, payload: dict, tenant_id: str) -> dict:
    import httpx

    url = f"{_COMPLETION_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                url,
                json={"input": payload},
                # completion 은 테넌트를 X-Forwarded-Host 의 첫 라벨로 가린다(main.py
                # DBConfigMiddleware). 게이트웨이를 거치지 않는 서비스 간 호출에는 이 헤더가
                # 없어 테넌트가 'localhost' 로 떨어지고, 다른 테넌트의 정의를 못 찾는다.
                headers={
                    "X-Forwarded-Host": tenant_id,
                    "subdomain": tenant_id,
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        raise RegressionUnavailable(f"실행 엔진({url}) 에 닿지 못했습니다: {e}") from e

    if response.status_code >= 400:
        raise RegressionUnavailable(
            f"실행 엔진이 {response.status_code} 를 돌려줬습니다: {response.text[:200]}"
        )
    return response.json() if response.content else {}


async def run(
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    base_ref: str,
    head_ref: str,
    cases: list[dict],
    skip_base: set[str] | None = None,
) -> dict:
    """시나리오를 두 버전에 재생해 케이스별 결과를 받아 온다.

    Returns:
        {"results": [...], "undecided": [...], "base_digest", "head_digest",
         "mode", "llm_calls", "engine_instances"} 또는 {"error", "message"}.
    """
    payload = {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "base_ref": base_ref or "",
        "head_ref": head_ref or "",
        # 채점에 필요한 것만 보낸다 — 회차 id 나 스위트 정보는 실행기가 알 필요가 없다.
        "cases": [
            {
                "eval_name": c.get("eval_name"),
                "prompt": c.get("prompt"),
                "assertions": c.get("assertions") or [],
                "checks": c.get("checks") or [],
            }
            for c in cases
        ],
        "skip_base": sorted(skip_base or set()),
    }
    return await _post("/regression-run", payload, tenant_id)


async def derive_dmn_cases(*, tenant_id: str, resource_id: str, base_ref: str) -> dict:
    """의사결정 규칙 표에서 시나리오를 파생해 받아 온다(모델 호출 없음)."""
    return await _post(
        "/regression-scenarios",
        {"resource_type": "dmn", "resource_id": resource_id, "base_ref": base_ref or ""},
        tenant_id,
    )


async def derive_process_cases(*, tenant_id: str, resource_id: str, base_ref: str) -> dict:
    """프로세스 정의의 갈림길 조합에서 시나리오를 파생해 받아 온다(모델·엔진 호출 없음).

    파생 규칙은 재생 규칙과 같은 지식이라 실행기 곁에 둔다 — 여기에 사본을 두면 엔진이
    분기 해석을 고칠 때 시나리오만 옛 규칙으로 만들어진다.
    """
    return await _post(
        "/regression-scenarios",
        {"resource_type": "bpmn", "resource_id": resource_id, "base_ref": base_ref or ""},
        tenant_id,
    )
