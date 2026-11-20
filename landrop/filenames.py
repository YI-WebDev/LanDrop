from __future__ import annotations

import os
import re
import unicodedata
from urllib.parse import quote

FALLBACK_NAME = "file"

_COMMON_FILESYSTEM_NAME_LIMIT_BYTES = 255
_SUFFIX_ROOM_BYTES = 55
MAX_NAME_BYTES = _COMMON_FILESYSTEM_NAME_LIMIT_BYTES - _SUFFIX_ROOM_BYTES
_MAX_EXT_BYTES = 32

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_EXTENSION_SPOOFING_BIDI_CONTROLS = re.compile("[؜‎‏‪-‮⁦-⁩]")
_PATH_SEPARATORS = re.compile(r"[/\\]")

_DEVICE_DIGITS = "0123456789¹²³"
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{d}" for d in _DEVICE_DIGITS}
    | {f"LPT{d}" for d in _DEVICE_DIGITS}
)


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = _PATH_SEPARATORS.split(name)[-1]
    name = _EXTENSION_SPOOFING_BIDI_CONTROLS.sub("", name)
    name = _INVALID_CHARS.sub("_", name)
    name = _strip_leading_dots_and_spaces(name)
    name = _strip_suffix_windows_drops(name)
    if not name:
        return FALLBACK_NAME
    if is_reserved_name(name):
        name = "_" + name
    return _truncate(name)


def _strip_leading_dots_and_spaces(name: str) -> str:
    return name.lstrip(" .")


def _strip_suffix_windows_drops(name: str) -> str:
    return name.rstrip(" .")


def is_reserved_name(name: str) -> bool:
    base = name.split(".", 1)[0].rstrip(" ")
    return base.upper() in _RESERVED_NAMES


def numbered_name(name: str, number: int) -> str:
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
    name = unicodedata.normalize("NFC", name)
    ascii_fallback = "".join(
        c if " " <= c <= "~" and c not in '"\\' else "_" for c in name
    )
    encoded = quote(name, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"
