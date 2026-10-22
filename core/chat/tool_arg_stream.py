"""core/chat/tool_arg_stream.py — 스트리밍되는 tool-call 인자에서 텍스트를 점진적으로 뽑는다.

`request_human_input` 처럼 **사용자에게 보여줄 본문이 도구 인자 안에** 들어가는 경우,
모델은 그 인자 JSON 을 수백 개의 델타로 나눠 스트리밍한다. 그런데 어시스턴트의
`content` 는 비어 있으므로, content 만 중계하면 화면에는 몇 분 동안 "생각 중..." 만
떠 있다가 결과가 한 번에 나타난다.

이 모듈은 아직 닫히지 않은(=부분적인) JSON 조각에서 지정한 문자열 필드의 값을
**지금까지 확정된 만큼만** 디코드해, 새로 늘어난 부분을 델타로 돌려준다.
불완전한 이스케이프(`\\` 로 끝나거나 `\\u12` 처럼 잘린 경우)는 다음 델타를 기다린다 —
깨진 문자를 화면에 내보내지 않기 위해서다.
"""
from __future__ import annotations

import re

_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _decode_json_string_at(buf: str, start: int) -> tuple[str, bool]:
    """`buf[start]` 가 여는 따옴표일 때, 확정된 문자열 접두사와 종료 여부를 돌려준다.

    Returns:
        (decoded_prefix, closed) — `closed` 는 닫는 따옴표까지 도달했는지.
    """
    out: list[str] = []
    i = start + 1
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch == '"':
            return "".join(out), True
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        # 이스케이프 — 뒤가 아직 안 왔으면 여기서 멈춘다(잘린 문자 방출 방지).
        if i + 1 >= n:
            break
        esc = buf[i + 1]
        if esc == "u":
            if i + 5 >= n:
                break
            hex4 = buf[i + 2 : i + 6]
            if len(hex4) < 4:
                break
            try:
                out.append(chr(int(hex4, 16)))
            except ValueError:
                out.append(hex4)
            i += 6
            continue
        out.append(_ESCAPES.get(esc, esc))
        i += 2
    return "".join(out), False


class ToolArgTextStreamer:
    """한 tool-call 의 인자 델타를 먹여주면, 보여줄 텍스트의 증가분만 돌려준다.

    `keys` 에 준 순서대로 필드를 이어 붙인다(예: question → context).
    앞 필드가 아직 닫히지 않았으면 뒤 필드는 시작하지 않는다 — 순서가 뒤집혀 보이지
    않게 하기 위해서다.
    """

    def __init__(self, keys: tuple[str, ...] = ("question", "context"), separator: str = "\n\n"):
        self._keys = keys
        self._separator = separator
        self._buf = ""
        self._emitted = 0

    def feed(self, delta: str) -> str:
        """인자 델타를 넣고, 이번에 새로 확정된 텍스트만 돌려준다(없으면 빈 문자열)."""
        if not delta:
            return ""
        self._buf += delta
        text = self._render()
        if len(text) <= self._emitted:
            return ""
        out = text[self._emitted :]
        self._emitted = len(text)
        return out

    def _render(self) -> str:
        parts: list[str] = []
        for key in self._keys:
            match = re.search(rf'"{re.escape(key)}"\s*:\s*"', self._buf)
            if match is None:
                break
            value, closed = _decode_json_string_at(self._buf, match.end() - 1)
            if value:
                parts.append(value)
            if not closed:
                break  # 아직 쓰는 중 — 다음 필드로 넘어가지 않는다
        return self._separator.join(p for p in parts if p)
