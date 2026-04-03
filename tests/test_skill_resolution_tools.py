from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import app.skills.resolution_tools as srt
from app.skills.resolution_tools import get_skill_resolution_tools


class SkillResolutionToolsTests(unittest.TestCase):
    @staticmethod
    def _call(tool_obj, payload):
        inv = getattr(tool_obj, "invoke", None)
        if callable(inv):
            return inv(payload)
        return tool_obj(**payload)

    def test_skill_volume_probe_finds_skill_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "t1" / "my_skill").mkdir(parents=True)
            (root / "t1" / "my_skill" / "SKILL.md").write_text("body", encoding="utf-8")
            prev = srt.SKILLS_ROOT_DIR
            try:
                srt.SKILLS_ROOT_DIR = str(root)
                with self.assertLogs("app.skills.resolution_tools", level=logging.INFO) as al:
                    raw = self._call(
                        srt.skill_volume_probe,
                        {"tenant_id": "t1", "skill_names_csv": "my_skill,other"},
                    )
            finally:
                srt.SKILLS_ROOT_DIR = prev
        self.assertTrue(
            any("skill_volume_probe" in e for e in al.output),
            al.output,
        )
        data = json.loads(raw)
        self.assertEqual(data["tier"], 1)
        self.assertTrue(data["tier1_any_hit"])
        self.assertEqual(data["missing"], ["other"])

    def test_get_skill_resolution_tools_count(self) -> None:
        tools = get_skill_resolution_tools()
        self.assertGreaterEqual(len(tools), 2)

    def test_report_tier_returns_json(self) -> None:
        raw = self._call(
            srt.report_skill_resolution_tier,
            {"tier": 2, "status": "started", "detail": "mcp"},
        )
        data = json.loads(raw)
        self.assertTrue(data.get("logged"))
        self.assertEqual(data.get("tier"), 2)


if __name__ == "__main__":
    unittest.main()
