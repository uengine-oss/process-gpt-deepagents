"""새 대화의 첫 메시지도 자기 방(작업 폴더)에서 일한다.

대화 id 없이 온 첫 메시지는 SDK 가 대화 id(context_id)를 새로 매겨 화면에 돌려준다. 실행기가 그 id 를
방으로 쓰지 않으면 테넌트 공용 .bpmn/ 에서 일해, 다른 실행이 남긴 파일(.skill-evals/… 등)이 이번
산출물과 답에 섞이고, 다음 턴(같은 id)은 이번에 만든 파일을 찾지 못한다(2026-10-27 Lite 공유 e2e).
"""

from core.agents.input_builder import resolve_chat_room_id


def test_first_chat_message_uses_the_new_conversation_id():
    assert resolve_chat_room_id({}, {"conversation_id": None}, new_conversation_id="458ffb48027c407bbd8b51c4a81abb84") == "458ffb48027c407bbd8b51c4a81abb84"


def test_a_known_conversation_keeps_its_id():
    assert resolve_chat_room_id({"conversation_id": "room-1"}, {}, new_conversation_id="new-ctx") == "room-1"
    assert resolve_chat_room_id({}, {"conversation_id": "room-2"}, new_conversation_id="new-ctx") == "room-2"


def test_without_a_new_id_nothing_changes():
    # 폴러 실행(채팅 아님)은 새 id 를 주지 않는다: 예전처럼 방 없음
    assert resolve_chat_room_id({}, {}) == ""


def test_the_executor_hands_the_chat_context_id_over():
    # 실행기가 채팅일 때 context_id 를 넘기는지(넘기지 않으면 위 규칙이 쓰이지 않는다)
    import inspect

    import executor

    src = inspect.getsource(executor.DeepAgentExecutor.execute)
    assert "new_conversation_id=context_id if is_chat" in src
