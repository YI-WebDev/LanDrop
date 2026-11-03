"""テスト共通の準備。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from landrop.app import AppConfig, create_app
from landrop.idle import IdleTracker

HOST_IP = "192.168.1.50"
PORT = 8765
TOKEN = "test-token-1234567890"
SESSION_KEY = "test-session-key"
BASE_URL = f"http://{HOST_IP}:{PORT}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def share_dir(tmp_path: Path) -> Path:
    path = tmp_path / "share"
    path.mkdir()
    return path


@pytest.fixture
def received_dir(tmp_path: Path) -> Path:
    path = tmp_path / "received"
    path.mkdir()
    return path


@pytest.fixture
def make_config(share_dir: Path, received_dir: Path) -> Callable[..., AppConfig]:
    def factory(**overrides: object) -> AppConfig:
        values: dict[str, object] = {
            "share_dir": share_dir,
            "received_dir": received_dir,
            "host_ip": HOST_IP,
            "port": PORT,
            "max_size": 10 * 1024 * 1024,
            "pc_name": "TEST-PC",
            "tracker": IdleTracker(0),
            "token": TOKEN,
            "session_key": SESSION_KEY,
        }
        values.update(overrides)
        return AppConfig(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def make_client(make_config: Callable[..., AppConfig]) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(authenticated: bool = True, **overrides: object) -> TestClient:
        client = TestClient(create_app(make_config(**overrides)), base_url=BASE_URL)
        clients.append(client)
        if authenticated:
            response = client.get(f"/auth?t={TOKEN}")
            assert response.status_code == 200
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def anon_client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(authenticated=False)


def asgi_request(
    app: Callable,
    path: str,
    *,
    method: str = "POST",
    query: str = "",
    headers: dict[str, str] | None = None,
    messages: list[dict] | None = None,
    on_receive: Callable[[int], None] | None = None,
) -> list[dict]:
    """ASGIアプリを直接呼び出す（途中切断など、HTTPクライアントでは再現しにくい状況用）。"""
    all_headers = {"host": f"{HOST_IP}:{PORT}", "cookie": f"landrop_session={SESSION_KEY}"}
    all_headers.update(headers or {})
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in all_headers.items()],
        "client": ("192.168.1.20", 50000),
        "server": (HOST_IP, PORT),
    }
    queue = list(messages or [])
    sent: list[dict] = []
    calls = 0

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        if on_receive:
            on_receive(calls)
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent
