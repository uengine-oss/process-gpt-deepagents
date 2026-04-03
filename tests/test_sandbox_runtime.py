from __future__ import annotations

import unittest

from app.sandbox.runtime import LocalSandboxRuntime


class SandboxRuntimeTests(unittest.TestCase):
    def test_workspace_write_run_collect_cleanup(self) -> None:
        runtime = LocalSandboxRuntime()
        ws = runtime.create_workspace(job_id="job1", agent_name="agentA")
        runtime.write_files(
            ws,
            [{"path": "scripts/hello.py", "content": "print('hello')\n"}],
        )
        result = runtime.run_shell(ws, "python3 scripts/hello.py", timeout=10)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)
        artifacts = runtime.collect_artifacts(ws, ["**/*.py"])
        self.assertTrue(any(p.endswith("hello.py") for p in artifacts))
        runtime.cleanup(ws, keep_on_failure=False)


if __name__ == "__main__":
    unittest.main()

