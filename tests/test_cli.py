"""起動オプションとターミナル表示。"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import threading
import types

import pytest
import qrcode

from landrop import cli
from landrop.idle import IdleTracker, StopRequest
from landrop.network import Interface
from landrop.qr import render_qr, render_qr_svg


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10GB", 10 * 1024**3),
        ("500MB", 500 * 1024**2),
        ("1.5G", int(1.5 * 1024**3)),
        ("2gib", 2 * 1024**3),
        ("64k", 64 * 1024),
        ("1024", 1024),
        ("1TB", 1024**4),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert cli.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "-1GB", "0", "10 XB", "1e3"])
def test_parse_size_rejects_invalid(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_size(text)


def test_defaults() -> None:
    args = cli.build_parser().parse_args([])

    assert args.ip is None
    assert args.port is None
    assert args.max_size == 10 * 1024**3
    assert args.timeout == 30
    assert args.share == cli.DEFAULT_SHARE
    assert cli.DEFAULT_SHARE.parts[-2:] == ("LanDrop", "share")
    assert cli.DEFAULT_RECEIVED.parts[-2:] == ("LanDrop", "received")
    assert cli.DEFAULT_PORT == 8765


def test_options() -> None:
    args = cli.build_parser().parse_args(
        ["--ip", "192.168.1.10", "--port", "9000", "--max-size", "2GB", "--timeout", "0"]
    )

    assert (args.ip, args.port, args.max_size, args.timeout) == ("192.168.1.10", 9000, 2 * 1024**3, 0)


@pytest.mark.parametrize("argv", [["--port", "0"], ["--port", "70000"], ["--timeout", "-1"], ["--timeout", "x"]])
def test_invalid_options_exit(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def test_format_size() -> None:
    assert cli.format_size(10 * 1024**3) == "10 GB"
    assert cli.format_size(1536 * 1024) == "1.5 MB"
    assert cli.format_size(12) == "12 B"


@pytest.mark.parametrize("ip", ["0.0.0.0", "255.255.255.255", "224.0.0.1", "abc", "192.168.1"])
def test_unusable_ip_option_is_rejected(ip: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._validate_ip(ip, []) is None
    assert capsys.readouterr().out


def test_ip_option_warns_for_virtual_adapter(capsys: pytest.CaptureFixture[str]) -> None:
    wsl = Interface("172.27.112.1", "vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter")

    assert cli._validate_ip("172.27.112.1", [wsl]) == "172.27.112.1"
    assert "仮想アダプター" in capsys.readouterr().out


def test_ambiguous_ip_lists_candidates_when_not_interactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(cli.network, "default_route_ip", lambda: None)
    interfaces = [
        Interface("192.168.1.5", "Wi-Fi", has_gateway=True),
        Interface("192.168.2.5", "イーサネット", has_gateway=True),
        Interface("127.0.0.1", "Loopback"),
    ]

    assert cli._select_ip(interfaces, detailed=True) is None
    out = capsys.readouterr().out
    assert "192.168.1.5" in out and "192.168.2.5" in out
    assert "127.0.0.1" not in out
    assert "--ip" in out


def test_ambiguous_ip_can_be_chosen_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(cli.network, "default_route_ip", lambda: None)
    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    interfaces = [
        Interface("192.168.1.5", "Wi-Fi", has_gateway=True),
        Interface("192.168.2.5", "イーサネット", has_gateway=True),
    ]

    assert cli._select_ip(interfaces, detailed=True) == "192.168.2.5"


def test_public_network_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.os, "name", "nt")
    public = Interface("192.168.1.5", "Wi-Fi", category="Public")
    private = Interface("192.168.1.6", "イーサネット", category="Private")

    assert "パブリック" in cli._profile_warnings("192.168.1.5", [public], detailed=True)[0]
    assert cli._profile_warnings("192.168.1.6", [private], detailed=True) == []
    # 確認できなくても起動は続け、その旨だけを伝える
    assert "確認できません" in cli._profile_warnings("192.168.1.7", [], detailed=False)[0]


def test_invalid_http_request_warning_becomes_one_japanese_notice(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = logging.getLogger("tests.uvicorn.error")
    notice = cli._InvalidRequestNotice()
    logger.addFilter(notice)
    try:
        for _ in range(3):
            logger.warning("Invalid HTTP request received.")
        logger.warning("ほかの警告")
    finally:
        logger.removeFilter(notice)

    # 英語の警告は出さず、ほかの警告はそのまま通す
    assert [r.getMessage() for r in caplog.records] == ["ほかの警告"]
    out = capsys.readouterr().out
    assert out.count("[お知らせ]") == 1
    assert "http://" in out


def _decode(rendered: str) -> list[list[bool]]:
    """描画したQRコードを、暗いマス = True の行列に戻す。"""
    halves = {"█": (False, False), "▀": (False, True), "▄": (True, False), " ": (True, True)}
    rows: list[list[bool]] = []
    for line in re.sub(r"\x1b\[[0-9;]*m", "", rendered).splitlines():
        pairs = [halves[c] for c in line]
        rows.append([top for top, _ in pairs])
        rows.append([bottom for _, bottom in pairs])
    return rows


@pytest.mark.parametrize("color", [True, False])
def test_qr_rendering_matches_qr_matrix(color: bool) -> None:
    url = "http://192.168.100.200:65535/auth?t=" + "A" * 43
    rendered = render_qr(url, color=color)

    expected = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    expected.add_data(url)
    expected.make(fit=True)
    matrix = expected.get_matrix()

    assert _decode(rendered)[: len(matrix)] == matrix
    assert ("\x1b[" in rendered) is color


def test_qr_svg_draws_every_dark_module() -> None:
    url = "http://192.168.1.5:8765/auth?t=" + "B" * 43
    svg = render_qr_svg(url)

    expected = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    expected.add_data(url)
    expected.make(fit=True)
    matrix = expected.get_matrix()

    assert f'viewBox="0 0 {len(matrix)} {len(matrix)}"' in svg
    assert svg.count("h1v1h-1z") == sum(cell for row in matrix for cell in row)


def test_no_open_option() -> None:
    assert cli.build_parser().parse_args([]).no_open is False
    assert cli.build_parser().parse_args(["--no-open"]).no_open is True


def test_pc_screen_shares_the_socket_when_listening_on_loopback() -> None:
    sock = socket.create_server(("127.0.0.1", 0))
    try:
        assert cli._bind_pc_socket("127.0.0.1", sock) is sock
    finally:
        sock.close()


def test_watcher_stops_server_on_request(capsys: pytest.CaptureFixture[str]) -> None:
    server = types.SimpleNamespace(should_exit=False)
    stop = StopRequest()
    stop.request("PCの画面から停止しました。")

    cli._watch(server, IdleTracker(0), stop, threading.Event(), 30)  # type: ignore[arg-type]

    assert server.should_exit is True
    assert "PCの画面から停止しました。" in capsys.readouterr().out
