"""無操作による自動停止の判定。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi.testclient import TestClient

from landrop.idle import ActivityMiddleware, IdleTracker, StopRequest
from landrop.security import AUTHENTICATED_SCOPE_KEY
from tests.conftest import FakeClock


def test_expires_after_timeout_without_activity() -> None:
    clock = FakeClock()
    tracker = IdleTracker(30 * 60, clock=clock)

    clock.advance(30 * 60 - 1)
    assert not tracker.is_expired()
    clock.advance(1)
    assert tracker.is_expired()


def test_activity_resets_the_timer() -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)

    clock.advance(59)
    tracker.touch()
    clock.advance(59)
    assert not tracker.is_expired()
    clock.advance(1)
    assert tracker.is_expired()


def test_zero_timeout_never_expires() -> None:
    clock = FakeClock()
    tracker = IdleTracker(0, clock=clock)

    clock.advance(10**9)
    assert not tracker.is_expired()


def _run_transfer(tracker: IdleTracker, clock: FakeClock, chunks: int, seconds_per_chunk: float) -> list[bool]:
    """1チャンクごとに時間が進む長い転送を ActivityMiddleware 越しに流し、
    次のデータを待っている間に自動停止の判定がどうなるかを記録する。"""
    observed: list[bool] = []

    async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    messages = [{"type": "http.request", "body": b"x", "more_body": i < chunks - 1} for i in range(chunks)]

    async def receive() -> dict:
        clock.advance(seconds_per_chunk)
        # 監視スレッドが、データの到着を待っている最中に判定した場合に相当する
        observed.append(tracker.is_expired())
        return messages.pop(0)

    async def send(message: dict) -> None:
        pass

    scope = {"type": "http", "path": "/api/upload", AUTHENTICATED_SCOPE_KEY: True}
    asyncio.run(ActivityMiddleware(app, tracker)(scope, receive, send))
    return observed


def test_does_not_expire_while_transferring() -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)

    # 合計でタイムアウトの10倍の時間がかかる転送でも、データが流れている間は止めない
    observed = _run_transfer(tracker, clock, chunks=20, seconds_per_chunk=30)

    assert observed == [False] * 20
    assert not tracker.is_expired()
    clock.advance(60)
    assert tracker.is_expired()


def test_stalled_transfer_counts_as_idle() -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)

    # データが途絶えたまま放置された接続では、停止を妨げない
    observed = _run_transfer(tracker, clock, chunks=2, seconds_per_chunk=61)

    assert observed[-1] is True


def test_unauthenticated_requests_do_not_keep_server_alive(make_client: Callable[..., TestClient]) -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)
    anon = make_client(authenticated=False, tracker=tracker)
    clock.advance(61)

    for path in ("/", "/api/files", "/auth?t=wrong", "/static/ui.css"):
        anon.get(path)
    assert tracker.is_expired()

    authed = make_client(tracker=tracker)
    authed.get("/api/files")
    assert not tracker.is_expired()


def test_auto_refresh_does_not_count_as_activity(make_client: Callable[..., TestClient]) -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)
    client = make_client(tracker=tracker)
    clock.advance(61)

    # 画面を開いたままにしているだけの自動更新では、自動停止を妨げない
    client.get("/api/files", headers={"X-LanDrop-Auto": "1"})
    assert tracker.is_expired()

    client.get("/api/files")
    assert not tracker.is_expired()


def test_remaining_seconds() -> None:
    clock = FakeClock()
    tracker = IdleTracker(60, clock=clock)

    clock.advance(20)
    assert tracker.remaining_seconds() == 40
    assert IdleTracker(0, clock=clock).remaining_seconds() is None


def test_active_requests_are_counted_during_transfer() -> None:
    tracker = IdleTracker(60)
    seen: list[int] = []

    async def app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        seen.append(tracker.active_requests)

    async def noop(*_: object) -> None:
        pass

    scope = {"type": "http", "path": "/api/files/a", AUTHENTICATED_SCOPE_KEY: True}
    asyncio.run(ActivityMiddleware(app, tracker)(scope, noop, noop))

    assert seen == [1]
    assert tracker.active_requests == 0


def test_stop_request_keeps_the_first_reason() -> None:
    stop = StopRequest()
    stop.request("1つ目")
    stop.request("2つ目")

    assert stop.reason == "1つ目"
