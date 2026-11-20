from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path

import uvicorn

from landrop import __version__, desktop, mdns, network
from landrop.app import AppConfig, create_app
from landrop.devices import DeviceStore
from landrop.idle import IdleTracker, StopRequest
from landrop.qr import render_qr
from landrop.security import PC_AUTH_PATH

DEFAULT_PORT = 8765
DEFAULT_SHARE = Path.home() / "LanDrop" / "share"
DEFAULT_RECEIVED = Path.home() / "LanDrop" / "received"
DEFAULT_MAX_SIZE = "10GB"
DEFAULT_TIMEOUT_MINUTES = 30.0
WINDOWLESS_APP_OUTPUT_LOG_FILE = Path.home() / "LanDrop" / "LanDrop.log"
REMEMBERED_DEVICES_FILE = Path.home() / "LanDrop" / "devices.json"
LAST_PORT_STATE_FILE = Path.home() / "LanDrop" / "state.json"
_STOP_AND_IDLE_POLL_SECONDS = 0.5
_JOIN_POLL_SECONDS = 0.5
_launched_without_terminal = False
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_ACCESS_LOG_EXPOSING_QR_TOKENS = False
_GRACEFUL_SHUTDOWN_SECONDS = 5
_LOOPBACK_IP = "127.0.0.1"
_PROGRESS_LINE_PREFIX = "ネットワークを確認"
_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def parse_size(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*", text, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("サイズは 10GB、500MB、1024 のように指定してください。")
    size = int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])
    if size <= 0:
        raise argparse.ArgumentTypeError("サイズには1バイト以上を指定してください。")
    return size


def format_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024
    return f"{size:.1f} TB".replace(".0 ", " ")


def _port(text: str) -> int:
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError("ポート番号は 1〜65535 で指定してください。")
    return int(text)


def _minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError("分数は0以上の数で指定してください（0で自動停止しない）。")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_command(),
        description="同じLAN内のPCとスマホの間で、ブラウザだけでファイルを送受信します。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="このヘルプを表示して終了する")
    parser.add_argument("--ip", help="待ち受けるLANのIPv4アドレス（省略時は自動検出）")
    parser.add_argument(
        "--port",
        type=_port,
        help=f"待ち受けるポート番号（省略時は前回と同じ番号、初めては {DEFAULT_PORT}。使用中なら空きを探す）",
    )
    parser.add_argument(
        "--share", type=Path, default=DEFAULT_SHARE, help="スマホへ渡すファイルを置くフォルダ（既定: %(default)s）"
    )
    parser.add_argument(
        "--received", type=Path, default=DEFAULT_RECEIVED, help="スマホから届いたファイルの保存先（既定: %(default)s）"
    )
    parser.add_argument(
        "--max-size",
        type=parse_size,
        default=parse_size(DEFAULT_MAX_SIZE),
        metavar="SIZE",
        help=f"1ファイルの上限サイズ。例: 500MB、2GB（既定: {DEFAULT_MAX_SIZE}）",
    )
    parser.add_argument(
        "--timeout",
        type=_minutes,
        default=DEFAULT_TIMEOUT_MINUTES,
        metavar="MINUTES",
        help="無操作が続いたら自動停止するまでの分数。0で自動停止しない（既定: 30）",
    )
    parser.add_argument("--no-open", action="store_true", help="起動時にPCの画面を開かない")
    parser.add_argument(
        "--forget-devices",
        action="store_true",
        help="覚えているスマホをすべて忘れる（次はQRコードの読み取りが必要）",
    )
    parser.add_argument(
        "--version", action="version", version=f"LanDrop {__version__}", help="バージョンを表示して終了する"
    )
    return parser


def _command() -> str:
    if getattr(sys, "frozen", False):
        return "LanDrop.exe" if os.name == "nt" else "LanDrop"
    return "python -m landrop"


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    args = build_parser().parse_args(argv)
    try:
        code = _run(args)
    except KeyboardInterrupt:
        print("\n中止しました。")
        return 130
    if code != 0:
        _keep_error_visible()
    return code


def _keep_error_visible() -> None:
    if not _launched_without_terminal:
        return
    sys.stdout.flush()
    try:
        lines = [line.strip() for line in WINDOWLESS_APP_OUTPUT_LOG_FILE.read_text(encoding="utf-8").splitlines()]
    except OSError:
        lines = []
    reason = "\n".join(
        line for line in lines if line and not line.startswith(_PROGRESS_LINE_PREFIX)
    )
    desktop.alert("LanDrop を起動できませんでした", f"{reason}\n\n（記録: {WINDOWLESS_APP_OUTPUT_LOG_FILE}）")


