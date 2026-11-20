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

_RFC1918_PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")

_VIRTUAL_KEYWORDS = (
    "virtual", "hyper-v", "vethernet", "wsl", "docker", "vmware", "virtualbox", "host-only",
    "vpn", "tap-windows", "wintun", "wireguard", "tailscale", "zerotier", "openvpn",
    "anyconnect", "globalprotect", "pangp", "fortinet", "forticlient", "loopback",
    "pseudo-interface", "bluetooth", "npcap", "teredo", "isatap",
)
_NON_PHYSICAL_INTERFACE_NAME = re.compile(
    r"(?:lo|utun|ipsec|ppp|bridge|vmenet|vmnet|vboxnet|awdl|llw|gif|stf|anpi|ap|feth"
    r"|docker|virbr|tun|tap|wg|tailscale)\d*|(?:br-|veth|zt).*",
    re.IGNORECASE,
)

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
    windows_network_category: str = ""

    @property
    def label(self) -> str:
        if self.description and self.description != self.name:
            return f"{self.name}（{self.description}）"
        return self.name


def exclusion_reason(iface: Interface) -> str | None:
    addr = ipaddress.IPv4Address(iface.ip)
    if addr.is_loopback:
        return "ループバック"
    if addr.is_link_local:
        return "リンクローカル（DHCPで取得できていない）"
    if addr in _CGNAT_NETWORK:
        return "VPNの可能性"
    text = f"{iface.name} {iface.description}".lower()
    text_marks_virtual_adapter = any(keyword in text for keyword in _VIRTUAL_KEYWORDS)
    name_is_not_physical_lan = _NON_PHYSICAL_INTERFACE_NAME.fullmatch(iface.name)
    if text_marks_virtual_adapter or name_is_not_physical_lan:
        return "仮想アダプター/VPN"
    if not any(addr in network for network in _RFC1918_PRIVATE_NETWORKS):
        return "プライベートアドレスではない"
    return None


def choose_interface(interfaces: list[Interface], route_ip: str | None) -> Interface | None:
    usable = [i for i in interfaces if exclusion_reason(i) is None]
    if len(usable) == 1:
        return usable[0]
    on_the_way_to_the_internet = [i for i in usable if i.has_gateway or i.ip == route_ip]
    if len(on_the_way_to_the_internet) == 1:
        return on_the_way_to_the_internet[0]
    return None


def sort_candidates(interfaces: list[Interface]) -> list[Interface]:
    return sorted(interfaces, key=lambda i: (exclusion_reason(i) is not None, not i.has_gateway))


def find_interfaces() -> tuple[list[Interface], bool]:
    found = _windows_interfaces() if os.name == "nt" else _ifconfig_interfaces()
    if found is not None:
        return _unique(found), True
    ips_without_adapter_names = []
    route_ip = default_route_ip()
    if route_ip:
        ips_without_adapter_names.append(route_ip)
    try:
        ips_without_adapter_names += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return _unique([Interface(ip=ip, name="（不明）") for ip in ips_without_adapter_names]), False


_DOCUMENTATION_ADDRESS = "192.0.2.1"


def default_route_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((_DOCUMENTATION_ADDRESS, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _unique(interfaces: list[Interface]) -> list[Interface]:
    seen: dict[str, Interface] = {}
    for iface in interfaces:
        seen.setdefault(iface.ip, iface)
    return list(seen.values())


_NO_CONSOLE_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _windows_interfaces() -> list[Interface] | None:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_QUERY],
            capture_output=True,
            timeout=20,
            check=True,
            creationflags=_NO_CONSOLE_WINDOW_FLAG,
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
            windows_network_category=item.get("category") or "",
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
    return [
        Interface(ip=i.ip, name=i.name, has_gateway=i.ip == route_ip)
        for i in interfaces
    ]


def parse_ifconfig(text: str) -> list[Interface]:
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
    winerror = getattr(error, "winerror", None)
    winsock_unavailable_codes = (10048, 10013)
    return error.errno in codes or winerror in winsock_unavailable_codes


def _bind_any_free_port(ip: str) -> socket.socket:
    return socket.create_server((ip, 0))


def bind_socket(ip: str, port: int, search: bool) -> socket.socket:
    ports = range(port, min(port + 50, 65536)) if search else [port]
    for candidate in ports:
        try:
            return socket.create_server((ip, candidate))
        except OSError as e:
            if not _is_port_unavailable(e):
                raise
    if search:
        return _bind_any_free_port(ip)
    raise PortUnavailableError(port)
