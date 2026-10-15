"""스킬 전용 도구 지정 API (GET/PUT /skill-tools/skill-only) — 테넌트 관리자만 지정을 바꾼다.

지정은 `configuration` 키 `skill_only_tools` 에 테넌트마다 둔다. 관리자가 아닌 사용자의 변경 요청은
서버에서 거부되고 저장소에 아무것도 남지 않아야 한다. 저장한 지정은 다음 에이전트 조립부터
쓰인다(openspec: bind-skill-tools). Supabase 는 메모리 대역을 쓴다.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from core.api.skill_tools_router import routes
from core.skills import tool_catalog
from tests.fixtures.fake_supabase import FakeSupabase

USER = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def sb(monkeypatch):
    fake = FakeSupabase()
    # 테스트 인증은 같은 사용자 id 로 들어온다 — t1 에서는 관리자, tenant1 에서는 일반 사용자.
    fake.tables["users"] = [
        {"id": USER, "tenant_id": "t1", "is_admin": True},
        {"id": USER, "tenant_id": "tenant1", "is_admin": False, "role": "editor"},
    ]
    fake.tables["configuration"] = []
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: fake)
    tool_catalog._skill_only_cache.clear()
    yield fake
    tool_catalog._skill_only_cache.clear()


@pytest.fixture
def client():
    return TestClient(Starlette(routes=routes), raise_server_exceptions=True)


def test_tenant_without_designation_has_no_skill_only_tools(sb, client):
    assert client.get("/skill-tools/skill-only?tenant_id=t1").json() == {"tools": []}
    assert tool_catalog.load_skill_only_tools("t1") == frozenset()


def test_admin_designates_tools_and_runtime_reads_them_immediately(sb, client):
    tool_catalog.load_skill_only_tools("t1")  # 캐시가 있어도 저장 뒤에는 새 값이어야 한다

    resp = client.put("/skill-tools/skill-only?tenant_id=t1", json={"tools": ["send_email_tool", "execute_process", "send_email_tool"]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tools": ["execute_process", "send_email_tool"]}
    assert client.get("/skill-tools/skill-only?tenant_id=t1").json()["tools"] == ["execute_process", "send_email_tool"]
    assert tool_catalog.load_skill_only_tools("t1") == {"execute_process", "send_email_tool"}


def test_non_admin_designation_is_rejected_by_the_server(sb, client):
    resp = client.put("/skill-tools/skill-only?tenant_id=tenant1", json={"tools": ["send_email_tool"]})

    assert resp.status_code == 403
    assert sb.tables["configuration"] == []
    assert tool_catalog.load_skill_only_tools("tenant1") == frozenset()


def test_admin_role_without_is_admin_flag_is_admin(sb, client):
    sb.tables["users"][1]["role"] = "admin"

    resp = client.put("/skill-tools/skill-only?tenant_id=tenant1", json={"tools": ["send_email_tool"]})

    assert resp.status_code == 200


def test_designation_is_per_tenant(sb, client):
    sb.tables["configuration"].append({"key": "skill_only_tools", "tenant_id": "tenant1", "value": {"tools": ["x"]}})

    assert tool_catalog.load_skill_only_tools("t1") == frozenset()
    assert tool_catalog.load_skill_only_tools("tenant1") == {"x"}


@pytest.mark.parametrize("body", [{"tools": "send_email_tool"}, {"tools": ["read_file"]}, ["send_email_tool"]])
def test_invalid_designation_is_rejected(sb, client, body):
    resp = client.put("/skill-tools/skill-only?tenant_id=t1", json=body)

    assert resp.status_code == 400
    assert sb.tables["configuration"] == []


# ── PATCH: 바꾼 도구만 보낸다(openspec skill-tool-binding-ui · skill-tool-catalog-api) ──────────

def _designated(sb, tenant="t1"):
    rows = [r for r in sb.tables["configuration"] if r["key"] == "skill_only_tools" and r["tenant_id"] == tenant]
    return rows[0]["value"]["tools"] if rows else []


def test_patch_from_two_screens_keeps_both_additions(sb, client):
    """화면 둘이 같은 지정 [A] 를 읽고 각자 하나씩 더해도 서로 지우지 않는다."""
    sb.tables["configuration"].append({"key": "skill_only_tools", "tenant_id": "t1", "value": {"tools": ["A"]}})

    first = client.patch("/skill-tools/skill-only?tenant_id=t1", json={"add": ["B"]})
    second = client.patch("/skill-tools/skill-only?tenant_id=t1", json={"add": ["C"]})

    assert first.status_code == 200 and second.status_code == 200, second.text
    assert second.json() == {"tools": ["A", "B", "C"]}
    assert _designated(sb) == ["A", "B", "C"]


def test_patch_removes_only_named_tools(sb, client):
    sb.tables["configuration"].append({"key": "skill_only_tools", "tenant_id": "t1", "value": {"tools": ["A", "B", "C"]}})

    resp = client.patch("/skill-tools/skill-only?tenant_id=t1", json={"remove": ["B", "not-there"], "add": ["D"]})

    assert resp.json() == {"tools": ["A", "C", "D"]}
    assert _designated(sb) == ["A", "C", "D"]


def test_patch_by_non_admin_is_rejected_and_changes_nothing(sb, client):
    sb.tables["configuration"].append({"key": "skill_only_tools", "tenant_id": "tenant1", "value": {"tools": ["A"]}})

    resp = client.patch("/skill-tools/skill-only?tenant_id=tenant1", json={"remove": ["A"]})

    assert resp.status_code == 403
    assert _designated(sb, "tenant1") == ["A"]


@pytest.mark.parametrize("body", [{"add": "B"}, {"remove": [1, {}]}, {"add": ["read_file"]}, {}, ["B"]])
def test_invalid_patch_is_rejected(sb, client, body):
    resp = client.patch("/skill-tools/skill-only?tenant_id=t1", json=body)

    assert resp.status_code == 400
    assert sb.tables["configuration"] == []


def test_catalog_reflects_a_patch_immediately(sb, client, monkeypatch):
    """카탈로그는 MCP 결과를 캐시해도 지정 여부는 매번 새로 계산한다."""
    import asyncio

    async def _raw(_mcp):
        return {"tools": [{"name": "send_email_tool", "source": "mcp", "server": "gmail-mcp",
                           "description": "", "schema_tokens": 1}], "unavailable_servers": []}

    monkeypatch.setattr(tool_catalog, "_build_raw_catalog", _raw)
    tool_catalog._catalog_cache.clear()
    before = asyncio.run(tool_catalog.build_catalog("t1", tenant_mcp={}))
    client.patch("/skill-tools/skill-only?tenant_id=t1", json={"add": ["send_email_tool"]})
    after = asyncio.run(tool_catalog.build_catalog("t1", tenant_mcp={}))
    tool_catalog._catalog_cache.clear()

    assert before["tools"][0]["skill_only"] is False
    assert after["tools"][0]["skill_only"] is True
