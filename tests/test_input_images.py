"""채팅 첨부 이미지를 [InputData] 글에서 떼어 모델 이미지 입력으로 옮기는 동작."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from core.chat import input_images as ii
from core.agents.input_builder import build_graph_inputs

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
DATA_URL = f"data:image/png;base64,{PNG}"


def _msg(input_data: dict, text: str = "첨부한 이미지를 프로세스로 만들어줘") -> str:
    return f"{text}\n\n[InputData]\n{json.dumps(input_data, ensure_ascii=False)}"


def test_split_removes_images_and_keeps_other_input_data():
    text = _msg({"images": [{"id": "1", "url": DATA_URL, "file": {}}], "files": [{"fileName": "a.pdf"}]})
    rest, refs = ii.split_input_images(text)
    assert refs == [DATA_URL]
    assert "base64" not in rest
    assert rest.startswith("첨부한 이미지를 프로세스로 만들어줘\n\n[InputData]\n")
    assert json.loads(rest.split("[InputData]\n", 1)[1]) == {"files": [{"fileName": "a.pdf"}]}


def test_split_drops_block_when_only_images():
    rest, refs = ii.split_input_images(_msg({"images": [DATA_URL]}))
    assert refs == [DATA_URL]
    assert rest == "첨부한 이미지를 프로세스로 만들어줘"


def test_split_keeps_following_blocks():
    text = _msg({"images": [{"url": DATA_URL}]}) + "\n\n[OpenUIForm]\n{}"
    rest, _ = ii.split_input_images(text)
    assert rest == "첨부한 이미지를 프로세스로 만들어줘\n\n[OpenUIForm]\n{}"


@pytest.mark.parametrize("text", ["그냥 글", _msg({"files": []}), "x\n\n[InputData]\n{깨진"])
def test_split_leaves_text_without_images(text):
    assert ii.split_input_images(text) == (text, [])


def test_resolve_downloads_remote_images(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("ok.png"):
            return httpx.Response(200, content=b"img", headers={"content-type": "image/png"})
        if request.url.path.endswith("html"):
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(400)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    refs = [DATA_URL, "https://s/sign/chat-images/ok.png?token=t", "https://s/x.html", "https://s/gone.png", "uploads/x.png"]
    resolved, failed = asyncio.run(ii.resolve_images(refs))
    assert resolved == [DATA_URL, f"data:image/png;base64,{base64.b64encode(b'img').decode()}"]
    assert failed == 3


def test_note_mentions_failures():
    assert "1장" in ii.images_note(1, 0)
    note = ii.images_note(0, 2)
    assert "2장은 불러오지 못했습니다" in note


def _graph_inputs(**kw):
    base = dict(human_answer_stripped="", run_state={}, checkpointer=None, checkpoint_thread_id="")
    base.update(kw)
    return build_graph_inputs("본문", **base)


def test_graph_inputs_attach_image_blocks():
    inputs, resume = _graph_inputs(images=[DATA_URL])
    assert resume is False
    content = inputs["messages"][-1]["content"]
    assert content == [
        {"type": "text", "text": "본문"},
        {"type": "image_url", "image_url": {"url": DATA_URL}},
    ]


def test_graph_inputs_with_checkpoint_and_duplicate_history():
    inputs, _ = _graph_inputs(images=[DATA_URL], has_checkpoint=True)
    assert inputs["messages"][0]["content"][1]["type"] == "image_url"

    history = [{"role": "assistant", "content": "안녕"}, {"role": "user", "content": "본문"}]
    inputs, _ = _graph_inputs(images=[DATA_URL], recent_messages=history)
    assert len(inputs["messages"]) == 2
    assert inputs["messages"][-1]["content"][1]["image_url"]["url"] == DATA_URL


def test_graph_inputs_without_images_stay_text():
    inputs, _ = _graph_inputs()
    assert inputs["messages"][-1]["content"] == "본문"
