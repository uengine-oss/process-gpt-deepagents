"""HTTP 스트림: 클라이언트 취소와 분리된 runner 완료(shield) 동작 검증."""

from __future__ import annotations

import asyncio
import unittest

from app.http_chat import await_http_chat_stream_runner


class TestHttpChatStreamRunner(unittest.TestCase):
    def test_await_http_chat_stream_runner_waits_for_work(self) -> None:
        async def main() -> None:
            completed = asyncio.Event()

            async def work() -> None:
                await asyncio.sleep(0.02)
                completed.set()

            t = asyncio.create_task(work())
            await await_http_chat_stream_runner(t)
            assert completed.is_set()

        asyncio.run(main())

    def test_stream_cancelled_but_runner_finishes_via_shield(self) -> None:
        """SSE 제너레이터가 CancelledError로 빠져도 finally에서 shield로 runner가 끝까지 돈다."""

        async def main() -> None:
            runner_done = asyncio.Event()

            async def runner() -> None:
                await asyncio.sleep(0.06)
                runner_done.set()

            async def stream_like() -> None:
                t = asyncio.create_task(runner())
                try:
                    await asyncio.sleep(0.001)
                    raise asyncio.CancelledError()
                finally:
                    await await_http_chat_stream_runner(t)

            with self.assertRaises(asyncio.CancelledError):
                await stream_like()
            self.assertTrue(runner_done.is_set())

        asyncio.run(main())
