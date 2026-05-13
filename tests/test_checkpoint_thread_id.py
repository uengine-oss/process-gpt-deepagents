"""Checkpoint thread id resolution for chat vs worker modes."""

from __future__ import annotations

import unittest

from app.executor.postgres_checkpointer import resolve_checkpoint_thread_id


class TestResolveCheckpointThreadId(unittest.TestCase):
    def test_chat_prefers_row_id(self) -> None:
        tid = resolve_checkpoint_thread_id(
            is_chat=True,
            row={"id": "conv-1", "proc_inst_id": "p1", "root_proc_inst_id": "r1"},
        )
        self.assertEqual(tid, "conv-1")

    def test_chat_falls_back_to_proc_inst(self) -> None:
        tid = resolve_checkpoint_thread_id(
            is_chat=True,
            row={"proc_inst_id": "room-9"},
        )
        self.assertEqual(tid, "room-9")

    def test_worker_uses_row_id(self) -> None:
        tid = resolve_checkpoint_thread_id(
            is_chat=False,
            row={"id": "todo-uuid-123"},
        )
        self.assertEqual(tid, "todo-uuid-123")

    def test_empty_when_missing(self) -> None:
        self.assertEqual(resolve_checkpoint_thread_id(is_chat=True, row={}), "")
        self.assertEqual(resolve_checkpoint_thread_id(is_chat=False, row={}), "")


if __name__ == "__main__":
    unittest.main()
