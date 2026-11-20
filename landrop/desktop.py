from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_TITLE = "LanDrop"
_MESSAGE_BOX_ERROR_ICON = 0x10
_MACOS_NEW_INSTANCE_FLAG = "-na"


def window_available() -> bool:
    return importlib.util.find_spec("webview") is not None


def show_window_until_closed(url: str) -> bool:
    import webview

    webview.create_window(WINDOW_TITLE, url, width=1200, height=820, min_size=(720, 560), text_select=True)
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None)
    except Exception:  # noqa: BLE001
        return False
    return True


def close_window() -> None:
    if not window_available():
        return
    import webview

    for window in list(webview.windows):
        window.destroy()


def open_app_window(url: str) -> None:
    command = _app_window_command()
    if command:
        try:
            subprocess.Popen([*command, f"--app={url}"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def _app_window_command() -> list[str] | None:
    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
        for root in filter(None, roots):
            for relative in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                exe = Path(root) / relative
                if exe.is_file():
                    return [str(exe)]
    elif sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge"):
            if Path(f"/Applications/{app}.app").is_dir():
                return ["open", _MACOS_NEW_INSTANCE_FLAG, app, "--args"]
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium"):
            exe = shutil.which(name)
            if exe:
                return [exe]
    return None


def reveal(path: Path) -> None:
    if sys.platform == "win32":
        quoted_path = f'"{path}"'
        subprocess.Popen(f"explorer.exe /select,{quoted_path}")
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_folder(path.parent)


def alert(title: str, message: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, _MESSAGE_BOX_ERROR_ICON)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        quoted = [text.replace("\\", "\\\\").replace('"', '\\"') for text in (title, message)]
        subprocess.run(["osascript", "-e", f'display alert "{quoted[0]}" message "{quoted[1]}"'], check=False)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
