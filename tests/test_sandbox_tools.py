from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.sandbox.tools import (
    sandbox_cleanup,
    sandbox_copy_mounted_skill,
    sandbox_create,
    sandbox_read_artifacts,
    sandbox_read_workspace_files,
    sandbox_run_shell,
    sandbox_write_and_run,
    sandbox_write_files,
)


class SandboxToolsTests(unittest.TestCase):
    @staticmethod
    def _call(tool_obj, payload):
        inv = getattr(tool_obj, "invoke", None)
        if callable(inv):
            return inv(payload)
        return tool_obj(**payload)

    def test_tool_flow(self) -> None:
        created = self._call(sandbox_create, {"job_id": "job2", "agent_name": "agentB"})
        ws_id = created["workspace_id"]
        payload = json.dumps([{"path": "run.py", "content": "print('ok')\n"}], ensure_ascii=False)
        write_result = self._call(sandbox_write_files, {"workspace_id": ws_id, "files_json": payload})
        self.assertEqual(write_result["count"], 1)
        run_result = self._call(
            sandbox_run_shell,
            {"workspace_id": ws_id, "command": "python3 run.py", "timeout_seconds": 10}
        )
        self.assertEqual(run_result["exit_code"], 0)
        self.assertIn("ok", run_result["stdout"])
        artifacts = self._call(sandbox_read_artifacts, {"workspace_id": ws_id, "patterns_csv": "**/*.py"})
        self.assertGreaterEqual(artifacts["count"], 1)
        cleaned = self._call(sandbox_cleanup, {"workspace_id": ws_id, "keep_on_failure": False})
        self.assertTrue(cleaned["cleaned"])

    def test_copy_mounted_skill_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp) / "skills"
            tenant = "t1"
            skill_dir = skills_root / tenant / "demo_skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "run.py").write_text("print('demo')\n", encoding="utf-8")

            created = self._call(sandbox_create, {"job_id": "job_copy", "agent_name": "agentC"})
            ws_id = created["workspace_id"]
            copy_result = self._call(
                sandbox_copy_mounted_skill,
                {
                    "workspace_id": ws_id,
                    "tenant_id": tenant,
                    "skill_name": "demo_skill",
                    "skills_root": str(skills_root),
                },
            )
            self.assertNotIn("error", copy_result or {})
            self.assertGreater(len(copy_result.get("copied_files", [])), 0)

            read_result = self._call(
                sandbox_read_workspace_files,
                {"workspace_id": ws_id, "paths_csv": "skill/run.py"},
            )
            self.assertIn("demo", read_result["files"].get("skill/run.py", ""))

            run_result = self._call(
                sandbox_run_shell,
                {"workspace_id": ws_id, "command": "python3 skill/run.py", "timeout_seconds": 10},
            )
            self.assertEqual(run_result["exit_code"], 0)
            self.assertIn("demo", run_result["stdout"])
            self._call(sandbox_cleanup, {"workspace_id": ws_id, "keep_on_failure": False})

    def test_write_and_run_default_python(self) -> None:
        created = self._call(sandbox_create, {"job_id": "job_war", "agent_name": "agentW"})
        ws_id = created["workspace_id"]
        out = self._call(
            sandbox_write_and_run,
            {
                "workspace_id": ws_id,
                "relative_path": "x.py",
                "content": "print('war_ok')\n",
                "timeout_seconds": 15,
            },
        )
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("war_ok", out["stdout"])
        self._call(sandbox_cleanup, {"workspace_id": ws_id, "keep_on_failure": False})


if __name__ == "__main__":
    unittest.main()

