"""요청마다 숨기는 MCP 도구(metadata.hidden_tools).

2026-11-27 Process GPT Lite 운영: 채팅에서 「이렇게 보내」라고 하자 에이전트가 테넌트 MCP 의 메일 도구(send_email)로
고객에게 답장을 직접 보냈다 — 회사 공용 발송 계정에서, 받은 메일의 스레드가 아닌 새 메일로. Lite 는 이제 사람의 턴에서
그 도구를 이름으로 숨기라고 하고(metadata.hidden_tools), 사람의 메일은 그 사람이 연결한 메일함에서 「보낼 메일」 카드의
보내기로만 나간다. 채팅 루트 · 업무 지정 · 프로필 · 서브에이전트의 MCP 도구가 모두 `_load_mcp_tools_grouped` 를 거치므로
거기서 한 번 거른다. 요청이 주지 않으면 아무것도 숨기지 않는다(본판 채팅 · 업무 실행은 그대로).
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest

import core.llm.mcp as mcp_mod
from core.llm.mcp import _load_mcp_tools_grouped, hidden_tool_names, hide_tools, reset_hidden_tools, set_hidden_tools


class _Tool:
    def __init__(self, name: str):
        self.name = name


class _FakeClient:
    """langchain_mcp_adapters 의 MultiServerMCPClient 대신: 서버마다 정해 둔 도구를 돌려준다."""

    servers = {
        "company-mail": ["send_email", "search_email"],
        "pgpt-mail": ["send_mail_as_me"],
    }

    def __init__(self, connections):
        self.connections = connections

    async def get_tools(self, server_name: str):
        return [_Tool(n) for n in self.servers[server_name]]


@pytest.fixture
def fake_client(monkeypatch):
    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(client_mod, "MultiServerMCPClient", _FakeClient)
    return _FakeClient


CFG = {"mcpServers": {"company-mail": {"url": "http://mail/mcp"}, "pgpt-mail": {"url": "http://lite/mcp/mail"}}}


def _names(grouped: dict) -> dict[str, list[str]]:
    return {srv: [t.name for t in tools] for srv, tools in grouped.items()}


def test_hidden_tool_names_reads_only_a_list_of_names():
    assert hidden_tool_names({"hidden_tools": ["send_email", " ", 3, "x"]}) == frozenset({"send_email", "x"})
    assert hidden_tool_names({"hidden_tools": "send_email"}) == frozenset()
    assert hidden_tool_names({}) == frozenset()
    assert hidden_tool_names(None) == frozenset()


async def test_a_request_that_hides_send_email_never_gets_it(fake_client):
    token = set_hidden_tools(hidden_tool_names({"hidden_tools": ["send_email"]}))
    try:
        grouped = await _load_mcp_tools_grouped(["company-mail", "pgpt-mail"], CFG)
    finally:
        reset_hidden_tools(token)
    assert _names(grouped) == {"company-mail": ["search_email"], "pgpt-mail": ["send_mail_as_me"]}


async def test_a_request_that_hides_nothing_keeps_every_tool(fake_client):
    token = set_hidden_tools(frozenset())
    try:
        grouped = await _load_mcp_tools_grouped(["company-mail"], CFG)
    finally:
        reset_hidden_tools(token)
    assert _names(grouped) == {"company-mail": ["send_email", "search_email"]}


async def test_hiding_is_per_request_not_global(fake_client):
    """한 요청이 숨긴 것이 같은 프로세스의 다른 요청(본판 채팅)에 번지지 않는다."""

    async def lite():
        set_hidden_tools(frozenset({"send_email"}))
        await asyncio.sleep(0)
        return _names(await _load_mcp_tools_grouped(["company-mail"], CFG))

    async def main_app():
        await asyncio.sleep(0)
        return _names(await _load_mcp_tools_grouped(["company-mail"], CFG))

    ours, theirs = await asyncio.gather(asyncio.create_task(lite(), context=contextvars.copy_context()),
                                        asyncio.create_task(main_app(), context=contextvars.copy_context()))
    assert ours == {"company-mail": ["search_email"]}
    assert theirs == {"company-mail": ["send_email", "search_email"]}


async def test_a_server_whose_only_tool_is_hidden_drops_out(fake_client):
    token = set_hidden_tools(frozenset({"send_mail_as_me"}))
    try:
        grouped = await _load_mcp_tools_grouped(["pgpt-mail"], CFG)
    finally:
        reset_hidden_tools(token)
    assert grouped == {}


def test_hide_tools_on_a_plain_list():
    tools = [_Tool("send_email"), _Tool("gmail_read")]
    assert [t.name for t in hide_tools(tools, frozenset({"send_email"}))] == ["gmail_read"]
    assert hide_tools(tools, frozenset()) is tools


def test_the_executor_sets_the_requests_hidden_tools_before_any_mcp_load():
    """executor 는 metadata.hidden_tools 를 MCP 설정을 읽기 전에 정한다(그 뒤 모든 로드가 거른다)."""
    src = open(mcp_mod.__file__.replace("core/llm/mcp.py", "executor.py"), encoding="utf-8").read()
    set_at = src.index("set_hidden_tools(_hidden_tools)")
    assert set_at < src.index("fetch_tenant_mcp, tenant_id) if tenant_id else {}")
    assert set_at < src.index("await load_chat_root_tools(tenant_mcp)")
    assert set_at < src.index("graph, _, _sub_mcp_tools_meta = await build_agent(")
