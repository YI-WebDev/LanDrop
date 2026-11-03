"""トークン認証・Cookie発行・未認証アクセスの拒否。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from landrop.app import AppConfig
from landrop.idle import IdleTracker
from tests.conftest import SESSION_KEY, TOKEN


def test_valid_token_sets_cookie_and_redirects_without_token(anon_client: TestClient) -> None:
    response = anon_client.get(f"/auth?t={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"landrop_session={SESSION_KEY}" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "path=/" in cookie.lower()
    # Cookie にはURLのトークンそのものを入れない
    assert TOKEN not in cookie


def test_after_auth_pages_are_available(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/api/files").status_code == 200
    info = client.get("/api/info").json()
    assert info["pc_name"] == "TEST-PC"
    # 画面に表示するアドレスにはトークンを含めない
    assert info["address"] == "http://192.168.1.50:8765"
    assert TOKEN not in client.get("/api/info").text


def test_wrong_or_missing_token_is_rejected(anon_client: TestClient) -> None:
    for url in ("/auth?t=wrong-token", "/auth", f"/auth?t={TOKEN}x", "/auth?t=%E3%81%82"):
        response = anon_client.get(url, follow_redirects=False)
        assert response.status_code == 403
        assert "set-cookie" not in response.headers
    assert anon_client.get("/api/files").status_code == 401


def test_unauthenticated_access_reveals_no_file_names(
    anon_client: TestClient, share_dir: Path
) -> None:
    (share_dir / "秘密の資料.pdf").write_bytes(b"secret")
    (share_dir / "subfolder").mkdir()

    for path in ("/", "/api/files", "/api/info", "/api/files/秘密の資料.pdf", "/api/files/x", "/nothing"):
        response = anon_client.get(path)
        assert response.status_code == 401, path
        assert "秘密" not in response.text
        assert "subfolder" not in response.text
        assert str(share_dir) not in response.text
        assert "TEST-PC" not in response.text


def test_unauthenticated_upload_is_rejected(anon_client: TestClient, received_dir: Path) -> None:
    response = anon_client.post("/api/upload?name=a.txt", content=b"hello")

    assert response.status_code == 401
    assert list(received_dir.iterdir()) == []


def test_wrong_cookie_is_rejected(anon_client: TestClient) -> None:
    anon_client.cookies.set("landrop_session", "guess")
    assert anon_client.get("/api/files").status_code == 401
    anon_client.cookies.set("landrop_session", TOKEN)
    assert anon_client.get("/api/files").status_code == 401


def test_static_assets_are_public_but_contain_no_file_info(anon_client: TestClient) -> None:
    response = anon_client.get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    script = anon_client.get("/static/app.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")


def test_tokens_are_random_per_launch(tmp_path: Path) -> None:
    def config() -> AppConfig:
        return AppConfig(
            share_dir=tmp_path,
            received_dir=tmp_path,
            host_ip="192.168.1.2",
            port=8765,
            max_size=1,
            pc_name="PC",
            tracker=IdleTracker(0),
        )

    first, second = config(), config()
    # secrets.token_urlsafe(32) は43文字
    assert len(first.token) == 43
    assert first.token != second.token
    assert first.token != first.session_key
    assert first.access_url == f"http://192.168.1.2:8765/auth?t={first.token}"
    assert first.token not in first.base_url
