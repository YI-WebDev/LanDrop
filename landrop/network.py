"""LANのIPv4アドレスの検出と、待ち受けポートの確保。"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass

# 一般的な家庭・社内LANで使われるアドレス範囲（RFC 1918）
_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
# Tailscaleなどが使う範囲
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")

# アダプター名・説明にこれらが含まれていれば、仮想アダプターやVPNとみなす
_VIRTUAL_KEYWORDS = (
    "virtual", "hyper-v", "vethernet", "wsl", "docker", "vmware", "virtualbox", "host-only",
    "vpn", "tap-windows", "wintun", "wireguard", "tailscale", "zerotier", "openvpn",
    "anyconnect", "globalprotect", "pangp", "fortinet", "forticlient", "loopback",
    "pseudo-interface", "bluetooth", "npcap", "teredo", "isatap",
)
# macOSなどのインターフェース名で、物理的なLANではないもの
_VIRTUAL_NAME = re.compile(
    r"(?:lo|utun|ipsec|ppp|bridge|vmenet|vmnet|vboxnet|awdl|llw|gif|stf|anpi|ap|feth"
    r"|docker|virbr|tun|tap|wg|tailscale)\d*|(?:br-|veth|zt).*",
    re.IGNORECASE,
)

# Windowsで使う読み取り専用のコマンド（IPアドレス、アダプターの説明、既定ゲートウェイ、接続プロファイル）
_WINDOWS_QUERY = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$desc = @{}; Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object { $desc[[int]$_.ifIndex] = $_.InterfaceDescription }
$gw = @{}; Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | ForEach-Object { $gw[[int]$_.ifIndex] = $true }
$cat = @{}; Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object { $cat[[int]$_.InterfaceIndex] = [string]$_.NetworkCategory }
@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | ForEach-Object {
  $i = [int]$_.InterfaceIndex
  [pscustomobject]@{ ip = $_.IPAddress; name = $_.InterfaceAlias; description = [string]$desc[$i]; gateway = [bool]$gw[$i]; category = [string]$cat[$i] }
}) | ConvertTo-Json -Compress
"""


@dataclass(frozen=True)
class Interface:
    ip: str
    name: str
    description: str = ""
    has_gateway: bool = False
    # Windowsのネットワークプロファイル（Public / Private / DomainAuthenticated）。不明なら空
    category: str = ""

    @property
    def label(self) -> str:
        if self.description and self.description != self.name:
            return f"{self.name}（{self.description}）"
        return self.name


def exclusion_reason(iface: Interface) -> str | None:
    """LANのアドレスとして自動選択しない理由。選択してよければ None。"""
    addr = ipaddress.IPv4Address(iface.ip)
    if addr.is_loopback:
        return "ループバック"
    if addr.is_link_local:
        return "リンクローカル（DHCPで取得できていない）"
    if addr in _CGNAT_NETWORK:
        return "VPNの可能性"
    text = f"{iface.name} {iface.description}".lower()
    if any(keyword in text for keyword in _VIRTUAL_KEYWORDS) or _VIRTUAL_NAME.fullmatch(iface.name):
        return "仮想アダプター/VPN"
    if not any(addr in network for network in _PRIVATE_NETWORKS):
        return "プライベートアドレスではない"
    return None


def choose_interface(interfaces: list[Interface], route_ip: str | None) -> Interface | None:
    """LANのアドレスを1つに確定できればそれを返し、確定できなければ None を返す。"""
    usable = [i for i in interfaces if exclusion_reason(i) is None]
    if len(usable) == 1:
        return usable[0]
    # 複数あれば、既定ゲートウェイを持つもの（インターネットへの通り道）を優先する
    preferred = [i for i in usable if i.has_gateway or i.ip == route_ip]
    if len(preferred) == 1:
        return preferred[0]
    return None


