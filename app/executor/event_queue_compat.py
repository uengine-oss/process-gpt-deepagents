"""Compatibility adapters for a2a-sdk EventQueue changes.

In a2a-sdk>=1.0, EventQueue.enqueue_event is async.
Our executor/tooling code often calls enqueue_event from sync code paths
(including worker threads). This adapter provides a sync enqueue_event()
that safely schedules the async enqueue onto the main asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _AsyncEnqueueEventQueue(Protocol):
    async def enqueue_event(self, event: Any) -> None: ...

    def task_done(self) -> None: ...


class ThreadsafeSyncEnqueueAdapter:
    """Expose a sync enqueue_event() for an async EventQueue implementation."""

    def __init__(
        self,
        inner: _AsyncEnqueueEventQueue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._inner = inner
        self._loop = loop

    def enqueue_event(self, event: Any) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self._loop:
            # We're already on the loop thread.
            asyncio.create_task(self._inner.enqueue_event(event))
            return

        def _call() -> None:
            try:
                asyncio.create_task(self._inner.enqueue_event(event))
            except Exception:
                logger.exception("❌ enqueue_event scheduling failed", exc_info=True)

        try:
            self._loop.call_soon_threadsafe(_call)
        except Exception:
            logger.exception("❌ call_soon_threadsafe failed", exc_info=True)

    def task_done(self) -> None:
        self._inner.task_done()

