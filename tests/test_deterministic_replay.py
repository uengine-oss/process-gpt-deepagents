from __future__ import annotations

import json

import pytest

from core.deterministic.replay import (
    _enabled_servers,
    _parameter_values,
    _run_code,
)


def test_extracts_business_parameters_from_korean_workitem() -> None:
    spec = {
        "parameters": [
            {"name": "product_name", "type": "string", "example": "iPhone"},
            {"name": "stock_increment", "type": "integer", "example": 60},
            {"name": "new_stock", "type": "integer", "example": 80},
            {"name": "reason", "type": "string", "example": "발주 입고"},
        ]
    }
    query = (
        "Galaxy 상품의 발주 입고를 반영하라. 입고 수량은 215, "
        "반영 후 재고는 250, 변경 사유는 발주 입고."
    )
    assert _parameter_values(query, spec) == {
        "product_name": "Galaxy",
        "stock_increment": 215,
        "new_stock": 250,
        "reason": "발주 입고",
    }


def test_only_enabled_mcp_servers_are_forwarded() -> None:
    assert _enabled_servers(
        {
            "mcpServers": {
                "on": {"command": "uvx", "enabled": True},
                "off": {"command": "uvx", "enabled": False},
            }
        }
    ) == {"mcpServers": {"on": {"command": "uvx"}}}


@pytest.mark.asyncio
async def test_executes_generated_code_as_subprocess_without_llm() -> None:
    code = """
import json, sys
payload = json.loads(sys.argv[1])
print(json.dumps({"ok": True, "results": [{"tool": "fixture", "data": payload}]}))
"""
    out = await _run_code(code, {"value": 42}, {"mcpServers": {}})
    assert out == {"ok": True, "results": [{"tool": "fixture", "data": {"value": 42}}]}