def _run(args: argparse.Namespace) -> int:
    share_dir = args.share.expanduser()
    received_dir = args.received.expanduser()
    try:
        share_dir.mkdir(parents=True, exist_ok=True)
        received_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"フォルダを作成できませんでした: {e}")
        return 1
    share_dir, received_dir = share_dir.resolve(), received_dir.resolve()
    if share_dir == received_dir:
        print("共有フォルダと受信フォルダには別々のフォルダを指定してください。")
        return 2

    print("ネットワークを確認しています...", flush=True)
    interfaces, detailed = network.find_interfaces()
    notices: list[str] = []
    ip = _validate_ip(args.ip, interfaces, notices) if args.ip else _select_ip(interfaces, detailed, notices)
    if ip is None:
        return 2

    requested_port = args.port or _last_port() or DEFAULT_PORT
    try:
        sock = network.bind_socket(ip, requested_port, search=args.port is None)
    except network.PortUnavailableError:
        print(f"ポート {requested_port} は使用中か、使用できません。--port で別の番号を指定してください。")
        return 1
    except OSError as e:
        print(f"{ip} で待ち受けできませんでした: {e}")
        print("このPCのLANのIPアドレスか確認し、--ip で指定し直してください。")
        return 1
    if sock.getsockname()[1] != requested_port:
        notices.append(
            f"ポート {requested_port} が使用中のため、{sock.getsockname()[1]} を使います。"
            "スマホのホーム画面から開けないときは、QRコードを読み取り直してください。"
        )
    _remember_port(requested_port, sock.getsockname()[1])
    pc_sock = _bind_pc_socket(ip, sock)
    nowhere_to_show_the_qr_code = pc_sock is None and _launched_without_terminal
    if nowhere_to_show_the_qr_code:
        sock.close()
        return 1
    notices += _profile_warnings(ip, interfaces, detailed)

    tracker = IdleTracker(args.timeout * 60)
    if args.forget_devices:
        try:
            REMEMBERED_DEVICES_FILE.unlink(missing_ok=True)
        except OSError as e:
            notices.append(f"覚えているスマホの一覧を消せませんでした: {e}")
    pc_name = socket.gethostname()
    is_loopback = ipaddress.IPv4Address(ip).is_loopback
    advertiser = (
        None
        if is_loopback
        else mdns.Advertiser(mdns.local_name(pc_name), ip, sock.getsockname()[1])
    )
    config = AppConfig(
        share_dir=share_dir,
        received_dir=received_dir,
        host_ip=ip,
        port=sock.getsockname()[1],
        max_size=args.max_size,
        pc_name=pc_name,
        tracker=tracker,
        startup_notices=tuple(notices),
        devices=DeviceStore(REMEMBERED_DEVICES_FILE),
        local_name=advertiser.name if advertiser else None,
    )
    app = create_app(config)
    pc_url = (
        f"http://{_LOOPBACK_IP}:{pc_sock.getsockname()[1]}"
        f"{PC_AUTH_PATH}?t={config.pc_url_token}"
        if pc_sock
        else None
    )
    _print_banner(config, args.timeout, pc_url, args.no_open)

    uvicorn_config = uvicorn.Config(
        app,
        host=ip,
        port=config.port,
        access_log=_ACCESS_LOG_EXPOSING_QR_TOKENS,
        log_level="warning",
        server_header=False,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
    )
    _install_invalid_request_notice()
    server = uvicorn.Server(uvicorn_config)
    finished = threading.Event()
    threading.Thread(
        target=_watch_for_stop,
        args=(server, tracker, config.stop, finished, args.timeout),
        daemon=True,
    ).start()
    sockets = [sock] if pc_sock in (None, sock) else [sock, pc_sock]
    if advertiser:
        advertiser.start()
    try:
        if pc_url and not args.no_open and desktop.window_available():
            _serve_with_window(server, sockets, pc_url, app.state.pc_window_closed)
        else:
            if pc_url and not args.no_open:
                _open_pc_screen(pc_url)
            server.run(sockets=sockets)
    except KeyboardInterrupt:
        pass
    finally:
        finished.set()
        if advertiser:
            advertiser.close()
        for s in sockets:
            s.close()
    print("LanDrop を停止しました。")
    return 0


def _install_invalid_request_notice() -> None:
    logging.getLogger("uvicorn.error").addFilter(_InvalidRequestNotice())


def _open_pc_screen(pc_url: str) -> None:
    desktop.open_app_window(pc_url)


def _last_port() -> int | None:
    try:
        port = json.loads(LAST_PORT_STATE_FILE.read_text(encoding="utf-8")).get("port")
    except (OSError, ValueError, AttributeError):
        return None
    return port if isinstance(port, int) and 1024 <= port <= 65535 else None


def _remember_port(requested: int, actual: int) -> None:
    port_changed = actual != requested
    if port_changed:
        return
    _save_last_port_if_possible(actual)


