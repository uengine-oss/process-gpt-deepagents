from __future__ import annotations

import unittest

from app.executor.asset_role_resolver import resolve_asset_roles
from app.executor.pipeline_runner import order_pipeline_steps


class AssetRolesTests(unittest.TestCase):
    def test_roles_assigned_with_confidence(self) -> None:
        out = resolve_asset_roles(
            assets=[{"id": "asset_0", "path": "form_template.pdf", "type": "pdf"}],
            user_text="양식 PDF를 채워 엑셀로 만들어줘",
        )
        assets = out.get("assets") or []
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].get("role"), "template_candidate")
        self.assertEqual(float(assets[0].get("confidence") or 0), 1.0)

    def test_pdf_forced_template_when_form_keywords_even_if_scores_differ(self) -> None:
        out = resolve_asset_roles(
            assets=[{"id": "a0", "path": "report.pdf", "type": "pdf"}],
            user_text="첨부 초안을 검토해 주세요",
        )
        assets = out.get("assets") or []
        self.assertEqual(assets[0].get("role"), "template_candidate")
        self.assertEqual(assets[0].get("confidence"), 1.0)

    def test_pdf_not_forced_without_form_keywords(self) -> None:
        out = resolve_asset_roles(
            assets=[{"id": "a0", "path": "manual.pdf", "type": "pdf"}],
            user_text="이 문서를 요약해 줘",
        )
        assets = out.get("assets") or []
        self.assertNotEqual(assets[0].get("role"), "template_candidate")

    def test_multi_pdf_template_picks_form_like_file(self) -> None:
        out = resolve_asset_roles(
            assets=[
                {"id": "a0", "path": "https://x/RQ_BP_001_요구사항.pdf", "type": "pdf"},
                {
                    "id": "a1",
                    "path": "https://x/업무Flow%20초안%20양식%20-%20Next%20BSS%20-%20DOSS%20Wiki.pdf",
                    "type": "pdf",
                },
                {"id": "a2", "path": "https://x/RQ_BP_002_참고.pdf", "type": "pdf"},
            ],
            user_text="첨부 양식에 맞춰 엑셀 초안을 만들어 주세요",
        )
        assets = out.get("assets") or []
        by_id = {str(a.get("id")): a for a in assets}
        self.assertEqual(by_id["a1"].get("role"), "template_candidate")
        self.assertEqual(by_id["a0"].get("role"), "reference_material")
        self.assertEqual(by_id["a2"].get("role"), "reference_material")


class PipelineOrderTests(unittest.TestCase):
    def test_topological_order(self) -> None:
        steps = [
            {"id": "a", "kind": "map_to_template", "inputs": ["template_schema", "process_facts"], "output_key": "filled_template"},
            {"id": "b", "kind": "extract_structure", "input_assets": ["x"], "output_key": "template_schema"},
            {"id": "c", "kind": "extract_facts", "input_assets": ["y"], "output_key": "process_facts"},
        ]
        ordered = order_pipeline_steps(steps)
        self.assertIsNotNone(ordered)
        assert ordered is not None
        kinds = [s["kind"] for s in ordered]
        self.assertLess(kinds.index("extract_structure"), kinds.index("map_to_template"))
        self.assertLess(kinds.index("extract_facts"), kinds.index("map_to_template"))


if __name__ == "__main__":
    unittest.main()
