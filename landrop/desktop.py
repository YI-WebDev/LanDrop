"""PCのデスクトップとの連携（PCの画面を開く、エクスプローラーやFinderで表示する）。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


def open_app_window(url: str) -> None:
    """アドレスバーのないアプリ用の窓で開く。EdgeやChromeがなければ、いつものブラウザで開く。"""
    command = _app_window_command()
    if command:
        try:
            subprocess.Popen([*command, f"--app={url}"])
            return
        except OSError:
            pass
    webbrowser.open(url)


def _app_window_command() -> list[str] | None:
    """アプリ用の窓（--app）を開ける、Chromium系のブラウザを起動するコマンド。"""
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
                # 起動済みのブラウザにも引数を渡せるよう、-n で新しく起動させる
                return ["open", "-na", app, "--args"]
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium"):
            exe = shutil.which(name)
            if exe:
                return [exe]
    return None


def reveal(path: Path) -> None:
    """ファイルを選んだ状態で、エクスプローラー（macOSはFinder）を開く。"""
    if sys.platform == "win32":
        # Windowsのパスには「"」を使えないため、そのまま囲んでよい
        subprocess.Popen(f'explorer.exe /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_folder(path.parent)


def alert(title: str, message: str) -> None:
    """画面のないアプリとして起動したときに、エラーを知らせる。"""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # type: ignore[attr-defined]
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
