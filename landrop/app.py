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
from landrop.devices import (
    ALREADY_REMEMBERED,
    DEVICE_COOKIE_MAX_AGE,
    DEVICE_COOKIE_NAME,
    DeviceStore,
    device_label,
)
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
    DEVICE_SCOPE_KEY,
    SecurityHeadersMiddleware,
    is_same_origin,
    message_page,
    security_headers,
    token_matches,
)
from landrop.storage import Receiver, UploadError, find_file, list_files

STATIC_DIR = Path(__file__).parent / "static"
_VALID_CONTENT_LENGTH = re.compile(r"[0-9]+")
_STOP_POLL_SECONDS = 0.5
_STATUS_CLIENT_DISCONNECTED = 400
_NO_SIZE_LIMIT = sys.maxsize

SCRIPTLESS_PREVIEW_TYPES = {
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
_PREVIEW_SANDBOX_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'",
    "Cache-Control": "private, max-age=300",
}


def _override_windows_registry_mime_types() -> None:
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("image/svg+xml", ".svg")


_override_windows_registry_mime_types()


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class AppConfig:
    share_dir: Path
    received_dir: Path
    host_ip: str
    port: int
    max_size: int
    pc_name: str
    tracker: IdleTracker
    phone_url_token: str = field(default_factory=_new_secret)
    phone_cookie_secret: str = field(default_factory=_new_secret)
    pc_url_token: str = field(default_factory=_new_secret)
    pc_cookie_secret: str = field(default_factory=_new_secret)
    stop: StopRequest = field(default_factory=StopRequest)
    startup_notices: tuple[str, ...] = ()
    pc_window_close_grace_seconds: float = 4.0
    devices: DeviceStore | None = None
    local_name: str | None = None

    @property
    def remembers_devices(self) -> bool:
        return self.devices is not None

    @property
    def base_url(self) -> str:
        return f"http://{self.host_ip}:{self.port}"

    @property
    def local_url(self) -> str | None:
        return f"http://{self.local_name}:{self.port}" if self.local_name else None

    @property
    def access_url(self) -> str:
        return f"{self.base_url}{AUTH_PATH}?t={self.phone_url_token}"


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status_code)


def _preview_type(name: str) -> str | None:
    return SCRIPTLESS_PREVIEW_TYPES.get(Path(name).suffix.lower())


async def _receive_upload(request: Request, receiver: Receiver, name: str) -> Response:
    if not is_same_origin(request):
        return _error(403, "このページ以外からの送信は受け付けません。")
    length = request.headers.get("content-length", "")
    if not _VALID_CONTENT_LENGTH.fullmatch(length):
        return _error(411, "ファイルサイズ（Content-Length）が必要です。")
    try:
        saved = await receiver.receive(name, int(length), request.stream())
    except UploadError as e:
        return _error(e.status_code, e.message)
    except ClientDisconnect:
        return Response(status_code=_STATUS_CLIENT_DISCONNECTED)
    except OSError:
        return _error(500, "PCの保存先フォルダに書き込めませんでした。")
    return JSONResponse({"name": saved}, status_code=201)


