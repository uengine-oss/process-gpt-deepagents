"""스킬 도구 카탈로그 · 스킬 연결 현황 API (GET /skill-tools/catalog, /skill-tools/bindings).

관리자가 도구를 스킬 전용으로 지정하고, 스킬 편집 화면이 SKILL.md `metadata.include_tools` 에
적을 도구를 고를 때 쓰는 정보다. 카탈로그의 이름이 실제 채팅 에이전트의 도구 이름과 다르면
지정해도 숨겨지지 않으므로, 에이전트 조립 결과와 같은지를 직접 비교한다. MCP 는 테스트용 stdio
서버를 실제로 띄우고, Supabase 는 메모리 대역으로 바꾼다(openspec: bind-skill-tools).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import core.llm.mcp as mcp_mod
import core.skills.skills as skills_mod
from core.api.skill_tools_router import routes
from core.skills import tool_catalog
from tests.fixtures.fake_supabase import FakeSupabase

SERVER = str(Path(__file__).parent / "fixtures" / "skill_bound_mcp_server.py")


def _server(kind: str) -> dict:
    return {"command": sys.executable, "args": [SERVER, kind]}


TENANT_MCP = {"mcpServers": {"orders": _server("orders"), "dup": _server("dup")}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    sb = FakeSupabase()
    sb.tables["users"] = [
        {"id": "00000000-0000-0000-0000-0000000000aa", "tenant_id": "t1", "is_admin": True},
        {"id": "agent-1", "tenant_id": "t1", "is_agent": True, "username": "주문 담당", "tools": "orders"},
    ]
    sb.tables["proc_def"] = [{
        "id": "order_proc", "name": "주문 처리", "tenant_id": "t1", "isdeleted": False,
        "definition": {"activities": [{"id": "check", "name": "주문 확인", "tools": "orders,other"}]},
    }]
    monkeypatch.setattr("core.storage.db.get_supabase", lambda: sb)
    mcp_calls: list[str] = []

    def _fetch(tenant_id):
        mcp_calls.append(tenant_id)
        return env_state["mcp"]

    env_state = {"mcp": TENANT_MCP, "sb": sb, "mcp_calls": mcp_calls, "root": tmp_path}
    monkeypatch.setattr(mcp_mod, "fetch_tenant_mcp", _fetch)
    monkeypatch.setattr(skills_mod, "_get_skills", lambda *a, **k: [str(tmp_path / "skills")])
    tool_catalog._catalog_cache.clear()
    tool_catalog._skill_only_cache.clear()
    yield env_state
    tool_catalog._catalog_cache.clear()
    tool_catalog._skill_only_cache.clear()


@pytest.fixture
def client():
    return TestClient(Starlette(routes=routes), raise_server_exceptions=True)


def _skill(root: Path, name: str, include_tools: str) -> None:
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\nmetadata:\n  include_tools: {include_tools}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _by_name(body: dict) -> dict:
    return {t["name"]: t for t in body["tools"]}


def test_mcp_tools_come_grouped_by_server(env, client):
    body = client.get("/skill-tools/catalog?tenant_id=t1").json()

    tools = _by_name(body)
    assert tools["lookup_order"]["source"] == "mcp"
    assert tools["lookup_order"]["server"] == "orders"
    assert tools["lookup_order"]["description"]
    assert tools["lookup_order"]["schema_tokens"] > 0
    assert tools["lookup_order"]["category"] == "bindable"
    assert body["generated_at"]


def test_builtin_tools_are_unbindable_and_skill_only_follows_the_designation(env, client):
    env["sb"].tables["configuration"] = [
        {"key": "skill_only_tools", "tenant_id": "t1", "value": {"tools": ["lookup_order", "gone_tool"]}},
    ]
    body = client.get("/skill-tools/catalog?tenant_id=t1").json()
    tools = _by_name(body)

    assert tools["read_file"]["category"] == "unbindable"
    assert tools["execute_process"]["category"] == "bindable"
    assert tools["lookup_order"]["skill_only"] is True
    assert tools["cancel_order"]["skill_only"] is False
    assert body["missing_skill_only_tools"] == ["gone_tool"]


def test_names_match_the_agent_the_runtime_builds(env, client):
    """제외되는 MCP 도구(get_current_user)는 서비스 도구로, 겹치는 이름(execute_process)은 실행되는 쪽으로 나온다."""
    tools = _by_name(client.get("/skill-tools/catalog?tenant_id=t1").json())

    assert tools["get_current_user"]["source"] == "service"
    assert tools["execute_process"]["source"] == "service"
    assert [t for t in tools if t == "execute_process"] == ["execute_process"]


async def test_catalog_names_equal_the_tools_bound_to_the_real_agent(env, tmp_path, monkeypatch):
    """카탈로그(기본 도구 제외)와 실제 build_agent 가 모델에 넘기는 도구 이름이 같아야 한다."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from deepagents.backends.filesystem import FilesystemBackend

    import core.agents.agent as agent_mod

    class _Model(BaseChatModel):
        names: list = []

        @property
        def _llm_type(self):
            return "record"

        def bind_tools(self, tools, **kwargs):
            self.names = [getattr(t, "name", None) or t.get("name") for t in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    model = _Model()
    (tmp_path / "skills").mkdir(exist_ok=True)
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda *a, **k: FilesystemBackend(root_dir=tmp_path, virtual_mode=True))
    monkeypatch.setattr(agent_mod, "_build_model", lambda: model)
    monkeypatch.setattr(agent_mod, "_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(agent_mod, "_get_skills", lambda *a, **k: [str(tmp_path / "skills")])

    def _no_git(_t):
        raise RuntimeError("no git")

    monkeypatch.setattr(agent_mod, "_get_git_provider", _no_git)

    catalog = await tool_catalog.build_catalog("t1")
    mcp_tools, _servers = await mcp_mod.load_chat_root_tools(TENANT_MCP)
    graph, _, _ = await agent_mod.build_agent(
        tenant_id="t1", extra_tools=mcp_tools, tenant_mcp=TENANT_MCP,
        checkpointer=InMemorySaver(), is_chat=True,
    )
    await graph.ainvoke({"messages": [HumanMessage("hi")]}, config={"configurable": {"thread_id": "t"}})

    runtime = set(model.names)
    builtins = runtime - {t["name"] for t in catalog["tools"] if t["category"] != "unbindable"}
    assert builtins <= tool_catalog.UNBINDABLE_TOOLS, f"카탈로그가 모르는 기본 도구: {builtins - tool_catalog.UNBINDABLE_TOOLS}"
    assert {t["name"] for t in catalog["tools"] if t["category"] != "unbindable"} == runtime - builtins


def test_unreachable_server_is_reported_and_others_still_listed(env, client):
    env["mcp"] = {"mcpServers": {**TENANT_MCP["mcpServers"], "broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(3)"]}}}

    body = client.get("/skill-tools/catalog?tenant_id=t1").json()

    assert "lookup_order" in _by_name(body)
    assert [u["server"] for u in body["unavailable_servers"]] == ["broken"]
    assert body["unavailable_servers"][0]["reason"]


