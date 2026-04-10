from __future__ import annotations

import unittest

from app.executor.pipeline_validator import (
    validate_pipeline_execution_state,
    validate_pipeline_spec,
)


class PipelineValidatorTests(unittest.TestCase):
    def test_template_candidate_requires_extract_structure(self) -> None:
        spec = {
            "asset_roles": {"assets": [{"role": "template_candidate", "path": "a.pdf", "type": "pdf"}]},
            "steps": [
                {"id": "e1", "kind": "extract_facts", "input_assets": ["asset_0"], "output_key": "facts"},
                {"id": "r1", "kind": "render_artifact", "input": "facts", "format": "xlsx", "output_key": "artifact_file"},
                {"id": "p1", "kind": "publish_artifact", "input": "artifact_file", "output_key": "public_url"},
            ],
        }
        ok, errs = validate_pipeline_spec(spec)
        self.assertFalse(ok)
        self.assertTrue(any("extract_structure" in e for e in errs))
        self.assertTrue(any("map_to_template" in e for e in errs))

    def test_valid_typical_pipeline(self) -> None:
        spec = {
            "asset_roles": {
                "assets": [
                    {"id": "asset_0", "role": "template_candidate", "path": "t.pdf", "type": "pdf"},
                    {"id": "asset_1", "role": "source_of_truth", "path": "p.bpmn", "type": "bpmn"},
                ]
            },
            "steps": [
                {
                    "id": "s1",
                    "kind": "extract_structure",
                    "input_assets": ["asset_0"],
                    "output_key": "template_schema",
                },
                {
                    "id": "s2",
                    "kind": "extract_facts",
                    "input_assets": ["asset_1"],
                    "output_key": "process_facts",
                },
                {
                    "id": "s3",
                    "kind": "map_to_template",
                    "inputs": ["template_schema", "process_facts"],
                    "output_key": "filled_template",
                },
                {
                    "id": "s4",
                    "kind": "render_artifact",
                    "input": "filled_template",
                    "format": "xlsx",
                    "output_key": "artifact_file",
                },
                {
                    "id": "s5",
                    "kind": "publish_artifact",
                    "input": "artifact_file",
                    "output_key": "public_url",
                },
            ],
        }
        ok, errs = validate_pipeline_spec(spec)
        self.assertTrue(ok, errs)
        self.assertEqual(errs, [])

    def test_publish_before_render_fails(self) -> None:
        spec = {
            "asset_roles": {"assets": []},
            "steps": [
                {"id": "s1", "kind": "extract_structure", "input_assets": ["a"], "output_key": "template_schema"},
                {"id": "s2", "kind": "extract_facts", "input_assets": ["b"], "output_key": "process_facts"},
                {"id": "s3", "kind": "map_to_template", "inputs": ["template_schema", "process_facts"], "output_key": "filled_template"},
                {"id": "s5", "kind": "publish_artifact", "input": "artifact_file", "output_key": "public_url"},
                {"id": "s4", "kind": "render_artifact", "input": "filled_template", "format": "xlsx", "output_key": "artifact_file"},
            ],
        }
        ok, errs = validate_pipeline_spec(spec)
        self.assertFalse(ok)
        self.assertTrue(any("after render" in e for e in errs))

    def test_render_requires_publish_without_template_candidate(self) -> None:
        spec = {
            "asset_roles": {"assets": [{"role": "source_of_truth", "path": "x.bpmn", "type": "bpmn"}]},
            "steps": [
                {"id": "s1", "kind": "extract_facts", "input_assets": ["a"], "output_key": "facts"},
                {"id": "s2", "kind": "render_artifact", "input": "facts", "format": "json", "output_key": "out"},
            ],
        }
        ok, errs = validate_pipeline_spec(spec)
        self.assertFalse(ok)
        self.assertTrue(any("publish_artifact" in e for e in errs))

    def test_template_candidate_order_enforced(self) -> None:
        spec = {
            "asset_roles": {"assets": [{"role": "template_candidate", "path": "t.pdf", "type": "pdf"}]},
            "steps": [
                {"id": "a", "kind": "extract_structure", "input_assets": ["x"], "output_key": "template_schema"},
                {"id": "b", "kind": "render_artifact", "input": "filled_template", "format": "xlsx", "output_key": "artifact_file"},
                {"id": "c", "kind": "map_to_template", "inputs": ["template_schema"], "output_key": "filled_template"},
                {"id": "d", "kind": "publish_artifact", "input": "artifact_file", "output_key": "public_url"},
            ],
        }
        ok, errs = validate_pipeline_spec(spec)
        self.assertFalse(ok)
        self.assertTrue(any("step order" in e for e in errs))

    def test_execution_state_requires_public_url_after_render(self) -> None:
        spec = {
            "asset_roles": {"assets": []},
            "steps": [{"id": "r", "kind": "render_artifact", "input": "x", "format": "xlsx", "output_key": "f"}],
        }
        ok, errs = validate_pipeline_execution_state(spec, {"f": {"text": "done"}})
        self.assertFalse(ok)
        self.assertTrue(any("public_url" in e for e in errs))
        ok2, errs2 = validate_pipeline_execution_state(spec, {"u": {"public_url": "https://example.com/a.xlsx"}})
        self.assertTrue(ok2, errs2)


if __name__ == "__main__":
    unittest.main()
