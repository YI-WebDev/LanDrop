"""PCのデスクトップとの連携（アプリ用の窓を開く）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from landrop import desktop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windowsのブラウザの場所を前提にしている")

URL = "http://127.0.0.1:8765/pc/auth?t=token"


def _point_program_folders_to(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        monkeypatch.setenv(name, str(root / name))


def test_opens_edge_as_app_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_program_folders_to(monkeypatch, tmp_path)
    edge = tmp_path / "ProgramFiles" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    launched: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", launched.append)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: pytest.fail("通常のブラウザで開いた"))

    desktop.open_app_window(URL)

    assert launched == [[str(edge), f"--app={URL}"]]


def test_falls_back_to_default_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point_program_folders_to(monkeypatch, tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)

    desktop.open_app_window(URL)

    assert opened == [URL]


def test_reveal_quotes_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[str] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", launched.append)

    desktop.reveal(Path(r"C:\Users\a\LanDrop\received\写真, 旅行 (1).jpg"))

    assert launched == [r'explorer.exe /select,"C:\Users\a\LanDrop\received\写真, 旅行 (1).jpg"']
