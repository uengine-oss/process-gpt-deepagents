from __future__ import annotations

import unittest


class SkillSlugHintTests(unittest.TestCase):
    def test_hinted_slugs_from_kebab_case(self) -> None:
        from app.agent.subagents import hinted_skill_slugs_from_query

        h = hinted_skill_slugs_from_query(
            "global-investment-analyzer 스킬에 대해서 설명해줘.",
            allowed_skill_names=[],
        )
        self.assertIn("global-investment-analyzer", h)

    def test_hinted_slugs_includes_allowed_when_mentioned(self) -> None:
        from app.agent.subagents import hinted_skill_slugs_from_query

        h = hinted_skill_slugs_from_query(
            "global-investment-analyzer 와 other-skill 사용",
            allowed_skill_names=["global-investment-analyzer", "other-skill"],
        )
        self.assertIn("global-investment-analyzer", h)
        self.assertIn("other-skill", h)


if __name__ == "__main__":
    unittest.main()