def _save_last_port_if_possible(port: int) -> None:
    try:
        LAST_PORT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_PORT_STATE_FILE.write_text(json.dumps({"port": port}), encoding="utf-8")
    except OSError:
        return


def _serve_with_window(
    server: uvicorn.Server,
    sockets: list[socket.socket],
    pc_url: str,
    on_window_closed: Callable[[], None],
) -> None:
    def serve() -> None:
        try:
            server.run(sockets=sockets)
        finally:
            desktop.close_window()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    window_was_shown = desktop.show_window_until_closed(pc_url)
    if window_was_shown:
        on_window_closed()
    else:
        desktop.open_app_window(pc_url)
    try:
        while thread.is_alive():
            thread.join(_JOIN_POLL_SECONDS)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join()


def _bind_pc_socket(ip: str, lan_sock: socket.socket) -> socket.socket | None:
    if ipaddress.IPv4Address(ip).is_loopback:
        return lan_sock
    try:
        return network.bind_socket(_LOOPBACK_IP, lan_sock.getsockname()[1], search=True)
    except OSError as e:
        print(f"[注意] PCの画面を用意できませんでした（{e}）。")
        return None


def _validate_ip(text: str, interfaces: list[network.Interface], notices: list[str]) -> str | None:
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        print("--ip にはIPv4アドレスを指定してください（例: --ip 192.168.1.10）。")
        return None
    if addr.is_unspecified or addr.is_multicast or addr == ipaddress.IPv4Address("255.255.255.255"):
        print(f"{addr} は指定できません。このPCのLANのIPアドレスを指定してください。")
        return None
    match = next((i for i in interfaces if i.ip == str(addr)), None)
    reason = network.exclusion_reason(match) if match else None
    if reason:
        notices.append(f"{addr} は「{reason}」のようです。スマホから接続できない場合は別のアドレスを指定してください。")
    return str(addr)


def _lan_candidates(interfaces: list[network.Interface]) -> list[network.Interface]:
    return [
        i
        for i in network.sort_candidates(interfaces)
        if not (ipaddress.IPv4Address(i.ip).is_loopback or ipaddress.IPv4Address(i.ip).is_link_local)
    ]


def _use_best_candidate(
    candidates: list[network.Interface], notices: list[str]
) -> str:
    best, others = candidates[0], "、".join(i.ip for i in candidates[1:])
    notices.append(
        f"LANのアドレスを1つに決められなかったため、{best.ip}（{best.label}）を使っています。"
        f"スマホから接続できないときは、ショートカットのリンク先の最後に「--ip {candidates[1].ip}」のように"
        f"ほかの候補（{others}）を付けて起動し直してください。"
    )
    return best.ip


def _select_ip(interfaces: list[network.Interface], detailed: bool, notices: list[str]) -> str | None:
    chosen = network.choose_interface(interfaces, network.default_route_ip())
    if chosen is not None:
        if not detailed:
            notices.append(f"アダプターの情報を取得できなかったため {chosen.ip} を使います。違う場合は --ip で指定してください。")
        return chosen.ip

    candidates = _lan_candidates(interfaces)
    if not candidates:
        print("LANのIPv4アドレスが見つかりませんでした。")
        print("Wi-Fiや有線LANにつながっているか確認するか、--ip でアドレスを指定してください。")
        return None

    if _launched_without_terminal:
        return _use_best_candidate(
            candidates, notices
        )

    print("\nLANのIPアドレスを1つに確定できませんでした。スマホと同じネットワークのものを選んでください。")
    for number, iface in enumerate(candidates, 1):
        reason = network.exclusion_reason(iface)
        note = f"  ※{reason}" if reason else ""
        print(f"  {number}) {iface.ip:<15}  {iface.label}{note}")
    if not _stdin_is_tty():
        print(f"--ip で指定して起動し直してください（例: {_command()} --ip 192.168.1.10）。")
        return None
    while True:
        try:
            answer = input("番号を入力してください（Enterで1）: ").strip()
        except EOFError:
            return None
        if not answer:
            return candidates[0].ip
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1].ip
        print(f"1〜{len(candidates)} の番号を入力してください。")


def _profile_warnings(ip: str, interfaces: list[network.Interface], detailed: bool) -> list[str]:
    if os.name != "nt" or ipaddress.IPv4Address(ip).is_loopback:
        return []
    match = next((i for i in interfaces if i.ip == ip), None)
    if not detailed or match is None or not match.windows_network_category:
        return ["ネットワークの種類（パブリック/プライベート）を確認できませんでした。"]
    if match.windows_network_category != "Public":
        return []
    return [
        f"「{match.name}」は「パブリック ネットワーク」に設定されているため、Windowsのファイアウォールにより"
        "スマホから接続できない可能性があります。自宅など信頼できるネットワークであれば、設定 > "
        "ネットワークとインターネット > Wi-Fi（またはイーサネット）> ネットワーク プロファイルの種類 で"
        "「プライベート」に変更してください（LanDropは設定を自動では変更しません）。"
    ]


