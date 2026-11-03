"""LanDrop を、ダブルクリックで起動できるアプリにまとめる。

    python -m pip install -r requirements-build.txt
    python packaging/build.py

Windowsでは dist/LanDrop.exe（1ファイル）、macOSでは dist/LanDrop.app ができる。
それぞれのOSの上で実行する（WindowsでmacOS用は作れない）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "packaging"
# 途中のファイルは、OneDriveなどの同期でロックされないよう一時フォルダに置く
WORK = Path(tempfile.gettempdir()) / "landrop-build"


def main() -> None:
    args = [
        str(HERE / "landrop_app.py"),
        "--name",
        "LanDrop",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        # 画面のHTML・CSS・JavaScript。landrop/app.py からは landrop/static として見える
        "--add-data",
        f"{ROOT / 'landrop' / 'static'}{os.pathsep}landrop/static",
        # Uvicorn は設定の文字列から部品を読み込むため、まとめて含めておく
        "--collect-submodules",
        "uvicorn",
        # PNGからOSごとのアイコン形式への変換には Pillow を使う
        "--icon",
        str(HERE / "icon.png"),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
    ]
    if sys.platform == "darwin":
        # ターミナルを開かない .app にする（表示は ~/LanDrop/LanDrop.log に残る）
        args += ["--windowed", "--osx-bundle-identifier", "local.landrop.app"]
    else:
        # Windowsでは1つの exe にする。黒い窓に、QRコードや停止の案内を表示する
        args += ["--onefile"]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    main()
