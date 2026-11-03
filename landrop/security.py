"""トークン認証とセキュリティ関連のHTTPヘッダー。"""

from __future__ import annotations

import ipaddress
import secrets

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

COOKIE_NAME = "landrop_session"
AUTH_PATH = "/auth"
# PCの画面。127.0.0.1 で待ち受けるソケットからだけ使え、スマホ用とは別のトークンとCookieで認証する
PC_PREFIX = "/pc"
PC_AUTH_PATH = "/pc/auth"
PC_COOKIE_NAME = "landrop_pc"
# 認証済みのリクエストであることを後段のミドルウェアへ伝えるためのキー
AUTHENTICATED_SCOPE_KEY = "landrop.authenticated"
# 未認証でも返してよいもの（ファイル名やフォルダ構成を含まない画面部品のみ）
_PUBLIC_PREFIXES = ("/static/",)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
}

_MESSAGE_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LanDrop</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/ui.css">
</head>
<body class="message-page">
<main class="message-card">
<p class="brand">LanDrop</p>
<h1>{title}</h1>
<p>{body}</p>
</main>
</body>
</html>
"""


def token_matches(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def message_page(title: str, body: str, status_code: int) -> HTMLResponse:
    return HTMLResponse(_MESSAGE_PAGE.format(title=title, body=body), status_code=status_code)


def is_same_origin(request: Request) -> bool:
    """ブラウザからの送信が、このサーバー自身の画面から行われたものか確認する。"""
    origin = request.headers.get("origin")
    if origin is None:
        # curl など Origin を付けないクライアント
        return True
    return origin == f"http://{request.headers.get('host', '')}"


def is_local_connection(scope: Scope) -> bool:
    """127.0.0.1 で待ち受けているソケットへの、このPC自身からの接続かどうか。"""
    server, client = scope.get("server"), scope.get("client")
    if not server or not client:
        return False
    try:
        return ipaddress.ip_address(server[0]).is_loopback and ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _is_pc_path(path: str) -> bool:
    return path == PC_PREFIX or path.startswith(PC_PREFIX + "/")


class AuthMiddleware:
    """セッションCookieのないリクエストを、内容を一切返さずに拒否する。"""

    def __init__(self, app: ASGIApp, session_key: str, pc_session_key: str) -> None:
        self.app = app
        self.session_key = session_key
        self.pc_session_key = pc_session_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        if _is_pc_path(path):
            await self._pc(scope, receive, send)
            return
        if path == AUTH_PATH or path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        cookie = Request(scope).cookies.get(COOKIE_NAME, "")
        if cookie and token_matches(cookie, self.session_key):
            scope[AUTHENTICATED_SCOPE_KEY] = True
            await self.app(scope, receive, send)
            return

        response: Response
        if path.startswith("/api/"):
            response = JSONResponse(
                {"detail": "接続が確認できません。PCに表示されたQRコードを読み取ってください。"},
                status_code=401,
            )
        else:
            response = message_page(
                "QRコードから接続してください",
                "PCのLanDropの画面（またはターミナル）に表示されたQRコードを、スマホのカメラで読み取ってください。",
                401,
            )
        await response(scope, receive, send)

    async def _pc(self, scope: Scope, receive: Receive, send: Send) -> None:
        response: Response
        if not is_local_connection(scope):
            # スマホなど他の機器からは、PCの画面があること自体を分からないようにする
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
        elif scope["path"] == PC_AUTH_PATH:
            await self.app(scope, receive, send)
            return
        elif token_matches(Request(scope).cookies.get(PC_COOKIE_NAME, ""), self.pc_session_key):
            scope[AUTHENTICATED_SCOPE_KEY] = True
            await self.app(scope, receive, send)
            return
        elif scope["path"].startswith(PC_PREFIX + "/api/"):
            response = JSONResponse(
                {"detail": "PCの画面の接続が確認できません。LanDropを起動し直してください。"}, status_code=401
            )
        else:
            response = message_page(
                "PCの画面を開き直してください",
                "LanDropを起動したときに開いた画面か、ターミナルに表示された「PCの画面」のURLから開いてください。",
                401,
            )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """すべてのレスポンスにセキュリティ関連のヘッダーを付ける。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = Headers(raw=message.get("headers", []))
                extra = [
                    (key.lower().encode("latin-1"), value.encode("latin-1"))
                    for key, value in SECURITY_HEADERS.items()
                    if key not in existing
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)
