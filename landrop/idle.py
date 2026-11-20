from __future__ import annotations

import time
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from landrop.security import AUTHENTICATED_SCOPE_KEY

AUTO_REFRESH_HEADER = "x-landrop-auto"


class IdleTracker:
    def __init__(self, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._last_activity = clock()
        self.active_requests = 0

    @property
    def auto_stop_enabled(self) -> bool:
        return self.timeout_seconds > 0

    def touch(self) -> None:
        self._last_activity = self._clock()

    def is_expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def remaining_seconds(self) -> float | None:
        if not self.auto_stop_enabled:
            return None
        return self.timeout_seconds - (self._clock() - self._last_activity)


class StopRequest:
    def __init__(self) -> None:
        self.reason: str | None = None

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason


class ActivityMiddleware:
    def __init__(self, app: ASGIApp, tracker: IdleTracker) -> None:
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get(AUTHENTICATED_SCOPE_KEY):
            await self.app(scope, receive, send)
            return

        tracker = self.tracker
        is_auto_refresh = (
            Headers(raw=list(scope.get("headers", []))).get(AUTO_REFRESH_HEADER) == "1"
        )
        if is_auto_refresh:
            await self.app(scope, receive, send)
            return

        async def tracked_receive() -> Message:
            message = await receive()
            tracker.touch()
            return message

        async def tracked_send(message: Message) -> None:
            await send(message)
            tracker.touch()

        tracker.touch()
        tracker.active_requests += 1
        try:
            await self.app(scope, tracked_receive, tracked_send)
        finally:
            tracker.active_requests -= 1
            tracker.touch()
