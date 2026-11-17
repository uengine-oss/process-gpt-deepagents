"""턴 시작에 대화 기록을 통째로 다시 쓴 갱신은 이번 턴의 도구 호출로 처리하지 않는다."""
from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from executor import _rewrites_history


def test_patch_tool_calls_rewrite_is_history():
    old = AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/workspace/a.md"}, "id": "c1"}])
    update = [RemoveMessage(id=REMOVE_ALL_MESSAGES), old, ToolMessage(content="ok", tool_call_id="c1")]
    assert _rewrites_history(update) is True


def test_new_messages_are_not_history():
    new = AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/workspace/b.md"}, "id": "c2"}])
    assert _rewrites_history([new]) is False
    assert _rewrites_history([]) is False
    assert _rewrites_history([RemoveMessage(id="m1"), new]) is False
