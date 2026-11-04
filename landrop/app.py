"""FastAPIアプリケーションの組み立て。"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import secrets
import sys
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from landrop import desktop
from landrop.filenames import content_disposition
from landrop.idle import ActivityMiddleware, IdleTracker, StopRequest
from landrop.qr import render_qr_svg
from landrop.security import (
    AUTH_PATH,
    COOKIE_NAME,
    PC_AUTH_PATH,
    PC_COOKIE_NAME,
    PC_PREFIX,
    AuthMiddleware,
    SecurityHeadersMiddleware,
    is_same_origin,
    message_page,
    token_matches,
)
from landrop.storage import Receiver, UploadError, find_file, list_files

STATIC_DIR = Path(__file__).parent / "static"
_CONTENT_LENGTH = re.compile(r"[0-9]+")
# PCの画面とのつなぎっぱなしの接続で、停止の依頼がないか確かめる間隔
_EVENTS_CHECK_SECONDS = 0.5

# PCの画面で中身を表示してよい種類。HTMLやSVGのように、開くとスクリプトが動く形式は含めない
PREVIEW_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
# 中身が別の形式だったとしても、直接開いたときにスクリプトを動かさない
_PREVIEW_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
    "Cache-Control": "private, max-age=300",
}

# Windowsではレジストリの設定で .js が text/plain になることがあり、nosniff と組み合わさると
# スクリプトが読み込まれなくなるため明示しておく
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


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
    # PCの画面用のトークンとCookieの値（スマホ用とは別）
    pc_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    pc_session_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    stop: StopRequest = field(default_factory=StopRequest)
    # 起動時の注意（パブリック ネットワークなど）。PCの画面に表示する
    notices: tuple[str, ...] = ()
    # PCの画面が閉じられてから停止するまでの猶予。再読み込みなら、この間に開き直される
    close_grace_seconds: float = 4.0

    @property
    def base_url(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    @property
    def access_url(self) -> str:
        return f"{self.base_url}{AUTH_PATH}?t={self.token}"


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status_code)


def _preview_type(name: str) -> str | None:
    return PREVIEW_TYPES.get(Path(name).suffix.lower())


async def _receive_upload(request: Request, receiver: Receiver, name: str) -> Response:
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


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="LanDrop", docs_url=None, redoc_url=None, openapi_url=None)
    receiver = Receiver(config.received_dir, config.max_size)
    # PCから共有フォルダへの追加。スマホからの受信と違い上限は設けない（空き容量は確認する）
    sharer = Receiver(config.share_dir, sys.maxsize)
    folders = {"share": config.share_dir, "received": config.received_dir}
    # 開いているPCの画面の数（つなぎっぱなしの接続の数）
    open_windows = 0

    def find_in(folder: str, name: str) -> Path | None:
        path = folders.get(folder)
        if path is None:
            return None
        try:
            return find_file(path, name)
        except OSError:
            return None

    def stop_when_closed() -> None:
        if open_windows:
            # 再読み込みや、別の窓で開き直された
            return
        if receiver.in_progress or config.tracker.active_requests:
            # 転送中なら、終わるのを待ってから止める
            _later(config.close_grace_seconds, stop_when_closed)
            return
        config.stop.request("PCの画面が閉じられたため、停止しました。")

    # ---------- スマホの画面 ----------

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
            shared = list_files(config.share_dir)
        except OSError:
            return _error(503, "PCの共有フォルダを読み取れません。")
        return JSONResponse({"files": [asdict(f) for f in shared]})

    @app.get("/api/files/{name}")
    def download(name: str) -> Response:
        path = find_in("share", name)
        if path is None:
            return _error(404, "ファイルが見つかりません。")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(path.name)},
        )

    @app.post("/api/upload")
    async def upload(request: Request, name: str = "") -> Response:
        return await _receive_upload(request, receiver, name)

    # ---------- PCの画面（127.0.0.1 からだけ使える） ----------

    @app.get(PC_AUTH_PATH)
    def pc_auth(t: str = "") -> Response:
        if not token_matches(t, config.pc_token):
            return message_page("このリンクは使えません", "LanDropを起動し直すと、新しいPCの画面が開きます。", 403)
        response = RedirectResponse(PC_PREFIX, status_code=303)
        response.set_cookie(PC_COOKIE_NAME, config.pc_session_key, httponly=True, samesite="strict", path=PC_PREFIX)
        return response

    @app.get(PC_PREFIX)
    def pc_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "pc.html", media_type="text/html; charset=utf-8")

    @app.get(PC_PREFIX + "/api/status")
    def pc_status() -> dict[str, object]:
        return {
            "pc_name": config.pc_name,
            "address": config.base_url,
            "phone_url": config.access_url,
            "share_dir": str(config.share_dir),
            "received_dir": str(config.received_dir),
            "max_size": config.max_size,
            "remaining_seconds": config.tracker.remaining_seconds(),
            "notices": list(config.notices),
        }

    @app.get(PC_PREFIX + "/qr.svg")
    def pc_qr() -> Response:
        return Response(render_qr_svg(config.access_url), media_type="image/svg+xml")

    @app.get(PC_PREFIX + "/api/events")
    def pc_events() -> StreamingResponse:
        """PCの画面が開いている間つないでおく接続。切れたら、画面が閉じられたとみなす。

        窓を閉じるとブラウザごと終了することもあるため、閉じるときの連絡には頼らない。
        """

        async def stream() -> AsyncIterator[bytes]:
            nonlocal open_windows
            open_windows += 1
            try:
                yield b"open\n"
                # 停止するときは、こちらから接続を終えてサーバーの終了を待たせない
                while config.stop.reason is None:
                    await asyncio.sleep(_EVENTS_CHECK_SECONDS)
            finally:
                open_windows -= 1
                _later(config.close_grace_seconds, stop_when_closed)

        return StreamingResponse(stream(), media_type="text/plain")

    @app.get(PC_PREFIX + "/api/files/{folder}")
    def pc_files(folder: str) -> Response:
        path = folders.get(folder)
        if path is None:
            return _error(404, "フォルダが見つかりません。")
        try:
            found = list_files(path)
        except OSError:
            return _error(503, "フォルダを読み取れません。")
        body: dict[str, object] = {
            "files": [{**asdict(f), "preview": _preview_type(f.name) is not None} for f in found]
        }
        if folder == "received":
            body["incoming"] = [asdict(i) for i in receiver.in_progress]
        return JSONResponse(body)

    @app.get(PC_PREFIX + "/preview/{folder}/{name}")
    def pc_preview(folder: str, name: str) -> Response:
        media_type = _preview_type(name)
        path = find_in(folder, name) if media_type else None
        if path is None:
            return _error(404, "表示できるファイルが見つかりません。")
        return FileResponse(path, media_type=media_type, headers=_PREVIEW_HEADERS)

    @app.post(PC_PREFIX + "/api/share")
    async def pc_share(request: Request, name: str = "") -> Response:
        return await _receive_upload(request, sharer, name)

    @app.post(PC_PREFIX + "/api/reveal")
    def pc_reveal(request: Request, folder: str = "", name: str = "") -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        path = find_in(folder, name)
        if path is None:
            return _error(404, "ファイルが見つかりません。")
        try:
            desktop.reveal(path)
        except OSError:
            return _error(500, "フォルダを開けませんでした。")
        return Response(status_code=204)

    @app.post(PC_PREFIX + "/api/open-folder")
    def pc_open_folder(request: Request, folder: str = "") -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        path = folders.get(folder)
        if path is None:
            return _error(404, "フォルダが見つかりません。")
        try:
            desktop.open_folder(path)
        except OSError:
            return _error(500, "フォルダを開けませんでした。")
        return Response(status_code=204)

    @app.post(PC_PREFIX + "/api/stop")
    def pc_stop(request: Request) -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        config.stop.request("PCの画面から停止しました。")
        return Response(status_code=202)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 後から追加したものほど外側で動く
    app.add_middleware(ActivityMiddleware, tracker=config.tracker)
    app.add_middleware(AuthMiddleware, session_key=config.session_key, pc_session_key=config.pc_session_key)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[config.host_ip, "localhost", "127.0.0.1"],
        www_redirect=False,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _later(seconds: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
