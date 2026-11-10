from core.chat.stream_scope import is_subagent_namespace


def test_root_graph_is_not_a_subagent():
    assert is_subagent_namespace(()) is False
    assert is_subagent_namespace(None) is False


def test_task_tool_subagent_namespaces():
    assert is_subagent_namespace(("tools:6f1c",)) is True
    assert is_subagent_namespace(("tools:6f1c", "model:aa")) is True
    assert is_subagent_namespace("tools:6f1c") is True


def test_other_subgraphs_are_kept():
    # 도구가 아닌 하위 그래프(예: 미들웨어 노드)의 글은 거르지 않는다
    assert is_subagent_namespace(("model:aa",)) is False