def test_other_tenant_is_rejected_without_connecting_its_mcp(env, client):
    response = client.get("/skill-tools/catalog?tenant_id=someone-else")

    assert response.status_code == 403
    assert env["mcp_calls"] == []


def test_changing_mcp_config_invalidates_the_cache(env, client):
    first = _by_name(client.get("/skill-tools/catalog?tenant_id=t1").json())
    assert "ping_other" not in first

    env["mcp"] = {"mcpServers": {**TENANT_MCP["mcpServers"], "other": _server("other")}}
    second = _by_name(client.get("/skill-tools/catalog?tenant_id=t1").json())

    assert second["ping_other"]["server"] == "other"


def test_bindings_list_skills_direct_assignments_and_unknown_names(env, client):
    env["sb"].tables["configuration"] = [
        {"key": "skill_only_tools", "tenant_id": "t1", "value": {"tools": ["lookup_order", "cancel_order"]}},
    ]
    _skill(env["root"], "order-lookup", "lookup_order")
    _skill(env["root"], "order-audit", "lookup_order no_such_tool execute_process")

    body = client.get("/skill-tools/bindings?tenant_id=t1").json()

    assert body["bindings"]["execute_process"]["skill_only"] is False, "스킬 전용이 아니면 적혀 있어도 처음부터 보인다"
    assert body["skill_only_without_skill"] == ["cancel_order"], "여는 스킬이 없는 스킬 전용 도구를 알려야 한다"
    lookup = body["bindings"]["lookup_order"]
    assert lookup["skill_only"] is True
    assert sorted(lookup["skills"]) == ["order-audit", "order-lookup"]
    assert lookup["server"] == "orders"
    kinds = sorted(a["kind"] for a in lookup["direct_assignments"])
    assert kinds == ["activity", "agent"]
    assert body["unknown_tools"] == [{"tool": "no_such_tool", "skill": "order-audit"}]
