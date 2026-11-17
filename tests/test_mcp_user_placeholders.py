"""테넌트 MCP 설정의 {{user_jwt}}·{{user_email}} 자리표시자를 요청 사용자 값으로 바꾼다(core.llm.mcp.resolve_user_placeholders)."""
from core.llm.mcp import resolve_user_placeholders

CFG = {
    "mcpServers": {
        "plain": {"url": "http://a/mcp", "headers": {"X-Key": "static"}},
        "lite": {"url": "http://lite/mcp/office", "headers": {"Authorization": "Bearer {{user_jwt}}"}},
        "mail": {"url": "http://b/mcp", "headers": {"X-User": "{{user_email}}"}},
        "stdio": {"command": "x", "args": [], "env": {"TOKEN": "{{user_jwt}}"}},
    },
    "other": 1,
}


def test_substitutes_for_the_requesting_user():
    out = resolve_user_placeholders(CFG, "jwt-123", "a@b.c")
    s = out["mcpServers"]
    assert s["lite"]["headers"]["Authorization"] == "Bearer jwt-123"
    assert s["mail"]["headers"]["X-User"] == "a@b.c"
    assert s["stdio"]["env"]["TOKEN"] == "jwt-123"
    assert out["other"] == 1


def test_servers_without_placeholders_are_unchanged():
    out = resolve_user_placeholders(CFG, "jwt-123", "a@b.c")
    assert out["mcpServers"]["plain"] is CFG["mcpServers"]["plain"]


def test_original_config_is_not_mutated():
    resolve_user_placeholders(CFG, "jwt-123", "a@b.c")
    assert CFG["mcpServers"]["lite"]["headers"]["Authorization"] == "Bearer {{user_jwt}}"


def test_server_needing_a_missing_value_is_left_out():
    out = resolve_user_placeholders(CFG, None, "a@b.c")
    s = out["mcpServers"]
    assert "lite" not in s and "stdio" not in s
    assert s["mail"]["headers"]["X-User"] == "a@b.c"
    assert "plain" in s


def test_no_servers():
    assert resolve_user_placeholders({}, "j", "e") == {}
    assert resolve_user_placeholders({"mcpServers": None}, "j", "e") == {"mcpServers": None}
