from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "packaging"
WORK_DIR = Path(tempfile.gettempdir()) / "landrop-build"


def main() -> None:
    work_dir = WORK_DIR
    static_files = [
        "--add-data",
        f"{ROOT / 'landrop' / 'static'}{os.pathsep}landrop/static",
    ]
    uvicorn_submodules = ["--collect-submodules", "uvicorn"]
    zeroconf_submodules = ["--collect-submodules", "zeroconf"]
    icon = ["--icon", str(HERE / "icon.png")]
    args = [
        str(HERE / "landrop_app.py"),
        "--name",
        "LanDrop",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        *static_files,
        *uvicorn_submodules,
        *zeroconf_submodules,
        *icon,
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
    ]
    no_console_window = ["--windowed"]
    args += no_console_window
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "local.landrop.app"]
    else:
        args += ["--onefile"]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    main()
