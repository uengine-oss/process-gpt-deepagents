from __future__ import annotations

import unittest


class ExecutorEventTests(unittest.TestCase):
    def test_build_event_payload_shape(self) -> None:
        try:
            from deepagents_executor import ExecutionContext, build_event_payload
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        ctx = ExecutionContext(
            correlation_id="c1",
            agent_path=["작업 실행 기획자"],
            task_id="t1",
            proc_inst_id="p1",
        )
        payload = build_event_payload(
            ctx,
            logical_event_type="agent_started",
            phase="planning",
            extra_data={"message": "x"},
        )
        self.assertEqual(payload["correlation_id"], "c1")
        self.assertEqual(payload["phase"], "planning")
        self.assertEqual(payload["message"], "x")

    def test_execution_context_agent_role_in_payload(self) -> None:
        try:
            from deepagents_executor import ExecutionContext, build_event_payload
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"executor import unavailable: {exc}")
            return

        ctx = ExecutionContext(
            correlation_id="c1",
            agent_path=["작업 실행 기획자", "서브 에이전트"],
            task_id="t1",
            proc_inst_id="p1",
            agent_role="서브 에이전트",
        )
        payload = build_event_payload(
            ctx,
            logical_event_type="tool_call",
            phase="tool_call",
            extra_data={"tool_name": "search"},
        )
        self.assertEqual(payload["role"], "서브 에이전트")
        self.assertEqual(payload["tool_name"], "search")

    def test_depth1_task_started_includes_required_agent_fields(self) -> None:
        try:
            from deepagents_executor import _ensure_required_root_task_started_fields
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        payload = {
            "depth": 1,
            "agent_name": "작업 실행 기획자",
            "message": "단일 DeepAgent 실행을 시작했습니다.",
        }
        _ensure_required_root_task_started_fields(payload, event_type="task_started")

        self.assertEqual(payload["role"], "작업 실행 기획자")
        self.assertEqual(payload["goal"], "단일 DeepAgent 실행을 시작했습니다.")
        self.assertEqual(payload["agent_profile"], "/images/chat-icon.png")

    def test_should_defer_hitl_interrupt_for_missing_parameter(self) -> None:
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "question": "필수 값이 누락되어 있습니다. 요청 변수를 확인하세요.",
            "reason": "ERROR-100 required value is missing",
            "raw_payload": {"requires_human": True},
        }
        self.assertTrue(_should_defer_hitl_interrupt(decision))

    def test_should_defer_hitl_interrupt_for_credentials_text_by_default(self) -> None:
        """Credential-shaped questions defer by default so mem0/MCP/sandbox can run first."""
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "question": "API key를 입력해 주세요.",
            "reason": "missing api key",
            "raw_payload": {"requires_human": True},
        }
        self.assertTrue(_should_defer_hitl_interrupt(decision))

    def test_should_not_defer_when_raw_hitl_immediate(self) -> None:
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "question": "Anything",
            "raw_payload": {"requires_human": True, "hitl_immediate": True},
        }
        self.assertFalse(_should_defer_hitl_interrupt(decision))

    def test_should_defer_natural_language_only_no_structural_flags(self) -> None:
        """Approval/legal wording alone does not trigger immediate HITL — tools must set flags."""
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "question": "법적 검토 및 담당자에게 허가가 나면 진행합니다.",
            "raw_payload": {"requires_human": True},
        }
        self.assertTrue(_should_defer_hitl_interrupt(decision))

    def test_should_not_defer_when_defer_hitl_false(self) -> None:
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "question": "x",
            "raw_payload": {"requires_human": True, "defer_hitl": False},
        }
        self.assertFalse(_should_defer_hitl_interrupt(decision))

    def test_should_not_defer_when_credential_required(self) -> None:
        try:
            from deepagents_executor import _should_defer_hitl_interrupt
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {
            "raw_payload": {"requires_human": True, "credential_required": True},
        }
        self.assertFalse(_should_defer_hitl_interrupt(decision))

    def test_resolve_pending_field_from_decision(self) -> None:
        try:
            from deepagents_executor import _resolve_pending_field_from_decision
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        decision = {"input_schema": {"field": "api_key"}}
        self.assertEqual(_resolve_pending_field_from_decision(decision), "api_key")

    def test_non_final_task_completed_is_not_terminal(self) -> None:
        try:
            from deepagents_executor import _enqueue_status
            from a2a.types import TaskState
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        class DummyQueue:
            def __init__(self) -> None:
                self.events = []

            def enqueue_event(self, event) -> None:
                self.events.append(event)

        q = DummyQueue()
        _enqueue_status(
            q,
            "task_completed",
            {"message": "planning done"},
            "p1",
            "t1",
            "deepagents",
            phase="planning",
        )
        self.assertEqual(len(q.events), 1)
        self.assertEqual(q.events[0].status.state, TaskState.TASK_STATE_WORKING)

    def test_classify_tenant_context_tool(self) -> None:
        try:
            from deepagents_executor import _classify_tenant_context_tool
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"executor import unavailable: {exc}")
            return

        class T:
            def __init__(self, name: str, server: str | None = None) -> None:
                self.name = name
                self._mcp_server = server

        self.assertEqual(_classify_tenant_context_tool(T("search", "mem0")), "mem0")
        self.assertEqual(_classify_tenant_context_tool(T("x", "org-memento")), "memento")
        self.assertEqual(_classify_tenant_context_tool(T("dmn_rule", "dmn-rule")), "dmn_rule")
        self.assertIsNone(_classify_tenant_context_tool(T("list_skills", "some-mcp")))

    def test_duplicate_hitl_request_detection(self) -> None:
        try:
            from deepagents_executor import _is_duplicate_hitl_request
        except Exception as exc:  # pragma: no cover - optional dependency environment
            self.skipTest(f"executor import unavailable: {exc}")
            return

        run_state = {
            "pending_field": "human_response",
            "last_question": "필수 값을 입력해 주세요",
        }
        self.assertTrue(
            _is_duplicate_hitl_request(
                run_state,
                question="필수 값을 입력해 주세요",
                pending_field="human_response",
            )
        )

    def test_coerce_tool_output_wraps_plain_string_for_tool_call(self) -> None:
        try:
            from langchain_core.messages import ToolMessage

            from deepagents_executor import _coerce_tool_output_for_tool_node
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"executor/langchain import unavailable: {exc}")
            return

        tool_input = {
            "name": "request_human_input",
            "args": {"question": "q"},
            "id": "call-abc",
            "type": "tool_call",
        }
        out = _coerce_tool_output_for_tool_node("hello", tool_input, "request_human_input")
        self.assertIsInstance(out, ToolMessage)
        self.assertEqual(out.content, "hello")
        self.assertEqual(out.tool_call_id, "call-abc")
        self.assertEqual(out.name, "request_human_input")

    def test_new_status_update_event_crew_type_result_omits_unspecified_fields(self) -> None:
        try:
            from app.executor.a2a_events import new_status_update_event
            from app.executor.context import ExecutionContext
        except Exception as exc:
            self.skipTest(f"imports unavailable: {exc}")
            return

        # Case 1: crew_type is 'result', unspecified logical_event_type, phase, agent_name
        ctx = ExecutionContext(
            correlation_id="c1",
            agent_path=["test_agent"],
            task_id="t1",
            proc_inst_id="p1",
        )
        evt = new_status_update_event(
            event_type="task_started",
            data={"message": "hello"},
            proc_inst_id="p1",
            task_id="t1",
            crew_type="result",
            exec_ctx=ctx,
        )
        from google.protobuf.json_format import MessageToDict
        meta = MessageToDict(evt.metadata)
        payload = meta.get("payload", {})
        self.assertNotIn("logical_event_type", payload)
        self.assertNotIn("phase", payload)
        self.assertNotIn("agent_name", payload)

        # Case 2: crew_type is 'result', but they are explicitly provided
        evt_explicit = new_status_update_event(
            event_type="task_started",
            data={"message": "hello"},
            proc_inst_id="p1",
            task_id="t1",
            crew_type="result",
            exec_ctx=ctx,
            logical_event_type="custom_started",
            phase="custom_phase",
            agent_name="custom_agent",
        )
        meta_explicit = MessageToDict(evt_explicit.metadata)
        payload_explicit = meta_explicit.get("payload", {})
        self.assertEqual(payload_explicit.get("logical_event_type"), "custom_started")
        self.assertEqual(payload_explicit.get("phase"), "custom_phase")
        self.assertEqual(payload_explicit.get("agent_name"), "custom_agent")


if __name__ == "__main__":
    unittest.main()