def _print_banner(config: AppConfig, timeout: float, pc_url: str | None, no_open: bool) -> None:
    timeout_text = f"{timeout:g}分間操作がなければ停止" if timeout > 0 else "しない"
    rows = [
        ("PC名", config.pc_name),
        ("アドレス", config.base_url),
        ("共有フォルダ", f"{config.share_dir}  （スマホへ渡すファイルを置く）"),
        ("受信フォルダ", f"{config.received_dir}  （スマホから届いたファイル）"),
        ("上限サイズ", f"1ファイル {format_size(config.max_size)}"),
        ("自動停止", timeout_text),
    ]
    print()
    print("=" * 64)
    print("  LanDrop を起動しました")
    print("=" * 64)
    for label, value in rows:
        print(f"  {_pad_to_display_width(label, 14)}{value}")
    print()

    qr = render_qr(config.access_url, color=_ansi_colors_available())
    if _can_print(qr):
        print("  スマホのカメラで、次のQRコードを読み取ってください。\n")
        print(qr)
        print()
        print("  QRコードが読めないときは、スマホのブラウザで次のURLを開いてください。")
    else:
        print("  このターミナルではQRコードを表示できません。スマホのブラウザで次のURLを開いてください。")
    print("  （このURLを知っている人は誰でも接続できます。他の人に見せないでください）")
    print(f"  {config.access_url}")
    print()
    if config.devices is not None:
        print("  一度接続したスマホは覚えておくので、次からはQRコードなしで開けます（30日間使わなければ忘れます）。")
        if config.local_url:
            print(f"  名前のアドレス: {config.local_url}  （スマホのホーム画面に追加すると、IPアドレスが変わっても開けます）")
        print()
    if pc_url:
        if no_open:
            print("  PCの画面を使うときは、このPCのブラウザで次のURLを開いてください。")
        else:
            print("  PCの画面を開きました。開かないときは、このPCのブラウザで次のURLを開いてください。")
        print(f"  {pc_url}")
        print()

    notices = [
        "通信は暗号化されません（HTTP）。公共Wi-Fi・ホテル・カフェなど、知らない人と共有するネットワークでは使わないでください。",
        *config.startup_notices,
    ]
    for notice in notices:
        print(f"  [注意] {notice}")
    print()
    print("  接続できないときは:")
    print("   1. スマホとPCが同じWi-Fi（同じルーター）につながっているか確認する")
    print("   2. ゲストWi-Fiや端末どうしの通信を遮断する設定、スマホのVPNがオフか確認する")
    print("   3. Windowsのファイアウォールで、LanDrop（または Python）の「プライベート ネットワーク」での通信を許可する")
    print(f"   4. 別のアドレスで起動し直す: {_command()} --ip <このPCのIPアドレス>")
    print("   詳しくは README.md の「接続できないとき」を参照してください。")
    print()
    print("  Ctrl+C で停止します（PCの画面を閉じても停止します）。", flush=True)


class _InvalidRequestNotice(logging.Filter):
    _MESSAGE = (
        "Invalid HTTP request received."
    )

    def __init__(self) -> None:
        super().__init__()
        self._shown = False

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != self._MESSAGE:
            return True
        if not self._shown:
            self._shown = True
            print("\n  [お知らせ] HTTPとして読めない接続を受け取ったため、受け付けずに切断しました。")
            print("             ブラウザが https:// で接続を試したときなどに起こり、LanDropの動作には影響しません。")
            print("             スマホでページが開かないときは、アドレスが http:// で始まっているか確認してください。")
            print("             （このお知らせは1回だけ表示します）", flush=True)
        return False


def _watch_for_stop(
    server: uvicorn.Server, tracker: IdleTracker, stop: StopRequest, finished: threading.Event, minutes: float
) -> None:
    while not finished.wait(_STOP_AND_IDLE_POLL_SECONDS):
        if stop.reason:
            message = stop.reason
        elif tracker.is_expired():
            message = f"{minutes:g}分間操作がなかったため、自動停止します。"
        else:
            continue
        print(f"\n{message}", flush=True)
        server.should_exit = True
        return


def _pad_to_display_width(text: str, width: int) -> str:
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - used, 1)


def _prepare_console() -> None:
    global _launched_without_terminal
    if sys.stdout is None:
        _launched_without_terminal = True
        _redirect_output_to_log_file()
    _replace_unprintable_characters()


def _redirect_output_to_log_file() -> None:
    WINDOWLESS_APP_OUTPUT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(WINDOWLESS_APP_OUTPUT_LOG_FILE, "w", encoding="utf-8", buffering=1)  # noqa: SIM115


def _replace_unprintable_characters() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _can_print(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _ansi_colors_available() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except (AttributeError, OSError):
        return False
