"""Tests for core/chat/chat_room.py incremental context persistence.

Covers the chat-stream-reconnect change's chat-context-incremental-persistence
requirement: context accumulated before a mid-turn exception must not be lost,
because it is written to the DB as each event arrives rather than once at the
very end of the turn.
"""

from unittest.mock import MagicMock, patch

from core.chat.chat_room import (
    merge_chat_room_context_from_event,
    update_chat_room_context,
)


def test_merge_accumulates_across_multiple_events_without_losing_earlier_state():
    ctx = None
    ctx = merge_chat_room_context_from_event(ctx, {"type": "tool_start", "tool": "write_file"})
    assert ctx["tools"] == [{"id": None, "name": "write_file", "tool": "write_file", "status": "running", "displayName": "write_file"}]

    # Simulate a crash happening right here, after event 1 — event 1's contribution
    # must already be reflected in `ctx` (this is exactly what gets persisted before
    # the next event, so a mid-turn exception cannot erase it).
    ctx_after_event_1 = ctx

    ctx = merge_chat_room_context_from_event(ctx, {"type": "plan_skills", "skills": ["bpmn-process-generation-skill"]})
    ctx = merge_chat_room_context_from_event(ctx, {"type": "tool_end", "tool": "write_file"})

    assert ctx_after_event_1["skills"] == []  # skills weren't known yet at that point
    assert ctx["skills"] == ["bpmn-process-generation-skill"]
    assert ctx["tools"][0]["status"] == "done"


class _FakeTable:
    def __init__(self, store: dict):
        self._store = store
        self._select_cols = None
        self._filter_id = None
        self._update_payload = None

    def select(self, cols):
        self._select_cols = cols
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def eq(self, _col, value):
        self._filter_id = value
        return self

    def execute(self):
        if self._update_payload is not None:
            row = self._store.setdefault(self._filter_id, {"id": self._filter_id})
            row.update(self._update_payload)
            return MagicMock(data=[row])
        row = self._store.get(self._filter_id)
        return MagicMock(data=[row] if row else [])


class _FakeSupabase:
    def __init__(self):
        self.rooms: dict[str, dict] = {}

    def table(self, name):
        assert name == "chat_rooms"
        return _FakeTable(self.rooms)


def test_update_chat_room_context_persists_each_event_before_the_next_one_arrives():
    """If the turn raised an exception between these two calls, the first event's
    context would still be durably saved — this is what the incremental persistence
    path guarantees over the old "save once at the very end" behavior."""
    fake_db = _FakeSupabase()
    with patch("core.chat.chat_room.get_supabase", return_value=fake_db):
        update_chat_room_context(
            tenant_id="tenant-1",
            room_id="room-1",
            evt={"type": "tool_start", "tool": "write_file"},
            has_file=False,
        )
        # Simulated crash point: verify event 1 is already durably persisted.
        persisted_after_event_1 = fake_db.rooms["room-1"]["context"]
        assert persisted_after_event_1["tools"][0]["status"] == "running"

        update_chat_room_context(
            tenant_id="tenant-1",
            room_id="room-1",
            evt={"type": "tool_end", "tool": "write_file"},
            has_file=False,
        )
        persisted_after_event_2 = fake_db.rooms["room-1"]["context"]
        assert persisted_after_event_2["tools"][0]["status"] == "done"
