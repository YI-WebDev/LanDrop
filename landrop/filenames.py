"""ファイル名の正規化・安全化と、ダウンロード用ヘッダーの生成。"""

from __future__ import annotations

import os
import re
import unicodedata
from urllib.parse import quote

# 名前を決められなかったときの代わりの名前
FALLBACK_NAME = "file"

# 連番や .part を付けても多くのファイルシステムの上限(255バイト)に収まるよう余裕を持たせる
MAX_NAME_BYTES = 200
_MAX_EXT_BYTES = 32

# Windowsで使えない文字と制御文字
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# 表示上の文字の向きを変えて拡張子を偽装できる双方向制御文字
_BIDI_CONTROLS = re.compile("[؜‎‏‪-‮⁦-⁩]")
_PATH_SEPARATORS = re.compile(r"[/\\]")

# Windowsの予約デバイス名（拡張子が付いていても予約扱いになる）
_DEVICE_DIGITS = "0123456789¹²³"
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{d}" for d in _DEVICE_DIGITS}
    | {f"LPT{d}" for d in _DEVICE_DIGITS}
)


def sanitize_filename(name: str) -> str:
    """受信したファイル名を、どのOSでも安全に保存できる名前に変換する。"""
    name = unicodedata.normalize("NFC", name)
    # パス要素を除去し、最後の要素だけを使う
    name = _PATH_SEPARATORS.split(name)[-1]
    name = _BIDI_CONTROLS.sub("", name)
    name = _INVALID_CHARS.sub("_", name)
    # 先頭のドットは隠しファイル化を、末尾のドットと空白はWindowsでの名前の食い違いを防ぐため除去する
    name = name.strip(" .")
    if not name:
        return FALLBACK_NAME
    if is_reserved_name(name):
        name = "_" + name
    return _truncate(name)


def is_reserved_name(name: str) -> bool:
    """Windowsの予約名（CON、NUL、COM1など）かどうか。"""
    base = name.split(".", 1)[0].rstrip(" ")
    return base.upper() in _RESERVED_NAMES


def numbered_name(name: str, number: int) -> str:
    """「写真.jpg」から「写真 (1).jpg」のような連番付きの名前を作る。"""
    if number == 0:
        return name
    stem, ext = os.path.splitext(name)
    return f"{stem} ({number}){ext}"


def _truncate(name: str) -> str:
    if len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > _MAX_EXT_BYTES:
        stem, ext = name, ""
    budget = MAX_NAME_BYTES - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
    return (stem or FALLBACK_NAME) + ext


def content_disposition(name: str) -> str:
    """ダウンロード用の Content-Disposition（RFC 6266 / RFC 5987 形式）を作る。"""
    name = unicodedata.normalize("NFC", name)
    # filename* に対応していない古いクライアント向けのASCII版
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\' else "_" for c in name)
    encoded = quote(name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
