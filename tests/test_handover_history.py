"""인계로 받은 일은 앞선 방들의 대화를 맥락으로 받는다(2026-10-23).

예전에는 넘기는 쪽 화면에 불러온 메시지(최근 10개 남짓)만 '대화 기록.md' 로 복사해 넘겼다.
이제 받은 방 id 로 인계 체인을 거슬러 올라가 원래 대화를 읽는다. 각 방은 인계한 시각까지다.
"""
from core.agents.input_builder import build_graph_inputs
from core.chat.handover_history import build_handover_history

EVENTS = [
    # 시작 방 A → 받은 방 B (홍길동 → 문서 생성 전문가), B → C (홍길동 → 이팀장)
    {"tenant_id": "t", "from_room_id": "A", "to_room_id": "B", "occurred_at": "2026-10-20T07:09:30+00:00",
     "from_user_name": "홍길동", "to_user_name": "문서 생성 전문가"},
    {"tenant_id": "t", "from_room_id": "B", "to_room_id": "C", "occurred_at": "2026-10-20T07:11:35+00:00",
     "from_user_name": "홍길동", "to_user_name": "이팀장"},
]
CHATS = {
    "A": [
        {"role": "user", "name": "홍길동", "content": "서울전자 견적 요청: 서버 3대, 대당 500만원", "timeStamp": "2026-10-20T07:05:00+00:00"},
        {"role": "user", "name": "홍길동", "content": "인계 뒤에 쓴 다른 이야기", "timeStamp": "2026-10-20T08:00:00+00:00"},
    ],
    "B": [
        {"role": "system", "name": "홍길동", "content": "홍길동 님이 업무를 넘겼습니다.", "timeStamp": "2026-10-20T07:09:31+00:00"},
        {"role": "assistant", "name": "문서 생성 전문가", "content": "견적서를 작성했습니다. 합계 16,500,000원", "timeStamp": "2026-10-20T07:11:11+00:00"},
    ],
}
ROOMS = [{"id": "A", "name": "견적 요청 · 서울전자"}, {"id": "B", "name": "견적서 초안 작성 · 견적 요청 · 서울전자"}]


class _Query:
    def __init__(self, table):
        self.table = table
        self.filters = {}
        self.in_filter = None

    def select(self, *_):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def in_(self, k, vs):
        self.in_filter = (k, set(vs))
        return self

    def order(self, *_ , **__):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        class R:
            pass

        r = R()
        if self.table == "handover_events":
            r.data = [e for e in EVENTS if all(e.get(k) == v for k, v in self.filters.items())]
        elif self.table == "chats":
            r.data = [{"uuid": str(i), "messages": m} for i, m in enumerate(CHATS.get(self.filters.get("id"), []))]
        else:
            k, vs = self.in_filter
            r.data = [x for x in ROOMS if x[k] in vs]
        return r


class _Sb:
    def table(self, name):
        return _Query(name)


def test_받은_방은_시작_방부터_앞선_대화를_순서대로_받는다():
    hist = build_handover_history(_Sb(), "C", "t")
    assert len(hist) == 1 and hist[0]["role"] == "user"
    text = hist[0]["content"]
    # 시작 방이 먼저, 바로 앞 방이 다음
    assert text.index("견적 요청 · 서울전자") < text.index("견적서 초안 작성")
    assert "서버 3대, 대당 500만원" in text
    assert "합계 16,500,000원" in text
    assert "홍길동 → 이팀장" in text


def test_각_방은_인계한_시각까지만_싣는다():
    text = build_handover_history(_Sb(), "C", "t")[0]["content"]
    assert "인계 뒤에 쓴 다른 이야기" not in text


def test_인계로_받지_않은_방은_이력이_없다():
    assert build_handover_history(_Sb(), "A", "t") == []
    assert build_handover_history(_Sb(), "C", "") == []


def test_앞선_대화는_새_요청보다_먼저_그래프에_들어간다():
    hist = build_handover_history(_Sb(), "C", "t")
    inputs, resume = build_graph_inputs(
        "[Handover] 견적서 승인",
        human_answer_stripped="",
        run_state={},
        checkpointer=None,
        checkpoint_thread_id="",
        recent_messages=hist,
    )
    assert resume is False
    msgs = inputs["messages"]
    assert msgs[0]["content"] == hist[0]["content"]
    assert msgs[-1]["content"] == "[Handover] 견적서 승인"


def test_너무_길면_오래된_쪽부터_줄인다():
    text = build_handover_history(_Sb(), "C", "t", max_chars=40)[0]["content"]
    assert "앞부분 생략" in text
    assert "16,500,000원" in text


def test_대화창_대화에서도_앞선_대화가_이_방_대화보다_먼저_들어간다():
    """받은 사람이 받은 방에서 에이전트와 대화할 때 — 이 방의 최근 대화 앞에 앞선 방들의 대화가 온다."""
    hist = build_handover_history(_Sb(), "C", "t")
    room_recent = [{"role": "user", "content": "앞선 대화에서 고객이 요청한 수량이 몇 대였지?"}]
    inputs, _ = build_graph_inputs(
        "앞선 대화에서 고객이 요청한 수량이 몇 대였지?",
        human_answer_stripped="",
        run_state={},
        checkpointer=None,
        checkpoint_thread_id="",
        recent_messages=[*hist, *room_recent],
    )
    msgs = inputs["messages"]
    assert "서버 3대" in msgs[0]["content"]
    assert msgs[-1]["content"] == "앞선 대화에서 고객이 요청한 수량이 몇 대였지?"
    assert len(msgs) == 2
