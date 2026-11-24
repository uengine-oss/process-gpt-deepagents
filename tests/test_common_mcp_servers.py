"""공통 MCP 서버(COMMON_MCP_SERVERS · COMMON_MCP_SERVERS_FILE) — 테넌트마다 tenants.mcp 에 등록하지 않아도
모든 테넌트의 에이전트에 붙는 서버(Process GPT Lite 의 폴더 · 패널 앱 · ArtCraft · 이음오피스 도구 등).

core.llm.mcp.fetch_tenant_mcp 가 테넌트 설정에 합치므로 채팅 실행과 스킬 도구 카탈로그가 같이 본다.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import core.llm.mcp as mcp_mod
from core.api.mcp_router import routes as mcp_routes
from core.api.skill_tools_router import routes as skill_tools_routes
from core.llm.mcp import (
    _load_mcp_tools_grouped,
    common_mcp_servers,
    fetch_tenant_mcp,
    is_mcp_server_enabled,
    resolve_user_placeholders,
)
from core.skills import tool_catalog

LITE = "http://process-gpt-lite-server.default.svc.cluster.local:8787/mcp"
SECRET = "s3cr3t-static-key"


def _lite(name: str) -> dict:
    return {"url": f"{LITE}/{name}", "type": "streamable-http", "enabled": True,
            "headers": {"Authorization": "Bearer {{user_jwt}}"}}


COMMON = {
    "pgpt-folders": _lite("folders"),
    "pgpt-photocraft": _lite("photocraft"),
    "static-tool": {"url": "http://static/mcp", "headers": {"X-Key": SECRET}},
}


class _Query:
    def __init__(self, rows: list[dict]):
        self.rows, self.filters, self.one = rows, [], False

    def select(self, *_a, **_k):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, _n):
        return self

    def single(self):
        self.one = True
        return self

    def execute(self):
        found = [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]
        data = (found[0] if found else None) if self.one else found
        return type("R", (), {"data": data})()


class _Supabase:
    """tenants 는 .single() 로 한 행(dict)을, 나머지는 목록을 돌려주는 실제 클라이언트 모양."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {"tenants": []}

    def table(self, name):
        return _Query(self.tables.setdefault(name, []))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ("COMMON_MCP_SERVERS", "COMMON_MCP_SERVERS_FILE",
                "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_KEY", "SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mcp_mod, "_common_env_cache", None)
    monkeypatch.setattr(mcp_mod, "_common_file_cache", None)
    tool_catalog._catalog_cache.clear()
    tool_catalog._skill_only_cache.clear()
    yield
    tool_catalog._catalog_cache.clear()
    tool_catalog._skill_only_cache.clear()


@pytest.fixture
def db(monkeypatch):
    sb = _Supabase()
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: sb)
    return sb


def _set_common(monkeypatch, value) -> None:
    monkeypatch.setenv("COMMON_MCP_SERVERS", value if isinstance(value, str) else json.dumps(value))


def _servers(cfg: dict) -> dict:
    return cfg.get("mcpServers") or {}


# ---------------------------------------------------------------------------
# 설정 읽기
# ---------------------------------------------------------------------------

def test_env_json_servers_reach_every_tenant_without_a_row(monkeypatch, db):
    _set_common(monkeypatch, COMMON)
    db.tables["tenants"] = [
        {"id": "t1", "mcp": {"mcpServers": {"own": {"url": "http://own/mcp"}}}},
        {"id": "t2", "mcp": None},
    ]

    t1, t2 = _servers(fetch_tenant_mcp("t1")), _servers(fetch_tenant_mcp("t2"))

    assert set(COMMON) <= set(t1) and "own" in t1
    assert set(COMMON) <= set(t2)
    assert t2["pgpt-folders"]["url"] == f"{LITE}/folders"


def test_wrapped_in_mcpServers_is_accepted(monkeypatch):
    _set_common(monkeypatch, {"mcpServers": COMMON})

    assert set(common_mcp_servers()) == set(COMMON)