def sort_candidates(interfaces: list[Interface]) -> list[Interface]:
    """選択肢として表示する順（使えそうなもの、ゲートウェイがあるものを先に）。"""
    return sorted(interfaces, key=lambda i: (exclusion_reason(i) is not None, not i.has_gateway))


def find_interfaces() -> tuple[list[Interface], bool]:
    """IPv4アドレスを持つインターフェースの一覧と、アダプター情報まで取得できたかを返す。"""
    found = _windows_interfaces() if os.name == "nt" else _ifconfig_interfaces()
    if found is not None:
        return _unique(found), True
    # OSのコマンドが使えないときは、名前の分からない候補だけを集める
    ips = []
    route_ip = default_route_ip()
    if route_ip:
        ips.append(route_ip)
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return _unique([Interface(ip=ip, name="（不明）") for ip in ips]), False


def default_route_ip() -> str | None:
    """既定の経路で使われる送信元アドレス。UDPの connect は実際にはパケットを送らない。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # RFC 5737 の文書用アドレス
            return s.getsockname()[0]
    except OSError:
        return None


def _unique(interfaces: list[Interface]) -> list[Interface]:
    seen: dict[str, Interface] = {}
    for iface in interfaces:
        seen.setdefault(iface.ip, iface)
    return list(seen.values())


def _windows_interfaces() -> list[Interface] | None:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_QUERY],
            capture_output=True,
            timeout=20,
            check=True,
            # ターミナルのないアプリから呼んでも、PowerShellの黒い窓を出さない
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return parse_windows_json(result.stdout.decode("utf-8-sig", "replace"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def parse_windows_json(text: str) -> list[Interface]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return [
        Interface(
            ip=item["ip"],
            name=item.get("name") or "",
            description=item.get("description") or "",
            has_gateway=bool(item.get("gateway")),
            category=item.get("category") or "",
        )
        for item in data
        if _is_ipv4(item.get("ip", ""))
    ]


def _ifconfig_interfaces() -> list[Interface] | None:
    command = shutil.which("ifconfig") or ("/sbin/ifconfig" if os.path.exists("/sbin/ifconfig") else None)
    if command is None:
        return None
    try:
        result = subprocess.run([command, "-a"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    interfaces = parse_ifconfig(result.stdout.decode("utf-8", "replace"))
    route_ip = default_route_ip()
    # ifconfig にはゲートウェイの情報がないため、既定の経路で使われるアドレスを目安にする
    return [
        Interface(ip=i.ip, name=i.name, has_gateway=i.ip == route_ip) for i in interfaces
    ]


def parse_ifconfig(text: str) -> list[Interface]:
    """macOS（BSD）形式の ifconfig 出力から、UP状態のIPv4アドレスを取り出す。"""
    interfaces = []
    name = ""
    up = False
    for line in text.splitlines():
        header = re.match(r"^([^\s:]+): flags=\w+<([^>]*)>", line)
        if header:
            name = header.group(1)
            up = "UP" in header.group(2).split(",")
            continue
        inet = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if inet and name and up:
            interfaces.append(Interface(ip=inet.group(1), name=name))
    return interfaces


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


class PortUnavailableError(Exception):
    pass


def _is_port_unavailable(error: OSError) -> bool:
    codes = {errno.EADDRINUSE, errno.EACCES}
    # Windowsのソケットのエラー番号（使用中 / Hyper-Vなどによる予約）
    winerror = getattr(error, "winerror", None)
    return error.errno in codes or winerror in (10048, 10013)


def bind_socket(ip: str, port: int, search: bool) -> socket.socket:
    """指定のアドレスで待ち受けるソケットを作る。search=True なら空きポートを探す。"""
    ports = range(port, min(port + 50, 65536)) if search else [port]
    for candidate in ports:
        try:
            return socket.create_server((ip, candidate))
        except OSError as e:
            if not _is_port_unavailable(e):
                raise
    if search:
        # 近くに空きがなければ、OSに空きポートを選ばせる
        return socket.create_server((ip, 0))
    raise PortUnavailableError(port)
