"""todolist 폴링을 이 프로세스가 맡을지.

세션당 파드 배포에서는 꺼야 한다. 켜 두면 대화마다 뜨는 파드가 전부 워크아이템
소비자가 되어, 유휴 TTL 로 회수될 파드가 남의 일을 집어간다 — 그 파드가 사라지면
집어간 일도 같이 멎는다. 증상이 조용해서(누가 집어갔는지 로그에만 남는다) 배포
전에 여기서 잡아야 한다.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _polling_enabled(monkeypatch, value: str | None) -> bool:
    if value is None:
        monkeypatch.delenv("DEEPAGENTS_PROCESS_POLLING", raising=False)
    else:
        monkeypatch.setenv("DEEPAGENTS_PROCESS_POLLING", value)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    try:
        return module.PROCESS_POLLING
    finally:
        sys.modules.pop("server", None)


def test_기본값은_켬이다(monkeypatch):
    """지금 배포(단일 워크로드가 채팅과 폴링을 겸한다)의 동작을 그대로 둔다."""
    assert _polling_enabled(monkeypatch, None) is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "NO"])
def test_끄는_값들(monkeypatch, value):
    assert _polling_enabled(monkeypatch, value) is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", ""])
def test_그_밖의_값은_켬으로_본다(monkeypatch, value):
    """애매하면 켜 둔다 — 실수로 꺼져서 아무도 todolist 를 안 집어가는 쪽이
    중복 소비보다 알아채기 어렵다."""
    assert _polling_enabled(monkeypatch, value) is True
