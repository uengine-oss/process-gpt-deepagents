from __future__ import annotations

import unittest


class DomainAgentMcpPrefetchTests(unittest.TestCase):
    def test_resolve_suffix_alias(self) -> None:
        from app.executor.domain_agent_mcp_prefetch import resolve_tenant_mcp_config_key_for_declared_server

        mcp_servers = {"tenant-user-gmail-mcp": {"url": "https://example.invalid"}}
        self.assertEqual(
            resolve_tenant_mcp_config_key_for_declared_server(
                mcp_servers=mcp_servers,
                declared="gmail-mcp",
            ),
            "tenant-user-gmail-mcp",
        )

    def test_extend_appends_matching_server_keys(self) -> None:
        from app.executor.domain_agent_mcp_prefetch import extend_mcp_request_with_domain_agent_declared_servers

        req = ["mem0"]
        extend_mcp_request_with_domain_agent_declared_servers(
            req,
            mcp_servers={"custom-mcp": {}},
            domain_agent_rows=[{"tools": "custom-mcp", "agent_type": ""}],
        )
        self.assertIn("custom-mcp", req)
        self.assertEqual(req.count("custom-mcp"), 1)

    def test_extend_skips_a2a_tool_names(self) -> None:
        from app.executor.domain_agent_mcp_prefetch import extend_mcp_request_with_domain_agent_declared_servers

        req: list[str] = []
        extend_mcp_request_with_domain_agent_declared_servers(
            req,
            mcp_servers={"some-server": {}},
            domain_agent_rows=[{"tools": "some_tool_name", "agent_type": "a2a"}],
        )
        self.assertEqual(req, [])


if __name__ == "__main__":
    unittest.main()
