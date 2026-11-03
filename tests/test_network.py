"""LANのIPアドレスの選び方と、ポートの確保。"""

from __future__ import annotations

import socket

import pytest

from landrop import network
from landrop.network import Interface, choose_interface, exclusion_reason

WIFI = Interface("192.168.10.20", "Wi-Fi", "Intel(R) Wi-Fi 6E AX211 160MHz", has_gateway=True, category="Private")
ETHERNET = Interface("192.168.10.21", "イーサネット", "Realtek PCIe GbE Family Controller", has_gateway=True)
WSL = Interface("172.27.112.1", "vEthernet (WSL (Hyper-V firewall))", "Hyper-V Virtual Ethernet Adapter")
HYPERV = Interface("172.20.48.1", "vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter #2")
VBOX = Interface("192.168.56.1", "イーサネット 3", "VirtualBox Host-Only Ethernet Adapter")
VMWARE = Interface("192.168.80.1", "VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8")
DOCKER = Interface("172.17.0.1", "docker0")
OPENVPN = Interface("10.8.0.2", "ローカル エリア接続 2", "TAP-Windows Adapter V9")
WIREGUARD = Interface("10.66.66.2", "wg0", "WireGuard Tunnel")
TAILSCALE = Interface("100.101.102.103", "Tailscale", "Tailscale Tunnel")
MAC_VPN = Interface("10.0.0.5", "utun3")
MAC_BRIDGE = Interface("192.168.64.1", "bridge100")
LOOPBACK = Interface("127.0.0.1", "Loopback Pseudo-Interface 1")
LINK_LOCAL = Interface("169.254.10.10", "Wi-Fi")


@pytest.mark.parametrize(
    "iface",
    [WSL, HYPERV, VBOX, VMWARE, DOCKER, OPENVPN, WIREGUARD, TAILSCALE, MAC_VPN, MAC_BRIDGE, LOOPBACK, LINK_LOCAL],
)
def test_virtual_and_unusable_adapters_are_excluded(iface: Interface) -> None:
    assert exclusion_reason(iface) is not None


@pytest.mark.parametrize(
    "iface", [WIFI, ETHERNET, Interface("10.0.1.5", "en0"), Interface("172.16.0.9", "Ethernet")]
)
def test_physical_lan_adapters_are_usable(iface: Interface) -> None:
    assert exclusion_reason(iface) is None


def test_public_address_is_not_auto_selected() -> None:
    assert exclusion_reason(Interface("8.8.8.8", "Ethernet")) == "プライベートアドレスではない"


def test_single_lan_adapter_is_chosen_among_virtual_ones() -> None:
    interfaces = [LOOPBACK, WSL, HYPERV, VBOX, TAILSCALE, WIFI, LINK_LOCAL]
    assert choose_interface(interfaces, route_ip=TAILSCALE.ip) == WIFI


def test_gateway_decides_between_two_lan_adapters() -> None:
    no_gateway = Interface("192.168.100.5", "イーサネット 2")
    assert choose_interface([no_gateway, WIFI], route_ip=None) == WIFI


def test_route_ip_decides_when_gateway_is_unknown() -> None:
    a, b = Interface("192.168.1.5", "en0"), Interface("192.168.2.5", "en1")
    assert choose_interface([a, b], route_ip="192.168.2.5") == b


def test_ambiguous_when_two_adapters_have_gateways() -> None:
    assert choose_interface([WIFI, ETHERNET], route_ip=None) is None


def test_none_when_only_virtual_adapters() -> None:
    assert choose_interface([LOOPBACK, WSL, VBOX], route_ip=None) is None


def test_candidates_are_sorted_usable_first() -> None:
    ordered = network.sort_candidates([WSL, Interface("192.168.100.5", "イーサネット 2"), WIFI])
    assert ordered[0] == WIFI
    assert ordered[-1] == WSL


def test_parse_windows_json() -> None:
    text = (
        '[{"ip":"192.168.10.108","name":"イーサネット","description":"Realtek PCIe GbE",'
        '"gateway":true,"category":"Public"},'
        '{"ip":"127.0.0.1","name":"Loopback Pseudo-Interface 1","description":"","gateway":false,"category":""}]'
    )
    interfaces = network.parse_windows_json(text)

    assert interfaces[0] == Interface("192.168.10.108", "イーサネット", "Realtek PCIe GbE", True, "Public")
    assert interfaces[1].ip == "127.0.0.1"
    # アドレスが1つだけのときは配列ではなくオブジェクトで返ってくる
    single = network.parse_windows_json('{"ip":"10.0.0.2","name":"Wi-Fi","gateway":true}')
    assert single == [Interface("10.0.0.2", "Wi-Fi", "", True, "")]


def test_parse_ifconfig() -> None:
    text = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 3c:22:fb:00:00:00
\tinet6 fe80::1c%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tstatus: active
en1: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.9.9 netmask 0xffffff00 broadcast 192.168.9.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.0.0.5 --> 10.0.0.5 netmask 0xffffffff
"""
    interfaces = network.parse_ifconfig(text)

    assert [(i.name, i.ip) for i in interfaces] == [
        ("lo0", "127.0.0.1"),
        ("en0", "192.168.1.23"),
        ("utun3", "10.0.0.5"),
    ]
    assert choose_interface(interfaces, route_ip="10.0.0.5") == interfaces[1]


def test_bind_socket_finds_free_port_when_default_is_in_use() -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        with network.bind_socket("127.0.0.1", port, search=True) as sock:
            assert sock.getsockname()[1] != port


def test_bind_socket_fails_for_explicit_port_in_use() -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        with pytest.raises(network.PortUnavailableError):
            network.bind_socket("127.0.0.1", port, search=False)
