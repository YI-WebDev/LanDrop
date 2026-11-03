"""無操作が続いたときの自動停止の判定と、停止の依頼。"""

from __future__ import annotations

import time
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from landrop.security import AUTHENTICATED_SCOPE_KEY

# 画面が一定間隔で行う一覧の自動更新に付けるヘッダー。操作とは数えない
AUTO_REFRESH_HEADER = "x-landrop-auto"


class IdleTracker:
    """最後に通信があった時刻を記録し、自動停止すべきかを判定する。

    転送中はデータを送受信するたびに記録が更新されるため、自動停止しない。
    """

    def __init__(self, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        # 0以下なら自動停止しない
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._last_activity = clock()
        # 処理中のリクエストの数（ダウンロードなど、時間のかかる転送が続いているかの判断に使う）
        self.active_requests = 0

    def touch(self) -> None:
        self._last_activity = self._clock()

    def is_expired(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def remaining_seconds(self) -> float | None:
        """自動停止までの残り秒数。自動停止しない設定なら None。"""
        if self.timeout_seconds <= 0:
            return None
        return self.timeout_seconds - (self._clock() - self._last_activity)


class StopRequest:
    """PCの画面からの停止の依頼。監視しているスレッドが、理由を表示してサーバーを止める。"""

    def __init__(self) -> None:
        self.reason: str | None = None

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason


class PcPages:
    """開いているPCの画面を、画面ごとのIDで把握する。

    ブラウザは隠れた窓の定期処理を1分に1回程度まで間引くため、
    それより長く連絡のない画面は閉じられたものとみなす。
    """

    ALIVE_SECONDS = 75

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_seen: dict[str, float] = {}
        self._closed: set[str] = set()

    def seen(self, page_id: str) -> None:
        if page_id and page_id not in self._closed:
            self._last_seen[page_id] = self._clock()

    def closed(self, page_id: str) -> None:
        self._closed.add(page_id)
        self._last_seen.pop(page_id, None)

    def any_open(self) -> bool:
        now = self._clock()
        return any(now - seen < self.ALIVE_SECONDS for seen in self._last_seen.values())


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
        if Headers(raw=list(scope.get("headers", []))).get(AUTO_REFRESH_HEADER) == "1":
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
