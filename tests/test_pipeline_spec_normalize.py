from __future__ import annotations

import unittest

from app.executor.pipeline_spec_middleware import (
    normalize_from_raw_tool_args,
    unwrap_nested_pipeline_spec_keys,
)


class PipelineSpecNormalizeTests(unittest.TestCase):
    def test_flat_steps_and_asset_roles(self) -> None:
        raw = {"asset_roles": {"assets": []}, "steps": [{"id": "s1", "kind": "extract_structure"}]}
        out = normalize_from_raw_tool_args(raw)
        self.assertEqual(out["asset_roles"], {"assets": []})
        self.assertEqual(len(out["steps"]), 1)

    def test_wrapped_once(self) -> None:
        inner = {"asset_roles": {"assets": []}, "steps": []}
        raw = {"pipeline_spec": inner}
        out = normalize_from_raw_tool_args(raw)
        self.assertEqual(out, inner)

    def test_double_wrapped(self) -> None:
        inner = {"steps": [], "asset_roles": {"assets": []}}
        raw = {"pipeline_spec": {"pipeline_spec": inner}}
        out = normalize_from_raw_tool_args(raw)
        self.assertEqual(out, inner)

    def test_merge_nested_then_top_level_overrides(self) -> None:
        raw = {
            "pipeline_spec": {"asset_roles": {"assets": [{"id": "a"}]}, "steps": []},
            "steps": [{"id": "x"}],
        }
        out = normalize_from_raw_tool_args(raw)
        self.assertEqual(len(out["steps"]), 1)
        self.assertEqual(out["steps"][0]["id"], "x")
        self.assertEqual(out["asset_roles"], {"assets": [{"id": "a"}]})

    def test_unwrap_standalone(self) -> None:
        d = {"pipeline_spec": {"steps": [1], "asset_roles": {}}}
        self.assertEqual(unwrap_nested_pipeline_spec_keys(d), {"steps": [1], "asset_roles": {}})


if __name__ == "__main__":
    unittest.main()
