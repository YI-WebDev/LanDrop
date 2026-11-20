"""一度接続したスマホを覚えて、次の起動からQRコードなしで通す仕組み。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from landrop import cli, mdns
from landrop.app import AppConfig, create_app
from landrop.devices import (
    ALREADY_REMEMBERED,
    DEVICE_COOKIE_NAME,
    DEVICE_TTL_SECONDS,
    HANDOFF_TTL_SECONDS,
    MAX_DEVICES,
    MAX_KEYS_PER_DEVICE,
    DeviceStore,
    device_label,
)
from tests.conftest import BASE_URL, PORT, TOKEN, FakeClock

LOCAL_NAME = "landrop-test-pc.local"
LOCAL_BASE = f"http://{LOCAL_NAME}:{PORT}"
PC_TOKEN = "pc-token-1234567890"
PC_SESSION_KEY = "pc-session-key"
IPHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"


# ---------- 端末の一覧（DeviceStore） ----------


def test_register_and_authenticate(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "devices.json")
    secret = store.register("iPhone")

    device_id = store.authenticate(secret)
    assert device_id is not None
    assert store.authenticate("wrong") is None
    assert store.authenticate("") is None
    assert [d["label"] for d in store.list()] == ["iPhone"]


def test_devices_survive_restart_and_file_has_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    secret = DeviceStore(path).register("Android")

    assert secret not in path.read_text(encoding="utf-8")
    assert DeviceStore(path).authenticate(secret) is not None


def test_unused_device_expires_but_used_device_stays(tmp_path: Path) -> None:
    clock = FakeClock()
    store = DeviceStore(tmp_path / "devices.json", clock=clock)
    used, unused = store.register("iPhone"), store.register("Android")

    # 使い続けている端末は、最初の登録から30日を過ぎても忘れない
    for _ in range(4):
        clock.advance(DEVICE_TTL_SECONDS / 2)
        assert store.authenticate(used) is not None
    assert store.authenticate(unused) is None
    assert [d["label"] for d in store.list()] == ["iPhone"]
    # 起動し直しても、期限切れの端末は読み込まない
    clock.advance(DEVICE_TTL_SECONDS)
    assert DeviceStore(tmp_path / "devices.json", clock=clock).list() == []


def test_oldest_device_is_dropped_over_the_limit(tmp_path: Path) -> None:
    clock = FakeClock()
    store = DeviceStore(None, clock=clock)
    first = store.register("最初")
    for _ in range(MAX_DEVICES):
        clock.advance(1)
        store.register("端末")

    assert len(store.list()) == MAX_DEVICES
    assert store.authenticate(first) is None


def test_forget_removes_every_key_of_the_device(tmp_path: Path) -> None:
    store = DeviceStore(tmp_path / "devices.json")
    secret = store.register("iPhone")
    device_id = store.authenticate(secret)
    assert device_id is not None
    other = store.redeem_handoff(store.create_handoff(device_id) or "")
    assert other is not None

    assert store.forget(device_id)
    assert store.authenticate(secret) is None
    assert store.authenticate(other) is None
    assert not store.forget(device_id)
    assert DeviceStore(tmp_path / "devices.json").list() == []


def test_handoff_is_one_time_and_expires(tmp_path: Path) -> None:
    clock = FakeClock()
    store = DeviceStore(None, clock=clock)
    device_id = store.authenticate(store.register("iPhone"))
    assert device_id is not None

    token = store.create_handoff(device_id)
    assert token is not None
    new_secret = store.redeem_handoff(token)
    assert new_secret is not None
    assert store.authenticate(new_secret) == device_id
    assert store.redeem_handoff(token) is None

    late = store.create_handoff(device_id)
    clock.advance(HANDOFF_TTL_SECONDS + 1)
    assert store.redeem_handoff(late or "") is None
    assert store.create_handoff("no-such-device") is None
    # 端末の一覧は増えない（同じ端末にアドレスごとの鍵が増えるだけ）
    assert len(store.list()) == 1


def test_keys_per_device_are_capped_by_least_recent_use() -> None:
    store = DeviceStore(None)
    first = store.register("iPhone")
    device_id = store.authenticate(first)
    assert device_id is not None
    keys = [store.redeem_handoff(store.create_handoff(device_id) or "") for _ in range(MAX_KEYS_PER_DEVICE)]

    # 誰も使っていない最初の鍵が消える
    assert store.authenticate(first) is None
    assert all(store.authenticate(k or "") == device_id for k in keys)


def test_key_in_use_survives_repeated_handoffs() -> None:
    # IPアドレスのページをホーム画面に置き、開くたびに名前のアドレスへ移るような使い方
    store = DeviceStore(None)
    ip_key = store.register("iPhone")
    for _ in range(MAX_KEYS_PER_DEVICE * 3):
        device_id = store.authenticate(ip_key)
        assert device_id is not None
        assert store.redeem_handoff(store.create_handoff(device_id) or "")


def test_handoff_to_an_address_that_already_has_the_key_adds_nothing() -> None:
    store = DeviceStore(None)
    ip_key = store.register("iPhone")
    device_id = store.authenticate(ip_key)
    assert device_id is not None
    local_key = store.redeem_handoff(store.create_handoff(device_id) or "")
    assert local_key

    for _ in range(MAX_KEYS_PER_DEVICE * 2):
        token = store.create_handoff(device_id) or ""
        assert store.redeem_handoff(token, current_device=device_id) == ALREADY_REMEMBERED
        # 使い捨ての鍵は、このときも使い切る
        assert store.redeem_handoff(token, current_device=device_id) is None
    assert store.authenticate(ip_key) == device_id
    assert store.authenticate(local_key) == device_id
    # 別の端末の鍵を持っていても、その端末として扱わない（新しい鍵を渡す）
    other = store.authenticate(store.register("Android"))
    assert store.redeem_handoff(store.create_handoff(device_id) or "", current_device=other)


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        '{"devices": [{"id": 1}]}',
        "[]",
        '{"devices": 3}',
        '{"devices": [{"id":"a","label":"x","created":0,"last_seen":"x","key_hashes":[]}]}',
        '{"devices": [{"id":"a","label":"x","created":0,"last_seen":1e18,"key_hashes":[1]}]}',
        '{"devices": [{"id":"a","label":"x","created":true,"last_seen":1e18,"key_hashes":[]}]}',
        '{"devices": [{"id":"a","label":null,"created":0,"last_seen":1e18,"key_hashes":[]}]}',
        '{"devices": ["a", null]}',
    ],
)
def test_broken_file_starts_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "devices.json"
    path.write_text(content, encoding="utf-8")

    store = DeviceStore(path)
    assert store.list() == []
    # 壊れたファイルは、次の登録で書き直される
    secret = store.register("iPhone")
    assert DeviceStore(path).authenticate(secret) is not None


def test_unreadable_entries_are_skipped_but_valid_ones_kept(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    secret = DeviceStore(path).register("iPhone")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["devices"].append({"id": "b", "label": "x", "created": 0, "last_seen": "x", "key_hashes": []})
    path.write_text(json.dumps(data), encoding="utf-8")

    assert DeviceStore(path).authenticate(secret) is not None


def test_device_label() -> None:
    assert device_label(IPHONE_UA) == "iPhone"
    assert device_label("Mozilla/5.0 (Linux; Android 16; Pixel 10) Chrome/140") == "Android"
    assert device_label("") == "端末"


# ---------- サーバー ----------


@pytest.fixture
def store(tmp_path: Path) -> DeviceStore:
    return DeviceStore(tmp_path / "devices.json")


@pytest.fixture
def make_app(make_config: Callable[..., AppConfig], store: DeviceStore) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(base_url: str = BASE_URL, **overrides: object) -> TestClient:
        values: dict[str, object] = {"devices": store, "local_name": LOCAL_NAME}
        values.update(overrides)
        client = TestClient(create_app(make_config(**values)), base_url=base_url, headers={"User-Agent": IPHONE_UA})
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


def test_qr_auth_remembers_the_phone(make_app: Callable[..., TestClient], store: DeviceStore) -> None:
    phone = make_app()
    response = phone.get(f"/auth?t={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{DEVICE_COOKIE_NAME}=")
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=" in cookie
    # 端末を覚えるときは、起動ごとのセッションCookieは使わない（解除をすぐに効かせるため）
    assert "landrop_session" not in cookie
    assert TOKEN not in cookie
    assert [d["label"] for d in store.list()] == ["iPhone"]
    assert phone.get("/api/info").json()["remembered"] is True


def test_scanning_again_does_not_add_a_device(make_app: Callable[..., TestClient], store: DeviceStore) -> None:
    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    phone.get(f"/auth?t={TOKEN}")

    assert len(store.list()) == 1


def test_remembered_phone_needs_no_qr_after_restart(
    make_app: Callable[..., TestClient], store: DeviceStore, tmp_path: Path
) -> None:
    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    cookie = phone.cookies[DEVICE_COOKIE_NAME]

    # 起動し直すと、QRコードのトークンもセッションの鍵も変わる
    restarted = make_app(token="new-token", session_key="new-session", devices=DeviceStore(tmp_path / "devices.json"))
    assert restarted.get("/api/files").status_code == 401
    restarted.cookies.set(DEVICE_COOKIE_NAME, cookie)
    assert restarted.get("/api/files").status_code == 200
    assert restarted.get(f"/auth?t={TOKEN}").status_code == 403


def test_without_store_the_old_session_cookie_is_used(make_app: Callable[..., TestClient]) -> None:
    phone = make_app(devices=None, local_name=None)
    response = phone.get(f"/auth?t={TOKEN}", follow_redirects=False)

    assert "landrop_session=" in response.headers["set-cookie"]
    info = phone.get("/api/info").json()
    assert info["remembered"] is False
    assert info["local_url"] is None


def test_handoff_moves_the_phone_to_the_local_name(
    make_app: Callable[..., TestClient], store: DeviceStore, tmp_path: Path
) -> None:
    store_path = tmp_path / "devices.json"
    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    info = phone.get("/api/info").json()
    assert info["local_url"] == LOCAL_BASE

    response = phone.post("/api/handoff", headers={"Origin": BASE_URL})
    assert response.status_code == 200
    url = response.json()["url"]
    assert url.startswith(f"{LOCAL_BASE}/auth?h=")
    assert TOKEN not in url

    # 名前のアドレスはCookieが別。使い捨ての鍵で、このアドレス用の鍵を受け取る
    local = make_app(base_url=LOCAL_BASE)
    assert local.get("/api/files").status_code == 401
    redirected = local.get(url.removeprefix(LOCAL_BASE), follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/"
    assert local.get("/api/files").status_code == 200
    assert len(store.list()) == 1
    # すでに名前のアドレスの鍵を持っていれば、もう一度移っても鍵を増やさない
    before = json.loads(store_path.read_text(encoding="utf-8"))["devices"][0]["key_hashes"]
    second = phone.post("/api/handoff", headers={"Origin": BASE_URL}).json()["url"]
    response = local.get(second.removeprefix(LOCAL_BASE), follow_redirects=False)
    assert response.status_code == 303
    assert "set-cookie" not in response.headers
    assert local.get("/api/files").status_code == 200
    assert sorted(store._devices[next(iter(store._devices))].key_hashes) == sorted(before)  # noqa: SLF001
    # 同じ鍵はもう使えない
    again = make_app(base_url=LOCAL_BASE)
    assert again.get(url.removeprefix(LOCAL_BASE), follow_redirects=False).status_code == 403


def test_handoff_requires_a_remembered_phone_and_same_origin(make_app: Callable[..., TestClient]) -> None:
    anon = make_app()
    assert anon.post("/api/handoff").status_code == 401

    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    assert phone.post("/api/handoff", headers={"Origin": "http://evil.example"}).status_code == 403

    no_name = make_app(local_name=None)
    no_name.get(f"/auth?t={TOKEN}")
    assert no_name.post("/api/handoff").status_code == 404

    # 起動ごとのセッションCookieで入った端末（覚えていない端末）には出さない
    session_only = make_app()
    session_only.cookies.set("landrop_session", "test-session-key")
    assert session_only.post("/api/handoff").status_code == 404


def test_invalid_handoff_token_is_rejected(make_app: Callable[..., TestClient]) -> None:
    local = make_app(base_url=LOCAL_BASE)
    response = local.get("/auth?h=guess", follow_redirects=False)

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_local_name_is_an_allowed_host_and_probe_image_is_allowed(make_app: Callable[..., TestClient]) -> None:
    local = make_app(base_url=LOCAL_BASE)
    assert local.get("/static/icon.svg").status_code == 200
    other = make_app(base_url=f"http://other-pc.local:{PORT}")
    assert other.get("/static/icon.svg").status_code == 400

    csp = make_app().get("/static/icon.svg").headers["content-security-policy"]
    assert f"img-src 'self' data: {LOCAL_BASE};" in csp
    plain = make_app(local_name=None).get("/static/icon.svg").headers["content-security-policy"]
    assert "img-src 'self' data:;" in plain


# ---------- PCの画面での一覧と解除 ----------


def _pc_client(make_config: Callable[..., AppConfig], store: DeviceStore) -> TestClient:
    config = make_config(pc_token=PC_TOKEN, pc_session_key=PC_SESSION_KEY, devices=store, local_name=LOCAL_NAME)
    client = TestClient(create_app(config), base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))
    assert client.get(f"/pc/auth?t={PC_TOKEN}").status_code == 200
    return client


def test_pc_lists_and_forgets_devices(
    make_app: Callable[..., TestClient], make_config: Callable[..., AppConfig], store: DeviceStore
) -> None:
    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    with _pc_client(make_config, store) as pc:
        status = pc.get("/pc/api/status").json()
        assert status["remembers_devices"] is True
        assert status["local_url"] == LOCAL_BASE

        devices = pc.get("/pc/api/devices").json()["devices"]
        assert [d["label"] for d in devices] == ["iPhone"]
        assert set(devices[0]) == {"id", "label", "created", "last_seen", "expires"}

        origin = {"Origin": f"http://127.0.0.1:{PORT}"}
        assert pc.post(f"/pc/api/devices/forget?id={devices[0]['id']}", headers={"Origin": "http://evil"}).status_code == 403
        assert pc.post(f"/pc/api/devices/forget?id={devices[0]['id']}", headers=origin).status_code == 204
        assert pc.post(f"/pc/api/devices/forget?id={devices[0]['id']}", headers=origin).status_code == 404
        assert pc.get("/pc/api/devices").json()["devices"] == []

    # 解除はすぐに効く
    assert phone.get("/api/files").status_code == 401


def test_phone_cannot_use_device_api(make_app: Callable[..., TestClient]) -> None:
    phone = make_app()
    phone.get(f"/auth?t={TOKEN}")
    assert phone.get("/pc/api/devices").status_code == 404
    assert phone.post("/pc/api/devices/forget?id=x").status_code == 404


# ---------- 名前と、前回のポート ----------


@pytest.mark.parametrize(
    ("pc_name", "expected"),
    [
        ("DESKTOP-AB12CD", "landrop-desktop-ab12cd.local"),
        ("My_PC.lan", "landrop-my-pc-lan.local"),
        ("やまだのPC", "landrop-pc.local"),
        ("田中", "landrop.local"),
        ("x" * 80, "landrop-" + "x" * 32 + ".local"),
    ],
)
def test_local_name(pc_name: str, expected: str) -> None:
    assert mdns.local_name(pc_name) == expected


def test_fallback_port_is_not_remembered() -> None:
    cli._remember_port(8765, 8766)
    assert cli._last_port() is None
    cli._remember_port(8765, 8765)
    assert cli._last_port() == 8765
    # 前回の番号が一時的に使われていても、覚えている番号は変えない
    cli._remember_port(8765, 8770)
    assert cli._last_port() == 8765


def test_last_port_is_remembered(tmp_path: Path) -> None:
    assert cli._last_port() is None
    cli._save_last_port(8766)
    assert cli._last_port() == 8766
    for broken in ("{", '{"port": "8765"}', '{"port": 80}', "[]"):
        cli.STATE_FILE.write_text(broken, encoding="utf-8")
        assert cli._last_port() is None, broken


def test_forget_devices_option() -> None:
    assert cli.build_parser().parse_args(["--forget-devices"]).forget_devices is True
    assert cli.build_parser().parse_args([]).forget_devices is False

