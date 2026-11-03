"""PCの画面（このPCの 127.0.0.1 からだけ使える画面とAPI）。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from landrop import app as app_module
from landrop.app import AppConfig, create_app
from tests.conftest import BASE_URL, PORT, TOKEN

PC_TOKEN = "pc-token-1234567890"
PC_SESSION_KEY = "pc-session-key"
LOCAL_URL = f"http://127.0.0.1:{PORT}"
LOCAL_ORIGIN = {"Origin": LOCAL_URL}

PcFactory = Callable[..., tuple[TestClient, AppConfig]]


@pytest.fixture
def make_pc(make_config: Callable[..., AppConfig]) -> Iterator[PcFactory]:
    clients: list[TestClient] = []

    def factory(authenticated: bool = True, **overrides: object) -> tuple[TestClient, AppConfig]:
        config = make_config(pc_token=PC_TOKEN, pc_session_key=PC_SESSION_KEY, **overrides)
        client = TestClient(create_app(config), base_url=LOCAL_URL, client=("127.0.0.1", 50000))
        clients.append(client)
        if authenticated:
            assert client.get(f"/pc/auth?t={PC_TOKEN}").status_code == 200
        return client, config

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def pc(make_pc: PcFactory) -> TestClient:
    return make_pc()[0]


def _wait_until(condition: Callable[[], bool], seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# ---------- 認証と、ほかの機器からの遮断 ----------


def test_pc_auth_sets_strict_cookie_and_redirects(make_pc: PcFactory) -> None:
    client, _ = make_pc(authenticated=False)

    response = client.get(f"/pc/auth?t={PC_TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/pc"
    cookie = response.headers["set-cookie"]
    assert f"landrop_pc={PC_SESSION_KEY}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/pc" in cookie


def test_wrong_pc_token_is_rejected(make_pc: PcFactory) -> None:
    client, _ = make_pc(authenticated=False)

    response = client.get("/pc/auth?t=wrong", follow_redirects=False)

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_pc_requires_its_own_cookie(make_pc: PcFactory, share_dir: Path) -> None:
    (share_dir / "秘密の資料.pdf").write_bytes(b"x")
    client, _ = make_pc(authenticated=False)

    api = client.get("/pc/api/files/share")
    page = client.get("/pc")

    assert api.status_code == 401
    assert page.status_code == 401
    assert "秘密の資料" not in api.text + page.text


def test_pc_is_not_reachable_from_other_devices(make_config: Callable[..., AppConfig]) -> None:
    config = make_config(pc_token=PC_TOKEN, pc_session_key=PC_SESSION_KEY)
    # スマホなど、LANのアドレスで待ち受けているソケットからの接続
    with TestClient(create_app(config), base_url=BASE_URL, client=("192.168.1.20", 50000)) as lan:
        lan.cookies.set("landrop_pc", PC_SESSION_KEY)
        for path in (f"/pc/auth?t={PC_TOKEN}", "/pc", "/pc/api/status", "/pc/qr.svg"):
            response = lan.get(path, follow_redirects=False)
            assert response.status_code == 404, path
            assert "set-cookie" not in response.headers


def test_phone_and_pc_cookies_are_separate(make_pc: PcFactory) -> None:
    client, _ = make_pc(authenticated=False)
    assert client.get(f"/auth?t={TOKEN}").status_code == 200

    # スマホ用のCookieではPCの画面を使えない
    assert client.get("/pc/api/status").status_code == 401

    other, _ = make_pc()
    # PCの画面のCookieでは、スマホ用のAPIを使えない
    assert other.get("/api/files").status_code == 401


# ---------- 表示 ----------


def test_status_contains_phone_url_and_folders(make_pc: PcFactory) -> None:
    client, config = make_pc()

    body = client.get("/pc/api/status").json()

    assert body["phone_url"] == config.access_url
    assert body["address"] == config.base_url
    assert body["share_dir"] == str(config.share_dir)
    assert body["received_dir"] == str(config.received_dir)
    assert body["remaining_seconds"] is None


def test_qr_code_is_svg_with_white_background(pc: TestClient) -> None:
    response = pc.get("/pc/qr.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")
    assert '<rect width="' in response.text and 'fill="#fff"' in response.text


def test_lists_both_folders_with_preview_flag(pc: TestClient, share_dir: Path, received_dir: Path) -> None:
    (share_dir / "資料.pdf").write_bytes(b"pdf")
    (received_dir / "写真.JPG").write_bytes(b"jpg")
    (received_dir / "動画.mov").write_bytes(b"mov")
    (received_dir / "ページ.html").write_bytes(b"<script>")
    (received_dir / ".受信中.landrop-0123456789abcdef.part").write_bytes(b"part")

    share = pc.get("/pc/api/files/share").json()
    received = pc.get("/pc/api/files/received").json()

    assert [f["name"] for f in share["files"]] == ["資料.pdf"]
    assert "incoming" not in share
    previews = {f["name"]: f["preview"] for f in received["files"]}
    assert previews == {"写真.JPG": True, "動画.mov": True, "ページ.html": False}
    assert received["incoming"] == []
    assert pc.get("/pc/api/files/other").status_code == 404


# ---------- 共有フォルダへの追加 ----------


def test_files_added_on_pc_are_shared_with_the_phone(
    make_pc: PcFactory, make_client: Callable[..., TestClient], share_dir: Path
) -> None:
    client, _ = make_pc()
    name = quote("写真 😀.jpg")

    first = client.post(f"/pc/api/share?name={name}", content=b"one", headers=LOCAL_ORIGIN)
    second = client.post(f"/pc/api/share?name={name}", content=b"two", headers=LOCAL_ORIGIN)

    assert (first.status_code, first.json()) == (201, {"name": "写真 😀.jpg"})
    assert second.json() == {"name": "写真 😀 (1).jpg"}
    assert (share_dir / "写真 😀.jpg").read_bytes() == b"one"
    phone = make_client()
    assert sorted(f["name"] for f in phone.get("/api/files").json()["files"]) == ["写真 😀 (1).jpg", "写真 😀.jpg"]


def test_adding_files_from_other_sites_is_rejected(pc: TestClient, share_dir: Path) -> None:
    response = pc.post("/pc/api/share?name=a.txt", content=b"x", headers={"Origin": "http://evil.example"})

    assert response.status_code == 403
    assert list(share_dir.iterdir()) == []


# ---------- 画像と動画の表示 ----------


def test_preview_is_inline_and_sandboxed(pc: TestClient, received_dir: Path) -> None:
    (received_dir / "写真.png").write_bytes(b"\x89PNG")

    response = pc.get(f"/pc/preview/received/{quote('写真.png')}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" not in response.headers.get("content-disposition", "")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["page.html", "image.svg", "memo.txt", ".hidden.png", "..%2Fsecret.png"])
def test_preview_refuses_other_types_and_paths(pc: TestClient, received_dir: Path, name: str) -> None:
    for existing in ("page.html", "image.svg", "memo.txt", ".hidden.png"):
        (received_dir / existing).write_bytes(b"<svg onload=alert(1)>")
    (received_dir.parent / "secret.png").write_bytes(b"secret")

    assert pc.get(f"/pc/preview/received/{name}").status_code == 404


# ---------- エクスプローラーで表示・フォルダを開く ----------


def test_reveal_and_open_folder_use_only_listed_files(
    pc: TestClient, received_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revealed: list[Path] = []
    opened: list[Path] = []
    monkeypatch.setattr(app_module.desktop, "reveal", revealed.append)
    monkeypatch.setattr(app_module.desktop, "open_folder", opened.append)
    (received_dir / "写真.jpg").write_bytes(b"x")

    ok = pc.post(f"/pc/api/reveal?folder=received&name={quote('写真.jpg')}", headers=LOCAL_ORIGIN)
    missing = pc.post("/pc/api/reveal?folder=received&name=nothing.jpg", headers=LOCAL_ORIGIN)
    outside = pc.post(f"/pc/api/reveal?folder=received&name={quote('../x')}", headers=LOCAL_ORIGIN)
    other_site = pc.post(
        f"/pc/api/reveal?folder=received&name={quote('写真.jpg')}", headers={"Origin": "http://evil.example"}
    )
    folder = pc.post("/pc/api/open-folder?folder=share", headers=LOCAL_ORIGIN)
    bad_folder = pc.post("/pc/api/open-folder?folder=C:/Windows", headers=LOCAL_ORIGIN)

    assert [ok.status_code, missing.status_code, outside.status_code, other_site.status_code] == [204, 404, 404, 403]
    assert revealed == [received_dir / "写真.jpg"]
    assert (folder.status_code, bad_folder.status_code) == (204, 404)
    assert len(opened) == 1


# ---------- 停止 ----------


def test_stop_button_requests_stop(make_pc: PcFactory) -> None:
    client, config = make_pc()

    assert client.post("/pc/api/stop", headers={"Origin": "http://evil.example"}).status_code == 403
    assert config.stop.reason is None
    assert client.post("/pc/api/stop", headers=LOCAL_ORIGIN).status_code == 202
    assert config.stop.reason == "PCの画面から停止しました。"


def _local_scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", f"127.0.0.1:{PORT}".encode()),
            (b"cookie", f"landrop_pc={PC_SESSION_KEY}".encode()),
            (b"x-landrop-auto", b"1"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }


async def _window(app: Callable, opened: asyncio.Event, close: asyncio.Event) -> None:
    """PCの画面1つ分の、つなぎっぱなしの接続。close がセットされたら切断する。"""
    first = True

    async def receive() -> dict:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await close.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            opened.set()

    await app(_local_scope("/pc/api/events"), receive, send)


def _pc_config(make_config: Callable[..., AppConfig], **overrides: object) -> AppConfig:
    return make_config(pc_token=PC_TOKEN, pc_session_key=PC_SESSION_KEY, **overrides)


def test_closing_the_last_window_stops(make_config: Callable[..., AppConfig]) -> None:
    config = _pc_config(make_config, close_grace_seconds=0.05)
    app = create_app(config)

    async def run() -> None:
        opened, close = asyncio.Event(), asyncio.Event()
        window = asyncio.create_task(_window(app, opened, close))
        await opened.wait()
        close.set()
        await window

    asyncio.run(run())

    assert _wait_until(lambda: config.stop.reason is not None)
    assert config.stop.reason == "PCの画面が閉じられたため、停止しました。"


def test_reloading_the_window_does_not_stop(make_config: Callable[..., AppConfig]) -> None:
    config = _pc_config(make_config, close_grace_seconds=0.1)
    app = create_app(config)

    async def run() -> None:
        before = (asyncio.Event(), asyncio.Event())
        task = asyncio.create_task(_window(app, *before))
        await before[0].wait()
        before[1].set()
        await task
        # 再読み込みで開き直された画面
        after = (asyncio.Event(), asyncio.Event())
        task = asyncio.create_task(_window(app, *after))
        await after[0].wait()
        await asyncio.sleep(0.4)
        assert config.stop.reason is None
        after[1].set()
        await task

    asyncio.run(run())

    assert _wait_until(lambda: config.stop.reason is not None)


def test_closing_waits_for_transfers(make_config: Callable[..., AppConfig]) -> None:
    config = _pc_config(make_config, close_grace_seconds=0.05)
    app = create_app(config)
    config.tracker.active_requests = 1  # スマホがダウンロード中

    async def run() -> None:
        opened, close = asyncio.Event(), asyncio.Event()
        window = asyncio.create_task(_window(app, opened, close))
        await opened.wait()
        close.set()
        await window

    asyncio.run(run())
    time.sleep(0.3)
    assert config.stop.reason is None

    config.tracker.active_requests = 0
    assert _wait_until(lambda: config.stop.reason is not None)


def test_stop_ends_the_window_connection(make_config: Callable[..., AppConfig]) -> None:
    config = _pc_config(make_config)
    app = create_app(config)

    async def run() -> None:
        opened, close = asyncio.Event(), asyncio.Event()
        window = asyncio.create_task(_window(app, opened, close))
        await opened.wait()
        config.stop.request("PCの画面から停止しました。")
        # サーバーの終了を待たせないよう、こちらから接続を終える
        await asyncio.wait_for(window, timeout=2)

    asyncio.run(run())


def test_window_connection_is_not_reachable_from_other_devices(make_config: Callable[..., AppConfig]) -> None:
    app = create_app(_pc_config(make_config))
    scope = _local_scope("/pc/api/events")
    scope["client"], scope["server"] = ("192.168.1.20", 50000), ("192.168.1.50", PORT)
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 404
