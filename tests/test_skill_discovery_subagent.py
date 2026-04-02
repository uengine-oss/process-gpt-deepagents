from __future__ import annotations

import unittest
from unittest.mock import MagicMock


class SkillDiscoverySubagentTests(unittest.TestCase):
    def test_hinted_slugs_from_kebab_case(self) -> None:
        from subagents import hinted_skill_slugs_from_query

        h = hinted_skill_slugs_from_query(
            "global-investment-analyzer 스킬에 대해서 설명해줘.",
            allowed_skill_names=[],
        )
        self.assertIn("global-investment-analyzer", h)

    def test_hinted_slugs_includes_allowed_when_mentioned(self) -> None:
        from subagents import hinted_skill_slugs_from_query

        h = hinted_skill_slugs_from_query(
            "global-investment-analyzer 와 other-skill 사용",
            allowed_skill_names=["global-investment-analyzer", "other-skill"],
        )
        self.assertIn("global-investment-analyzer", h)
        self.assertIn("other-skill", h)

    def test_synthetic_row_name_matches_subagent(self) -> None:
        from skill_discovery_subagent import (
            SKILL_DISCOVERY_EVENT_ROLE,
            SKILL_DISCOVERY_SUBAGENT_NAME,
            build_skill_discovery_subagent,
            synthetic_skill_discovery_agent_row,
        )

        row = synthetic_skill_discovery_agent_row()
        sa = build_skill_discovery_subagent(all_tools=[], query="q", tenant_id="t1")
        self.assertEqual(row.get("alias"), SKILL_DISCOVERY_SUBAGENT_NAME)
        self.assertEqual(row.get("role"), SKILL_DISCOVERY_EVENT_ROLE)
        self.assertEqual(sa.get("name"), SKILL_DISCOVERY_SUBAGENT_NAME)

    def test_claude_skills_tools_attached_when_tagged(self) -> None:
        from skill_discovery_subagent import build_skill_discovery_subagent

        t1 = MagicMock()
        t1.name = "search_skills"
        setattr(t1, "_mcp_server", "claude-skills")
        t2 = MagicMock()
        t2.name = "other_mcp_tool"
        setattr(t2, "_mcp_server", "other-server")

        sa = build_skill_discovery_subagent(all_tools=[t1, t2], query="", tenant_id=None)
        names = {getattr(t, "name", None) for t in sa.get("tools") or []}
        self.assertIn("search_skills", names)
        self.assertNotIn("other_mcp_tool", names)


if __name__ == "__main__":
    unittest.main()
