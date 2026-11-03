"""無操作が続いたときの自動停止の判定。"""

from __future__ import annotations

import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from landrop.security import AUTHENTICATED_SCOPE_KEY


class IdleTracker:
    """最後に通信があった時刻を記録し、自動停止すべきかを判定する。

    転送中はデータを送受信するたびに記録が更新されるため、自動停止しない。
    """

    def __init__(self, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        # 0以下なら自動停止しない
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._last_activity = clock()

    def touch(self) -> None:
        self._last_activity = self._clock()

    def is_expired(self) -> bool:
        if self.timeout_seconds <= 0:
            return False
        return self._clock() - self._last_activity >= self.timeout_seconds


class ActivityMiddleware:
    """認証済みのリクエストの送受信を、操作があったとして記録する。"""

    def __init__(self, app: ASGIApp, tracker: IdleTracker) -> None:
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get(AUTHENTICATED_SCOPE_KEY):
            await self.app(scope, receive, send)
            return

        tracker = self.tracker

        async def tracked_receive() -> Message:
            message = await receive()
            tracker.touch()
            return message

        async def tracked_send(message: Message) -> None:
            await send(message)
            tracker.touch()

        tracker.touch()
        try:
            await self.app(scope, tracked_receive, tracked_send)
        finally:
            tracker.touch()
