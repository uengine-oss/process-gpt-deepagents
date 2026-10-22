import json

from core.chat.tool_arg_stream import ToolArgTextStreamer


def _feed_char_by_char(payload: str, **kwargs) -> str:
    s = ToolArgTextStreamer(**kwargs)
    return "".join(s.feed(ch) for ch in payload)


def test_streams_question_then_context_in_order():
    args = json.dumps(
        {"question": "이대로 진행할까요?", "context": "1) 신청\n2) 승인"}, ensure_ascii=False
    )
    assert _feed_char_by_char(args) == "이대로 진행할까요?\n\n1) 신청\n2) 승인"


def test_emits_incrementally_while_value_is_still_open():
    s = ToolArgTextStreamer()
    assert s.feed('{"question":"이대로') == "이대로"
    assert s.feed(' 진행할까요?"') == " 진행할까요?"
    # question 이 닫히기 전에는 context 로 넘어가지 않는다
    assert s.feed(',"context":"초안') == "\n\n초안"


def test_never_emits_partial_escape_sequences():
    s = ToolArgTextStreamer()
    s.feed('{"question":"a')
    # 백슬래시만 온 상태 — 다음 글자를 기다려야 한다
    assert s.feed("\\") == ""
    assert s.feed("n") == "\n"
    # \\uXXXX 도 4자리가 다 올 때까지 참는다
    assert s.feed("\\u00") == ""
    assert s.feed("41") == "A"


def test_escaped_quote_does_not_close_the_string():
    args = r'{"question":"\"승인\" 하시겠어요?","context":"끝"}'
    assert _feed_char_by_char(args) == '"승인" 하시겠어요?\n\n끝'


def test_no_output_for_unrelated_arguments():
    s = ToolArgTextStreamer()
    assert s.feed('{"file_path":"/workspace/a.json","content":"{}"}') == ""


def test_chunked_delivery_matches_whole_delivery():
    args = json.dumps(
        {"question": "질문", "context": "여러 줄\n본문\t탭 포함"}, ensure_ascii=False
    )
    whole = ToolArgTextStreamer().feed(args)
    chunked = "".join(
        (lambda s: [s.feed(args[i : i + 7]) for i in range(0, len(args), 7)])(
            ToolArgTextStreamer()
        )
    )
    assert chunked == whole == "질문\n\n여러 줄\n본문\t탭 포함"
