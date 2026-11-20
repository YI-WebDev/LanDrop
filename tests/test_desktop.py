from __future__ import annotations

import sys
import types
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


class _FakeWebview(types.ModuleType):
    def __init__(self, fail: bool = False) -> None:
        super().__init__("webview")
        self.fail = fail
        self.windows: list[types.SimpleNamespace] = []
        self.started_with: list[object] = []

    def create_window(self, title: str, url: str, **options: object) -> types.SimpleNamespace:
        window = types.SimpleNamespace(title=title, url=url, destroyed=False)
        window.destroy = lambda: setattr(window, "destroyed", True)
        self.windows.append(window)
        return window

    def start(self, gui: object = None) -> None:
        self.started_with.append(gui)
        if self.fail:
            raise RuntimeError("WebView2 がない")


def test_show_window_until_closed_uses_webview2(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert desktop.show_window_until_closed(URL) is True
    assert [(w.title, w.url) for w in fake.windows] == [("LanDrop", URL)]
    assert fake.started_with == ["edgechromium"]


def test_show_window_until_closed_reports_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "webview", _FakeWebview(fail=True))

    assert desktop.show_window_until_closed(URL) is False


def test_close_window_destroys_open_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeWebview()
    fake.create_window("LanDrop", URL)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "window_available", lambda: True)

    desktop.close_window()

    assert fake.windows[0].destroyed is True


def test_window_needs_pywebview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.importlib.util, "find_spec", lambda name: None)

    assert desktop.window_available() is False
    desktop.close_window()