def _run_after_delay(seconds: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()


def create_app(config: AppConfig) -> FastAPI:
    app = FastAPI(title="LanDrop", docs_url=None, redoc_url=None, openapi_url=None)
    receiver = Receiver(config.received_dir, config.max_size)
    sharer = Receiver(config.share_dir, _NO_SIZE_LIMIT)
    folders = {"share": config.share_dir, "received": config.received_dir}
    open_pc_screen_connections = 0

    def find_in(folder: str, name: str) -> Path | None:
        path = folders.get(folder)
        if path is None:
            return None
        try:
            return find_file(path, name)
        except OSError:
            return None

    def stop_after_transfers_if_still_closed(pc_screen_is_still_closed: Callable[[], bool]) -> None:
        if not pc_screen_is_still_closed():
            return
        transfers_are_running = receiver.in_progress or config.tracker.active_requests
        if transfers_are_running:
            _run_after_delay(
                config.pc_window_close_grace_seconds,
                lambda: stop_after_transfers_if_still_closed(pc_screen_is_still_closed),
            )
            return
        config.stop.request("PCの画面が閉じられたため、停止しました。")

    app.state.pc_window_closed = lambda: stop_after_transfers_if_still_closed(lambda: True)

    def remember_device_with_cookie(response: Response, secret: str) -> Response:
        response.set_cookie(
            DEVICE_COOKIE_NAME,
            secret,
            max_age=DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get(AUTH_PATH)
    def auth(request: Request, t: str = "", h: str = "") -> Response:
        response = RedirectResponse("/", status_code=303)
        devices = config.devices
        arrived_at_local_name_from_handoff = bool(h) and devices is not None
        if arrived_at_local_name_from_handoff:
            current = devices.authenticate(request.cookies.get(DEVICE_COOKIE_NAME, ""))
            secret = devices.redeem_handoff(h, current_device=current)
            if secret == ALREADY_REMEMBERED:
                return response
            if secret is not None:
                return remember_device_with_cookie(response, secret)
        if not t or not token_matches(t, config.phone_url_token):
            return message_page(
                "このリンクは使えません",
                "LanDropを起動し直した場合は、新しく表示されたQRコードを読み取ってください。",
                403,
            )
        if devices is None:
            response.set_cookie(COOKIE_NAME, config.phone_cookie_secret, httponly=True, samesite="lax", path="/")
            return response
        device_is_already_remembered = devices.authenticate(request.cookies.get(DEVICE_COOKIE_NAME, "")) is not None
        if device_is_already_remembered:
            return response
        return remember_device_with_cookie(response, devices.register(device_label(request.headers.get("user-agent", ""))))

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/api/info")
    def info(request: Request) -> dict[str, object]:
        phone_is_remembered = DEVICE_SCOPE_KEY in request.scope
        return {
            "pc_name": config.pc_name,
            "address": config.base_url,
            "max_size": config.max_size,
            "remembered": phone_is_remembered,
            "local_url": config.local_url,
        }

    @app.post("/api/handoff")
    def handoff(request: Request) -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        device_id = request.scope.get(DEVICE_SCOPE_KEY)
        if config.devices is None or config.local_url is None or device_id is None:
            return _error(404, "名前のアドレスは使えません。")
        token = config.devices.create_handoff(device_id)
        if token is None:
            return _error(404, "名前のアドレスは使えません。")
        return JSONResponse({"url": f"{config.local_url}{AUTH_PATH}?h={token}"})

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

    @app.get(PC_AUTH_PATH)
    def pc_auth(t: str = "") -> Response:
        if not token_matches(t, config.pc_url_token):
            return message_page("このリンクは使えません", "LanDropを起動し直すと、新しいPCの画面が開きます。", 403)
        response = RedirectResponse(PC_PREFIX, status_code=303)
        response.set_cookie(PC_COOKIE_NAME, config.pc_cookie_secret, httponly=True, samesite="strict", path=PC_PREFIX)
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
            "local_url": config.local_url,
            "remembers_devices": config.remembers_devices,
            "share_dir": str(config.share_dir),
            "received_dir": str(config.received_dir),
            "max_size": config.max_size,
            "remaining_seconds": config.tracker.remaining_seconds(),
            "notices": list(config.startup_notices),
        }

    @app.get(PC_PREFIX + "/qr.svg")
    def pc_qr() -> Response:
        return Response(render_qr_svg(config.access_url), media_type="image/svg+xml")

    @app.get(PC_PREFIX + "/api/events")
    def pc_events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            nonlocal open_pc_screen_connections
            open_pc_screen_connections += 1
            try:
                yield b"open\n"
                while config.stop.reason is None:
                    await asyncio.sleep(_STOP_POLL_SECONDS)
            finally:
                open_pc_screen_connections -= 1
                _run_after_delay(
                    config.pc_window_close_grace_seconds,
                    lambda: stop_after_transfers_if_still_closed(
                        lambda: open_pc_screen_connections == 0
                    ),
                )

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
        return FileResponse(path, media_type=media_type, headers=_PREVIEW_SANDBOX_HEADERS)

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

    @app.get(PC_PREFIX + "/api/devices")
    def pc_devices() -> dict[str, object]:
        return {"devices": config.devices.list() if config.devices else []}

    @app.post(PC_PREFIX + "/api/devices/forget")
    def pc_forget_device(request: Request, id: str = "") -> Response:  # noqa: A002
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        if config.devices is None or not config.devices.forget(id):
            return _error(404, "端末が見つかりません。")
        return Response(status_code=204)

    @app.post(PC_PREFIX + "/api/stop")
    def pc_stop(request: Request) -> Response:
        if not is_same_origin(request):
            return _error(403, "このページ以外からの操作は受け付けません。")
        config.stop.request("PCの画面から停止しました。")
        return Response(status_code=202)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    allowed_hosts = [config.host_ip, "localhost", "127.0.0.1"]
    if config.local_name:
        allowed_hosts.append(config.local_name)
    headers_with_local_name_image = security_headers(config.local_url)
    middlewares_innermost_first = [
        (ActivityMiddleware, {"tracker": config.tracker}),
        (
            AuthMiddleware,
            {
                "phone_cookie_secret": config.phone_cookie_secret,
                "pc_cookie_secret": config.pc_cookie_secret,
                "devices": config.devices,
            },
        ),
        (TrustedHostMiddleware, {"allowed_hosts": allowed_hosts, "www_redirect": False}),
        (SecurityHeadersMiddleware, {"headers": headers_with_local_name_image}),
    ]
    for middleware, options in middlewares_innermost_first:
        app.add_middleware(middleware, **options)
    return app
