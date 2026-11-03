"""起動オプションの解析と、サーバーの起動。"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import re
import socket
import sys
import threading
import unicodedata
from pathlib import Path

import uvicorn

from landrop import __version__, desktop, network
from landrop.app import AppConfig, create_app
from landrop.idle import IdleTracker, StopRequest
from landrop.qr import render_qr
from landrop.security import PC_AUTH_PATH

DEFAULT_PORT = 8765
DEFAULT_SHARE = Path.home() / "LanDrop" / "share"
DEFAULT_RECEIVED = Path.home() / "LanDrop" / "received"
DEFAULT_MAX_SIZE = "10GB"
DEFAULT_TIMEOUT_MINUTES = 30.0
# 画面のないアプリとして起動したときに、ターミナルへの表示の代わりに書き出すファイル
LOG_FILE = Path.home() / "LanDrop" / "LanDrop.log"
_WATCH_SECONDS = 0.5
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int:
    """「10GB」「500MB」「1024」のような指定をバイト数にする（1KB = 1024バイト）。"""
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
        "--port", type=_port, help=f"待ち受けるポート番号（省略時は {DEFAULT_PORT}。使用中なら空きを探す）"
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
        "--version", action="version", version=f"LanDrop {__version__}", help="バージョンを表示して終了する"
    )
    return parser


def _command() -> str:
    """案内に表示する起動コマンド。"""
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
    """ダブルクリックで起動したアプリが、エラーを読む前に閉じてしまわないようにする。"""
    if not getattr(sys, "frozen", False):
        return
    if _stdin_is_tty():
        try:
            input("\nEnterキーを押すと閉じます。")
        except EOFError:
            pass
    else:
        desktop.alert("LanDrop を起動できませんでした", f"詳しくは {LOG_FILE} を確認してください。")


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
    ip = _validate_ip(args.ip, interfaces) if args.ip else _select_ip(interfaces, detailed)
    if ip is None:
        return 2

    requested_port = args.port or DEFAULT_PORT
    try:
        sock = network.bind_socket(ip, requested_port, search=args.port is None)
    except network.PortUnavailableError:
        print(f"ポート {requested_port} は使用中か、使用できません。--port で別の番号を指定してください。")
        return 1
    except OSError as e:
        print(f"{ip} で待ち受けできませんでした: {e}")
        print("このPCのLANのIPアドレスか確認し、--ip で指定し直してください。")
        return 1
    pc_sock = _bind_pc_socket(ip, sock)

    tracker = IdleTracker(args.timeout * 60)
    config = AppConfig(
        share_dir=share_dir,
        received_dir=received_dir,
        host_ip=ip,
        port=sock.getsockname()[1],
        max_size=args.max_size,
        pc_name=socket.gethostname(),
        tracker=tracker,
    )
    app = create_app(config)
    pc_url = f"http://127.0.0.1:{pc_sock.getsockname()[1]}{PC_AUTH_PATH}?t={config.pc_token}" if pc_sock else None
    warnings = _profile_warnings(ip, interfaces, detailed)
    _print_banner(config, args.timeout, requested_port, warnings, pc_url, args.no_open)

    uvicorn_config = uvicorn.Config(
        app,
        host=ip,
        port=config.port,
        # アクセスログにはQRコードのURL（トークン）が残るため出力しない
        access_log=False,
        log_level="warning",
        server_header=False,
        # 停止時に転送中の接続を待ち続けないようにする（.part は削除される）
        timeout_graceful_shutdown=5,
    )
    # Config の作成時にUvicornがログを設定し直すため、フィルターはその後で付ける
    logging.getLogger("uvicorn.error").addFilter(_InvalidRequestNotice())
    server = uvicorn.Server(uvicorn_config)
    finished = threading.Event()
    threading.Thread(target=_watch, args=(server, tracker, config.stop, finished, args.timeout), daemon=True).start()
    if pc_url and not args.no_open:
        # ソケットはすでに待ち受けているので、サーバーの準備より先に開いてもつながる
        desktop.open_app_window(pc_url)
    sockets = [sock] if pc_sock in (None, sock) else [sock, pc_sock]
    try:
        server.run(sockets=sockets)
    except KeyboardInterrupt:
        pass
    finally:
        finished.set()
        for s in sockets:
            s.close()
    print("LanDrop を停止しました。")
    return 0


def _bind_pc_socket(ip: str, lan_sock: socket.socket) -> socket.socket | None:
    """PCの画面用に、このPCからだけ届く 127.0.0.1 で待ち受けるソケットを用意する。"""
    if ipaddress.IPv4Address(ip).is_loopback:
        return lan_sock
    try:
        return network.bind_socket("127.0.0.1", lan_sock.getsockname()[1], search=True)
    except OSError as e:
        print(f"[注意] PCの画面を用意できませんでした（{e}）。スマホとの送受信はこのまま使えます。")
        return None


def _validate_ip(text: str, interfaces: list[network.Interface]) -> str | None:
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
        print(f"[注意] {addr} は「{reason}」のようです。スマホから接続できない場合は別のアドレスを指定してください。")
    return str(addr)


def _select_ip(interfaces: list[network.Interface], detailed: bool) -> str | None:
    chosen = network.choose_interface(interfaces, network.default_route_ip())
    if chosen is not None:
        if not detailed:
            print(f"[注意] アダプターの情報を取得できなかったため {chosen.ip} を使います。違う場合は --ip で指定してください。")
        return chosen.ip

    # スマホから届くことのないループバックとリンクローカルは選択肢に出さない
    candidates = [
        i
        for i in network.sort_candidates(interfaces)
        if not (ipaddress.IPv4Address(i.ip).is_loopback or ipaddress.IPv4Address(i.ip).is_link_local)
    ]
    if not candidates:
        print("LANのIPv4アドレスが見つかりませんでした。")
        print("Wi-Fiや有線LANにつながっているか確認するか、--ip でアドレスを指定してください。")
        return None

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
    """Windowsで、接続中のネットワークが「パブリック」になっていれば警告する。"""
    if os.name != "nt" or ipaddress.IPv4Address(ip).is_loopback:
        return []
    match = next((i for i in interfaces if i.ip == ip), None)
    if not detailed or match is None or not match.category:
        return ["ネットワークの種類（パブリック/プライベート）を確認できませんでした。"]
    if match.category != "Public":
        return []
    return [
        f"「{match.name}」は「パブリック ネットワーク」に設定されています。",
        "Windowsのファイアウォールにより、スマホから接続できない可能性があります。",
        "自宅など信頼できるネットワークであれば、設定 > ネットワークとインターネット >",
        "Wi-Fi（またはイーサネット）> ネットワーク プロファイルの種類 で「プライベート」に変更してください。",
        "（LanDropは設定を自動では変更しません）",
    ]


def _print_banner(
    config: AppConfig, timeout: float, requested_port: int, warnings: list[str], pc_url: str | None, no_open: bool
) -> None:
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
        print(f"  {_pad(label, 14)}{value}")
    print()

    qr = render_qr(config.access_url, color=_enable_ansi())
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
    if pc_url:
        if no_open:
            print("  PCの画面を使うときは、このPCのブラウザで次のURLを開いてください。")
        else:
            print("  PCの画面を開きました。開かないときは、このPCのブラウザで次のURLを開いてください。")
        print(f"  {pc_url}")
        print()

    notices = [
        [
            "通信は暗号化されません（HTTP）。公共Wi-Fi・ホテル・カフェなど、",
            "知らない人と共有するネットワークでは使わないでください。",
        ]
    ]
    if config.port != requested_port:
        notices.append([f"ポート {requested_port} が使用中のため、{config.port} を使います。"])
    if warnings:
        notices.append(warnings)
    for first, *rest in notices:
        print(f"  [注意] {first}")
        for line in rest:
            print(f"         {line}")
    print()
    print("  接続できないときは:")
    print("   1. スマホとPCが同じWi-Fi（同じルーター）につながっているか確認する")
    print("   2. ゲストWi-Fiや端末どうしの通信を遮断する設定、スマホのVPNがオフか確認する")
    print("   3. Windowsのファイアウォールで、Python の「プライベート ネットワーク」での通信を許可する")
    print(f"   4. 別のアドレスで起動し直す: {_command()} --ip <このPCのIPアドレス>")
    print("   詳しくは README.md の「接続できないとき」を参照してください。")
    print()
    print("  Ctrl+C で停止します（PCの画面を閉じても停止します）。", flush=True)


class _InvalidRequestNotice(logging.Filter):
    """HTTPとして読めない接続についてのUvicornの英語の警告を、日本語のお知らせ1回に置き換える。

    ブラウザが http:// のアドレスをまず https:// で開こうとすると、TLSの接続要求が届いてこの警告が出る。
    ブラウザはその後HTTPで接続し直すことが多く、接続のたびに繰り返し表示されると紛らわしい。
    """

    _MESSAGE = "Invalid HTTP request received."

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


def _watch(
    server: uvicorn.Server, tracker: IdleTracker, stop: StopRequest, finished: threading.Event, minutes: float
) -> None:
    """無操作による自動停止と、PCの画面からの停止の依頼を見張る。"""
    while not finished.wait(_WATCH_SECONDS):
        if stop.reason:
            message = stop.reason
        elif tracker.is_expired():
            message = f"{minutes:g}分間操作がなかったため、自動停止します。"
        else:
            continue
        print(f"\n{message}", flush=True)
        server.should_exit = True
        return


def _pad(text: str, width: int) -> str:
    """全角文字を2文字分として、表示幅をそろえる。"""
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - used, 1)


def _prepare_console() -> None:
    if sys.stdout is None:
        # 画面のないアプリ（macOSの .app）として起動したときは、表示をファイルに残す
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_FILE, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    # 表示できない文字（フォルダ名の絵文字など）があっても起動を止めない
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


def _enable_ansi() -> bool:
    """色指定のエスケープシーケンスを使えるか。Windowsでは仮想端末モードを有効にする。"""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # 標準出力
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False
