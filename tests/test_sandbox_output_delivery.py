from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.sandbox.output_delivery import (
    collect_readable_artifacts,
    note_sandbox_tool_output_paths,
    sandbox_output_paths_begin_task,
    take_sandbox_output_paths_task,
)


class SandboxOutputDeliveryTests(unittest.TestCase):
    def test_note_and_take_paths(self) -> None:
        sandbox_output_paths_begin_task("tid-1")
        note_sandbox_tool_output_paths(
            "tid-1",
            "sandbox_write_files",
            {"written_files": ["/tmp/a.xlsx", "/tmp/b.py"]},
            None,
        )
        got = take_sandbox_output_paths_task("tid-1")
        self.assertEqual(got, {"/tmp/a.xlsx", "/tmp/b.py"})
        self.assertEqual(take_sandbox_output_paths_task("tid-1"), set())

    def test_collect_readable_respects_sandbox_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SANDBOX_BASE_DIR"] = tmp
            p = Path(tmp) / "out.csv"
            p.write_text("a,b\n1,2\n", encoding="utf-8")
            got = collect_readable_artifacts({str(p)})
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0][0], "out.csv")
            self.assertIn(b"a,b", got[0][1])


if __name__ == "__main__":
    unittest.main()
