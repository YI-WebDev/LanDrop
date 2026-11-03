"""ファイル名の安全化（Windows予約名・使用できない文字・NFC正規化）。"""

from __future__ import annotations

import unicodedata

import pytest

from landrop.filenames import MAX_NAME_BYTES, is_reserved_name, numbered_name, sanitize_filename


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("写真.jpg", "写真.jpg"),
        ("家族 👨‍👩‍👧 旅行.png", "家族 👨‍👩‍👧 旅行.png"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("Nul.tar.gz", "_Nul.tar.gz"),
        ("AUX .txt", "_AUX .txt"),
        ("COM1.log", "_COM1.log"),
        ("LPT9", "_LPT9"),
        ("COM¹.txt", "_COM¹.txt"),
        ("CONOUT$", "_CONOUT$"),
        ("COM10.txt", "COM10.txt"),
        ("CONSOLE.txt", "CONSOLE.txt"),
        ("NULL", "NULL"),
        ('a<b>c:d"e|f?g*h.txt', "a_b_c_d_e_f_g_h.txt"),
        ("tab\there\x00.txt", "tab_here_.txt"),
        ("末尾のドット...", "末尾のドット"),
        ("末尾の空白   ", "末尾の空白"),
        ("file. . .", "file"),
        (".bashrc", "bashrc"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        ("folder/", "file"),
        ("", "file"),
        ("...", "file"),
        ("photo‮gpj.exe", "photogpj.exe"),
    ],
)
def test_sanitize_filename(name: str, expected: str) -> None:
    assert sanitize_filename(name) == expected


def test_sanitize_normalizes_to_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "ポケモン・がっこう.txt")
    result = sanitize_filename(nfd)

    assert result == unicodedata.normalize("NFC", nfd)
    assert unicodedata.is_normalized("NFC", result)


def test_long_names_are_truncated_keeping_extension() -> None:
    result = sanitize_filename("あ" * 200 + ".jpeg")

    assert len(result.encode("utf-8")) <= MAX_NAME_BYTES
    assert result.endswith(".jpeg")
    assert result.startswith("あ")


def test_reserved_check_uses_part_before_first_dot() -> None:
    assert is_reserved_name("prn.anything.here")
    assert is_reserved_name("aux")
    assert not is_reserved_name("auxiliary.txt")


def test_numbered_name() -> None:
    assert numbered_name("写真.jpg", 0) == "写真.jpg"
    assert numbered_name("写真.jpg", 1) == "写真 (1).jpg"
    assert numbered_name("README", 2) == "README (2)"
    assert numbered_name("archive.tar.gz", 3) == "archive.tar (3).gz"
