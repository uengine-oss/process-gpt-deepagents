from __future__ import annotations

from types import SimpleNamespace

from app.http_chat import ChatRequest, _build_http_stream_terminal_events


def _mk_request(message: str) -> ChatRequest:
    return ChatRequest(
        tenant_id="t1",
        user_uid="u1",
        user_name="soon",
        user_email="soon@example.com",
        message=message,
    )


def test_done_fallback_never_returns_raw_user_message() -> None:
    req = _mk_request("사용자 비밀 요청 원문")
    resp = SimpleNamespace(response="")
    _, done_evt = _build_http_stream_terminal_events(
        conv_id="c1",
        request=req,
        outcome={"response": resp},
        token_count=0,
        tool_event_count=0,
        streamed_tool_events=[],
        streamed_token_parts=["사용자 비밀 요청 원문"],
        token_preview_parts=[],
    )
    assert isinstance(done_evt, dict)
    content = str(done_evt.get("content") or "")
    assert "사용자 비밀 요청 원문" not in content
    assert "요청 작업을 수행했지만 최종 답변을 생성하지 못했습니다." in content


def test_error_done_redacts_user_message() -> None:
    req = _mk_request("원문 메시지")
    _, done_evt = _build_http_stream_terminal_events(
        conv_id="c2",
        request=req,
        outcome={"error": "실패: 원문 메시지"},
        token_count=0,
        tool_event_count=0,
        streamed_tool_events=[],
        streamed_token_parts=[],
        token_preview_parts=[],
    )
    assert isinstance(done_evt, dict)
    content = str(done_evt.get("content") or "")
    assert "원문 메시지" not in content
    assert "[REDACTED_USER_MESSAGE]" in content

