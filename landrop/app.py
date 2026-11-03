"""FastAPIアプリケーションの組み立て。"""

from __future__ import annotations

import mimetypes
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from landrop.filenames import content_disposition
from landrop.idle import ActivityMiddleware, IdleTracker
from landrop.security import (
    AUTH_PATH,
    COOKIE_NAME,
    AuthMiddleware,
    SecurityHeadersMiddleware,
    is_same_origin,
    message_page,
    token_matches,
)
from landrop.storage import Receiver, UploadError, find_shared_file, list_shared_files

STATIC_DIR = Path(__file__).parent / "static"
_CONTENT_LENGTH = re.compile(r"[0-9]+")

# Windowsではレジストリの設定で .js が text/plain になることがあり、nosniff と組み合わさると
# スクリプトが読み込まれなくなるため明示しておく
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


@dataclass(frozen=True)
class AppConfig:
    share_dir: Path
    received_dir: Path
    host_ip: str
    port: int
    max_size: int
    pc_name: str
    tracker: IdleTracker
    # QRコードのURLに含めるトークン（起動ごとに生成）
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Cookieに保存する値。URLに載るトークンとは別の値にしておく
    session_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def base_url(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    @property
    def access_url(self) -> str:
        return f"{self.base_url}{AUTH_PATH}?t={self.token}"


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status_code)


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="LanDrop", docs_url=None, redoc_url=None, openapi_url=None)
    receiver = Receiver(config.received_dir, config.max_size)

    @app.get(AUTH_PATH)
    def auth(t: str = "") -> Response:
        if not token_matches(t, config.token):
            return message_page(
                "このリンクは使えません",
                "LanDropを起動し直した場合は、新しく表示されたQRコードを読み取ってください。",
                403,
            )
        # トークンがアドレスバーや履歴に残らないよう、トークンなしのURLへ移動させる
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, config.session_key, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/api/info")
    def info() -> dict[str, object]:
        return {"pc_name": config.pc_name, "address": config.base_url, "max_size": config.max_size}

    @app.get("/api/files")
    def files() -> Response:
        try:
            shared = list_shared_files(config.share_dir)
        except OSError:
            return _error(503, "PCの共有フォルダを読み取れません。")
        return JSONResponse({"files": [asdict(f) for f in shared]})

    @app.get("/api/files/{name}")
    def download(name: str) -> Response:
        try:
            path = find_shared_file(config.share_dir, name)
        except OSError:
            path = None
        if path is None:
            return _error(404, "ファイルが見つかりません。")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(path.name)},
        )

    @app.post("/api/upload")
    async def upload(request: Request, name: str = "") -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの送信は受け付けません。")
        length = request.headers.get("content-length", "")
        if not _CONTENT_LENGTH.fullmatch(length):
            return _error(411, "ファイルサイズ（Content-Length）が必要です。")
        try:
            saved = await receiver.receive(name, int(length), request.stream())
        except UploadError as e:
            return _error(e.status_code, e.message)
        except ClientDisconnect:
            # 相手が切断しているため、このレスポンスは届かない
            return Response(status_code=400)
        except OSError:
            return _error(500, "PCの保存先フォルダに書き込めませんでした。")
        return JSONResponse({"name": saved}, status_code=201)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 後から追加したものほど外側で動く
    app.add_middleware(ActivityMiddleware, tracker=config.tracker)
    app.add_middleware(AuthMiddleware, session_key=config.session_key)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[config.host_ip, "localhost", "127.0.0.1"],
        www_redirect=False,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app
