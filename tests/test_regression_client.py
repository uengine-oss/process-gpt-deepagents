"""regression_client 가 실행 엔진에 테넌트를 제대로 실어 보내는지.

completion 은 테넌트를 `X-Forwarded-Host` 로 가린다. 게이트웨이를 거치지 않는 서비스 간
호출에서 이 헤더를 빠뜨리면 테넌트가 'localhost' 로 떨어져, 로컬에서는 멀쩡하고 배포
테넌트에서만 "정의를 찾을 수 없습니다" 로 깨진다.
"""

from __future__ import annotations

import asyncio

import httpx

from core.skills import regression_client


class _FakeResponse:
    status_code = 200
    content = b"{}"
    text = "{}"

    def json(self):
        return {"cases": []}


def _capture(monkeypatch) -> dict:
    sent: dict = {}

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update(url=url, json=json, headers=headers)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return sent


def test_tenant_is_sent_as_forwarded_host(monkeypatch):
    sent = _capture(monkeypatch)

    asyncio.run(regression_client.derive_process_cases(
        tenant_id="hoseo-osy", resource_id="order_process", base_ref="v1",
    ))

    assert sent["headers"]["X-Forwarded-Host"] == "hoseo-osy"
    assert sent["url"].endswith("/regression-scenarios")
    assert sent["json"]["input"]["resource_id"] == "order_process"


def test_unreachable_engine_names_the_url(monkeypatch):
    class _DownClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(httpx, "AsyncClient", _DownClient)

    try:
        asyncio.run(regression_client.derive_process_cases(
            tenant_id="hoseo-osy", resource_id="order_process", base_ref="",
        ))
    except regression_client.RegressionUnavailable as e:
        assert "/regression-scenarios" in str(e)
    else:
        raise AssertionError("엔진에 닿지 못하면 RegressionUnavailable 이어야 한다")
