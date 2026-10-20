"""방 목록 미리보기가 마지막 에이전트 답과 같다(2026-10-20).

실환경에서 미리보기는 '참고할 문서를 검색했습니다…', 실제 답은 '…파일을 찾지 못했습니다' 였다.
화면이 최종 답을 받을 때만 미리보기를 고쳐, 사용자가 방을 떠났거나 할 일 폴러가 답한 방은
사용자 질문·중간 안내·'NEW' 가 남았다. 답을 저장하는 서버가 미리보기도 쓴다.
"""
from core.chat import chat_room


class _Table:
    def __init__(self, sink):
        self.sink = sink

    def update(self, payload):
        self.sink["payload"] = payload
        return self

    def eq(self, key, value):
        self.sink["where"] = (key, value)
        return self

    def execute(self):
        self.sink["executed"] = True


class _Client:
    def __init__(self, sink):
        self.sink = sink

    def table(self, name):
        self.sink["table"] = name
        return _Table(self.sink)


def test_preview_is_the_final_answer(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(chat_room, "get_supabase", lambda: _Client(sink))
    answer = "작업 디렉터리에서 '한빛상사 견적서' 관련 파일을 찾지 못했습니다. 견적서를 다시 올려 주세요."
    chat_room.set_chat_room_preview(room_id="room-1", text=answer)
    assert sink["table"] == "chat_rooms" and sink["where"] == ("id", "room-1")
    assert sink["payload"]["message"]["msg"] == answer[: chat_room.PREVIEW_LEN]
    assert sink["payload"]["message"]["type"] == "text"


def test_empty_answer_keeps_the_preview(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(chat_room, "get_supabase", lambda: _Client(sink))
    chat_room.set_chat_room_preview(room_id="room-1", text="  ")
    assert "executed" not in sink


def test_executor_writes_the_preview_when_a_turn_or_handover_finishes():
    """채팅 턴 종료와 인계 결과 저장 두 곳에서 미리보기를 쓴다(연결이 빠지면 다시 어긋난다)."""
    src = open("executor.py", encoding="utf-8").read()
    assert "await a_set_chat_room_preview(room_id=chat_room_id, text=raw_text)" in src
    assert "await a_set_chat_room_preview(room_id=room_id, text=content)" in src
