"""LangGraph v2 스트림 envelope의 interrupts → state.__interrupt__ 병합 검증."""

from __future__ import annotations

import unittest

from app.agent.hitl import extract_interrupt_payload
from deepagents_executor import _coerce_langgraph_stream_chunk


class TestLanggraphStreamCoerceInterrupt(unittest.TestCase):
    def test_v2_values_envelope_merges_interrupts_into_state_dict(self) -> None:
        from langgraph.types import Interrupt

        intr = Interrupt(value={"question": "확인?", "context": ""}, id="test-id-1")
        part = {
            "type": "values",
            "ns": (),
            "data": {"messages": []},
            "interrupts": (intr,),
        }
        pairs = _coerce_langgraph_stream_chunk(part)
        self.assertEqual(len(pairs), 1)
        mode, data = pairs[0]
        self.assertEqual(mode, "values")
        assert isinstance(data, dict)
        self.assertIn("__interrupt__", data)
        payload = extract_interrupt_payload(data)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("question"), "확인?")

    def test_v2_values_without_interrupts_passes_data_unchanged(self) -> None:
        part = {"type": "values", "ns": (), "data": {"messages": [{"role": "user", "content": "hi"}]}}
        pairs = _coerce_langgraph_stream_chunk(part)
        self.assertEqual(pairs, [("values", part["data"])])

    def test_legacy_tuple_values_unchanged(self) -> None:
        from langgraph.types import Interrupt

        state = {"messages": [], "__interrupt__": (Interrupt(value={"question": "q"}, id="id2"),)}
        pairs = _coerce_langgraph_stream_chunk(("values", state))
        self.assertEqual(pairs, [("values", state)])


if __name__ == "__main__":
    unittest.main()
