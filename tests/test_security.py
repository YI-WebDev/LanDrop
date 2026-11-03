"""Hostヘッダーの検証・セキュリティヘッダー・CORS・別サイトからの送信。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "host",
    ["evil.example.com", "evil.example.com:8765", "192.168.1.51:8765", "landrop.local", "0.0.0.0:8765"],
)
def test_unknown_host_header_is_rejected(client: TestClient, host: str) -> None:
    response = client.get("/api/files", headers={"host": host})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


@pytest.mark.parametrize("host", ["192.168.1.50:8765", "192.168.1.50", "localhost:8765", "127.0.0.1:8765"])
def test_allowed_host_header_passes(anon_client: TestClient, host: str) -> None:
    # Hostの検査を通過し、認証の検査で止まる
    assert anon_client.get("/api/files", headers={"host": host}).status_code == 401


def test_security_headers_on_every_response(client: TestClient, anon_client: TestClient) -> None:
    responses = [
        client.get("/"),
        client.get("/api/files"),
        anon_client.get("/api/files"),
        client.get("/api/files", headers={"host": "evil.example.com"}),
        client.get("/static/app.js"),
    ]
    for response in responses:
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_cors_is_not_allowed(client: TestClient) -> None:
    preflight = client.options(
        "/api/upload",
        headers={"origin": "http://evil.example.com", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    response = client.get("/api/files", headers={"origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_upload_from_another_site_is_rejected(client: TestClient, received_dir: Path) -> None:
    response = client.post(
        "/api/upload?name=a.txt", content=b"x", headers={"origin": "http://evil.example.com"}
    )

    assert response.status_code == 403
    assert list(received_dir.iterdir()) == []


def test_upload_from_own_page_is_accepted(client: TestClient, received_dir: Path) -> None:
    response = client.post(
        "/api/upload?name=a.txt", content=b"x", headers={"origin": "http://192.168.1.50:8765"}
    )

    assert response.status_code == 201
    assert (received_dir / "a.txt").read_bytes() == b"x"


def test_api_docs_are_disabled(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