def test_file_source_is_parsed_once_and_reread_when_it_changes(monkeypatch, tmp_path):
    path = tmp_path / "common-mcp.json"
    path.write_text(json.dumps(COMMON), encoding="utf-8")
    monkeypatch.setenv("COMMON_MCP_SERVERS_FILE", str(path))

    first = common_mcp_servers()
    assert set(first) == set(COMMON)
    assert common_mcp_servers() is first  # 파일이 그대로면 다시 읽지 않는다

    path.write_text(json.dumps({"pgpt-improve": _lite("improve")}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    assert set(common_mcp_servers()) == {"pgpt-improve"}


def test_env_and_file_combine_and_env_wins_on_the_same_name(monkeypatch, tmp_path):
    path = tmp_path / "common-mcp.json"
    path.write_text(json.dumps({"pgpt-folders": _lite("folders"), "pgpt-improve": _lite("improve")}), encoding="utf-8")
    monkeypatch.setenv("COMMON_MCP_SERVERS_FILE", str(path))
    _set_common(monkeypatch, {"pgpt-folders": {"url": "http://override/mcp"}})

    servers = common_mcp_servers()

    assert set(servers) == {"pgpt-folders", "pgpt-improve"}
    assert servers["pgpt-folders"]["url"] == "http://override/mcp"


@pytest.mark.parametrize("source", ["env", "file"])
def test_invalid_json_is_ignored_with_one_warning_that_hides_the_content(monkeypatch, tmp_path, caplog, db, source):
    broken = '{"pgpt-x": {"url": "http://x", "headers": {"Authorization": "Bearer ' + SECRET + '"}'
    if source == "env":
        _set_common(monkeypatch, broken)
    else:
        path = tmp_path / "common-mcp.json"
        path.write_text(broken, encoding="utf-8")
        monkeypatch.setenv("COMMON_MCP_SERVERS_FILE", str(path))
    db.tables["tenants"] = [{"id": "t1", "mcp": {"mcpServers": {"own": {"url": "http://own/mcp"}}}}]
    caplog.set_level(logging.WARNING, logger="core.llm.mcp")

    assert common_mcp_servers() == {}
    assert common_mcp_servers() == {}
    assert list(_servers(fetch_tenant_mcp("t1"))) == ["own"]

    warnings = [r for r in caplog.records if r.name == "core.llm.mcp" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SECRET not in caplog.text and "pgpt-x" not in caplog.text


def test_non_object_values_are_ignored(monkeypatch):
    _set_common(monkeypatch, '["pgpt-folders"]')
    assert common_mcp_servers() == {}

    monkeypatch.setattr(mcp_mod, "_common_env_cache", None)
    _set_common(monkeypatch, {"pgpt-folders": _lite("folders"), "bad": "http://x"})
    assert list(common_mcp_servers()) == ["pgpt-folders"]


def test_missing_file_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMON_MCP_SERVERS_FILE", str(tmp_path / "nope.json"))
    assert common_mcp_servers() == {}


# ---------------------------------------------------------------------------
# 테넌트 설정과 합치기
# ---------------------------------------------------------------------------

def test_tenant_entry_with_the_same_name_wins(monkeypatch, db):
    _set_common(monkeypatch, COMMON)
    own = {"url": "http://tenant-folders/mcp", "type": "sse"}
    db.tables["tenants"] = [{"id": "t1", "mcp": {"mcpServers": {"pgpt-folders": own}}}]

    folders = _servers(fetch_tenant_mcp("t1"))["pgpt-folders"]

    assert folders == own  # 공통 항목의 헤더가 섞여 들어가지 않는다


async def test_tenant_can_switch_a_common_server_off(monkeypatch, db):
    _set_common(monkeypatch, COMMON)
    db.tables["tenants"] = [{"id": "t1", "mcp": {"mcpServers": {"pgpt-photocraft": {"enabled": False}}}}]

    cfg = resolve_user_placeholders(fetch_tenant_mcp("t1"), "jwt-1", "u@x.io")
    photocraft = _servers(cfg)["pgpt-photocraft"]
    errors: dict[str, str] = {}
    grouped = await _load_mcp_tools_grouped(["pgpt-photocraft"], cfg, errors=errors)

    assert not is_mcp_server_enabled(photocraft)
    assert grouped == {} and errors == {}  # 끈 것은 부르지도, 실패로 보고하지도 않는다
    assert is_mcp_server_enabled(_servers(cfg)["pgpt-folders"])  # 다른 공통 서버는 그대로


def test_tenant_entry_without_a_connection_overlays_the_common_one(monkeypatch, db):
    _set_common(monkeypatch, {"pgpt-folders": {**_lite("folders"), "enabled": False}})
    db.tables["tenants"] = [{"id": "t1", "mcp": {"mcpServers": {"pgpt-folders": {"enabled": True}}}}]

    folders = _servers(fetch_tenant_mcp("t1"))["pgpt-folders"]

    assert folders["url"] == f"{LITE}/folders" and is_mcp_server_enabled(folders)


def test_cached_common_config_is_not_mutated(monkeypatch, db):
    _set_common(monkeypatch, COMMON)
    db.tables["tenants"] = [{"id": "t1", "mcp": {}}]

    cfg = fetch_tenant_mcp("t1")
    _servers(cfg)["pgpt-folders"]["headers"]["Authorization"] = "changed"
    _servers(cfg)["pgpt-photocraft"]["enabled"] = False
    resolve_user_placeholders(fetch_tenant_mcp("t1"), "jwt-1", "u@x.io")

    assert common_mcp_servers() == COMMON
    assert _servers(fetch_tenant_mcp("t1"))["pgpt-folders"]["headers"]["Authorization"] == "Bearer {{user_jwt}}"


def test_placeholders_in_common_servers_take_the_request_user(monkeypatch, db):
    """executor 와 같은 순서: fetch_tenant_mcp → resolve_user_placeholders."""
    _set_common(monkeypatch, COMMON)
    db.tables["tenants"] = [{"id": "t1", "mcp": {}}]

    servers = _servers(resolve_user_placeholders(fetch_tenant_mcp("t1"), "jwt-1", "u@x.io"))

    assert servers["pgpt-folders"]["headers"]["Authorization"] == "Bearer jwt-1"
    assert servers["pgpt-photocraft"]["headers"]["Authorization"] == "Bearer jwt-1"
    assert servers["static-tool"]["headers"]["X-Key"] == SECRET


def test_common_server_needing_a_jwt_is_skipped_when_the_request_has_none(monkeypatch, db, caplog):
    _set_common(monkeypatch, COMMON)
    db.tables["tenants"] = [{"id": "t1", "mcp": {}}]
    caplog.set_level(logging.INFO, logger="core.llm.mcp")

    servers = _servers(resolve_user_placeholders(fetch_tenant_mcp("t1"), None, "u@x.io"))

    assert "pgpt-folders" not in servers and "pgpt-photocraft" not in servers
    assert "static-tool" in servers
    assert "skip pgpt-folders: needs ['{{user_jwt}}'] but the request has none" in caplog.text


def test_process_gpt_mcp_is_still_injected(monkeypatch, db):
    _set_common(monkeypatch, COMMON)
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.local")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    db.tables["tenants"] = [{"id": "t1", "mcp": {"mcpServers": {"own": {"url": "http://own/mcp"}}}}]

    servers = _servers(fetch_tenant_mcp("t1"))

    assert "process-gpt-mcp" in servers
    assert {"own", *COMMON} <= set(servers)


def test_without_common_servers_the_tenant_config_is_unchanged(db):
    tenant = {"mcpServers": {"own": {"url": "http://own/mcp"}}, "other": 1}
    db.tables["tenants"] = [{"id": "t1", "mcp": tenant}]

    assert fetch_tenant_mcp("t1") == tenant


# ---------------------------------------------------------------------------
# 스킬 도구 카탈로그
# ---------------------------------------------------------------------------

STDIO_SERVER = str(Path(__file__).parent / "fixtures" / "skill_bound_mcp_server.py")


def test_tool_catalog_lists_common_server_tools(monkeypatch, db):
    """카탈로그도 fetch_tenant_mcp 를 거쳐 공통 서버를 보고, {{user_jwt}} 는 부른 사용자 토큰으로 바꾼다."""
    _set_common(monkeypatch, {
        "common-orders": {"command": sys.executable, "args": [STDIO_SERVER, "orders"]},
        "common-per-user": {"command": sys.executable, "args": [STDIO_SERVER, "other"], "env": {"USER_JWT": "{{user_jwt}}"}},
    })
    db.tables["tenants"] = [{"id": "t1", "mcp": {}}]
    client = TestClient(Starlette(routes=skill_tools_routes), raise_server_exceptions=True)

    # 토큰이 없으면 그 서버는 부르지 않고 뺀다(실패로도 보고하지 않는다).
    body = client.get("/skill-tools/catalog?tenant_id=t1").json()
    tools = {t["name"]: t for t in body["tools"]}
    assert tools["lookup_order"]["server"] == "common-orders"
    assert "ping_other" not in tools
    assert body["unavailable_servers"] == []

    # 토큰이 있는 호출은 앞의 캐시를 쓰지 않고 그 서버까지 본다.
    body = client.get("/skill-tools/catalog?tenant_id=t1", headers={"Authorization": "Bearer jwt-1"}).json()
    tools = {t["name"]: t for t in body["tools"]}
    assert tools["lookup_order"]["server"] == "common-orders"
    assert tools["ping_other"]["server"] == "common-per-user"


# ---------------------------------------------------------------------------
# GET /mcp/common
# ---------------------------------------------------------------------------

def test_mcp_common_lists_enabled_names_only(monkeypatch):
    _set_common(monkeypatch, {**COMMON, "pgpt-off": {**_lite("off"), "enabled": False}})
    client = TestClient(Starlette(routes=mcp_routes))

    res = client.get("/mcp/common")

    assert res.status_code == 200
    assert res.json() == {"servers": ["pgpt-folders", "pgpt-photocraft", "static-tool"]}
    assert "http" not in res.text and SECRET not in res.text and "user_jwt" not in res.text


def test_mcp_common_is_empty_without_configuration():
    client = TestClient(Starlette(routes=mcp_routes))

    assert client.get("/mcp/common").json() == {"servers": []}
