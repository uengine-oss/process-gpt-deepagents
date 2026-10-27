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
    def __init__(self, table, data):
        self.table = table
        self.events, self.chats, self.rooms = data
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
            r.data = [e for e in self.events if all(e.get(k) == v for k, v in self.filters.items())]
        elif self.table == "chats":
            r.data = [{"uuid": str(i), "messages": m} for i, m in enumerate(self.chats.get(self.filters.get("id"), []))]
        else:
            k, vs = self.in_filter
            r.data = [x for x in self.rooms if x[k] in vs]
        return r


class _Sb:
    def __init__(self, events=None, chats=None, rooms=None):
        self.data = (EVENTS if events is None else events, CHATS if chats is None else chats, ROOMS if rooms is None else rooms)

    def table(self, name):
        return _Query(name, self.data)


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


# 합류(2026-10-27) — 홍길동이 생산 계획(P)을 1공장(이팀장)·2공장(박대리)에게 나눠 준 뒤,
# 같은 방에서 결과를 모아 최과장(J)에게 넘긴다. 2공장은 중간에 김주임(C2)에게 한 번 더 넘겼다.
G = "g-1"
MERGE_EVENTS = [
    {"tenant_id": "t", "from_room_id": "P", "to_room_id": "B1", "occurred_at": "2026-10-27T01:00:00+00:00",
     "from_user_name": "홍길동", "to_user_name": "이팀장", "group_id": G, "next_activity_label": "1공장 생산"},
    {"tenant_id": "t", "from_room_id": "P", "to_room_id": "B2", "occurred_at": "2026-10-27T01:00:00.200+00:00",
     "from_user_name": "홍길동", "to_user_name": "박대리", "group_id": G, "next_activity_label": "2공장 생산"},
    {"tenant_id": "t", "from_room_id": "B2", "to_room_id": "C2", "occurred_at": "2026-10-27T02:00:00+00:00",
     "from_user_name": "박대리", "to_user_name": "김주임", "group_id": None, "next_activity_label": "2공장 검수"},
    {"tenant_id": "t", "from_room_id": "P", "to_room_id": "J", "occurred_at": "2026-10-27T03:00:00+00:00",
     "from_user_name": "홍길동", "to_user_name": "최과장", "group_id": None, "next_activity_label": "생산 결과 보고"},
]
MERGE_CHATS = {
    "P": [{"role": "user", "name": "홍길동", "content": "이번 달 생산 계획을 공장별로 나눠 진행", "timeStamp": "2026-10-27T00:50:00+00:00"}],
    "B1": [
        {"role": "assistant", "name": "이팀장", "content": "1공장 생산 수량 1,200대 확정", "timeStamp": "2026-10-27T01:30:00+00:00"},
        {"role": "user", "name": "이팀장", "content": "합류 뒤에 쓴 1공장 메모", "timeStamp": "2026-10-27T04:00:00+00:00"},
    ],
    "B2": [{"role": "assistant", "name": "박대리", "content": "2공장 생산 수량 850대", "timeStamp": "2026-10-27T01:40:00+00:00"}],
    "C2": [{"role": "assistant", "name": "김주임", "content": "2공장 검수 불량 3대", "timeStamp": "2026-10-27T02:30:00+00:00"}],
}
MERGE_ROOMS = [
    {"id": "P", "name": "생산 계획"}, {"id": "B1", "name": "1공장 생산"},
    {"id": "B2", "name": "2공장 생산"}, {"id": "C2", "name": "2공장 검수"},
]


def _merge_sb():
    return _Sb(MERGE_EVENTS, MERGE_CHATS, MERGE_ROOMS)


def test_합류한_방은_모든_갈래의_대화를_갈래별로_받는다():
    text = build_handover_history(_merge_sb(), "J", "t")[0]["content"]
    assert "공장별로 나눠 진행" in text
    assert "갈래 1: 이팀장 · 1공장 생산" in text and "갈래 2: 박대리 · 2공장 생산" in text
    assert "1,200대" in text and "850대" in text
    # 시작 방 → 1공장 갈래 → 2공장 갈래
    assert text.index("생산 계획") < text.index("1,200대") < text.index("850대")


def test_갈래_안에서_이어_넘긴_방도_그_갈래에_들어간다():
    text = build_handover_history(_merge_sb(), "J", "t")[0]["content"]
    assert "불량 3대" in text
    assert text.index("갈래 2") < text.index("불량 3대")


def test_갈래_방은_합류_시각까지만_싣는다():
    text = build_handover_history(_merge_sb(), "J", "t")[0]["content"]
    assert "합류 뒤에 쓴 1공장 메모" not in text


def test_갈래_방은_형제_갈래를_받지_않는다():
    text = build_handover_history(_merge_sb(), "B1", "t")[0]["content"]
    assert "공장별로 나눠 진행" in text
    assert "850대" not in text and "갈래" not in text.split("\n\n", 1)[1]


def test_나눠_주기_전에_넘긴_인계는_합류가_아니다():
    """나눠 준 뒤에 일어난 인계만 합류다. 나눠 주기 전 인계의 받은 방은 갈래를 받지 않는다."""
    events = [
        {"tenant_id": "t", "from_room_id": "P", "to_room_id": "E", "occurred_at": "2026-10-27T00:55:00+00:00",
         "from_user_name": "홍길동", "to_user_name": "정사원", "group_id": None, "next_activity_label": "자재 확인"},
        *MERGE_EVENTS,
    ]
    text = build_handover_history(_Sb(events, MERGE_CHATS, MERGE_ROOMS), "E", "t")[0]["content"]
    assert "1,200대" not in text and "갈래 1" not in text
