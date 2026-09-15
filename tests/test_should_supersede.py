"""새 turn 이 이전 turn 을 대체하는지 이어가는지 — DeepAgentExecutor 의 판단.

SDK 는 turn 을 시작하기 전에 이 훅을 물어보고, False 면 이전 turn 을 취소하지 않고
끝나기를 기다린다. 예전에는 이 판단을 하려고 SDK 의 InflightRegistry 전체를 Redis
구현으로 갈아끼웠다(core/chat/sdk_registry.py). 판단만 돌려주면 되는 일이었다.

틀렸을 때의 증상이 조용하지 않다: HITL 응답인데 True 를 돌려주면 이전 turn 이
취소되면서 interrupt 체크포인트가 사라져 재개가 불가능해지고, 화면에는
"새 메시지로 대체되어 이전 응답이 취소되었습니다" 만 남는다.
"""

from executor import DeepAgentExecutor


class _Context:
    """`get_context_data()` 만 있으면 되는 최소 컨텍스트."""

    def __init__(self, extras=None, row=None):
        self._data = {"extras": extras or {}, "row": row or {}}

    def get_context_data(self):
        return self._data


def _decide(extras=None, row=None) -> bool:
    return DeepAgentExecutor().should_supersede(_Context(extras, row))


def test_일반_메시지는_이전_턴을_대체한다():
    assert _decide({"metadata": {}}) is True


def test_HITL_응답은_이전_턴을_이어간다():
    """프론트는 request_human_input 도구 호출 시점에 이미 확인 패널을 그린다 —
    사용자가 곧바로 답하면 이전 turn 이 아직 돌고 있다."""
    extras = {
        "human_response_answer": "승인",
        "metadata": {"run_state": {"tool_name": "request_human_input"}},
    }
    assert _decide(extras) is False


def test_run_state_가_extras_최상위에_있어도_인식한다():
    extras = {
        "human_response_answer": "승인",
        "run_state": {"tool_name": "request_human_input"},
        "metadata": {},
    }
    assert _decide(extras) is False


def test_run_state_가_workitem_draft_에_있어도_인식한다():
    """todolist 경로는 run_state 를 workitem 의 draft 로 실어 나른다."""
    extras = {"human_response_answer": "승인", "metadata": {}}
    row = {"draft": {"run_state": {"tool_name": "request_human_input"}}}
    assert _decide(extras, row) is False


def test_답변이_비어_있으면_이어가기가_아니다():
    """run_state 만 남아 있고 답변이 없는 것은 그냥 새 메시지다."""
    extras = {
        "human_response_answer": "   ",
        "metadata": {"run_state": {"tool_name": "request_human_input"}},
    }
    assert _decide(extras) is True


def test_다른_도구의_run_state_는_이어가기가_아니다():
    extras = {
        "human_response_answer": "승인",
        "metadata": {"run_state": {"tool_name": "write_file"}},
    }
    assert _decide(extras) is True


def test_run_state_가_없으면_이어가기가_아니다():
    extras = {"human_response_answer": "승인", "metadata": {}}
    assert _decide(extras) is True


def test_컨텍스트가_비어_있어도_터지지_않는다():
    """프로세스/워크리스트 경로는 채팅과 다른 모양의 컨텍스트로 들어온다."""
    assert _decide() is True

    class _Empty:
        def get_context_data(self):
            return None

    assert DeepAgentExecutor().should_supersede(_Empty()) is True
