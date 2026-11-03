"""共有フォルダの一覧とダウンロード（パストラバーサル・シンボリックリンク・隠しファイル）。"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from landrop import storage
from landrop.filenames import content_disposition


def _names(client: TestClient) -> list[str]:
    return [f["name"] for f in client.get("/api/files").json()["files"]]


def test_lists_files_with_size_and_modified_time(client: TestClient, share_dir: Path) -> None:
    old = share_dir / "old.txt"
    old.write_bytes(b"12345")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (share_dir / "new.txt").write_bytes(b"1")
    (share_dir / "folder").mkdir()

    files = client.get("/api/files").json()["files"]

    assert [f["name"] for f in files] == ["new.txt", "old.txt"]
    assert files[1]["size"] == 5
    assert files[1]["modified"] == 1_700_000_000


def test_download_japanese_filename(client: TestClient, share_dir: Path) -> None:
    name = "写真 テスト😀 (旅行).jpg"
    (share_dir / name).write_bytes(b"jpeg-data")

    response = client.get(f"/api/files/{quote(name)}")

    assert response.status_code == 200
    assert response.content == b"jpeg-data"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"filename*=UTF-8''{quote(name, safe='')}" in disposition
    assert response.headers["content-type"] == "application/octet-stream"
    assert name in _names(client)


def test_download_accepts_nfd_request_for_nfc_name(client: TestClient, share_dir: Path) -> None:
    name = unicodedata.normalize("NFC", "がぎぐ.txt")
    (share_dir / name).write_bytes(b"ok")

    nfd = unicodedata.normalize("NFD", name)
    assert client.get(f"/api/files/{quote(nfd)}").content == b"ok"


def test_content_disposition_has_ascii_fallback() -> None:
    assert content_disposition("report.pdf") == (
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    )
    header = content_disposition('写真 "1".jpg')
    assert header.startswith('attachment; filename="__ _1_.jpg"; ')
    assert header.endswith("filename*=UTF-8''%E5%86%99%E7%9C%9F%20%221%22.jpg")
    # ヘッダーインジェクションにつながる改行は残さない
    assert "\r" not in content_disposition("a\r\nSet-Cookie: x.txt")
    assert "\n" not in content_disposition("a\r\nSet-Cookie: x.txt")


@pytest.mark.parametrize(
    "path",
    [
        "/api/files/..%2Fsecret.txt",
        "/api/files/..%5Csecret.txt",
        "/api/files/%2E%2E%5Csecret.txt",
        "/api/files/..",
        "/api/files/%2E%2E",
        "/api/files/sub%2F..%2F..%2Fsecret.txt",
        "/api/files/C:%5C..%5Csecret.txt",
        "/api/files/visible.txt%3A%3A%24DATA",
    ],
)
def test_path_traversal_is_rejected(client: TestClient, share_dir: Path, path: str) -> None:
    (share_dir.parent / "secret.txt").write_bytes(b"TOP-SECRET")
    (share_dir / "visible.txt").write_bytes(b"public")

    response = client.get(path)

    assert response.status_code == 404
    assert b"TOP-SECRET" not in response.content


def test_find_file_rejects_paths(share_dir: Path) -> None:
    secret = share_dir.parent / "secret.txt"
    secret.write_bytes(b"x")
    (share_dir / "ok.txt").write_bytes(b"x")

    assert storage.find_file(share_dir, "ok.txt") == share_dir / "ok.txt"
    for name in ("../secret.txt", "..\\secret.txt", str(secret), "", ".", "..", "ok.txt\x00"):
        assert storage.find_file(share_dir, name) is None


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"この環境ではシンボリックリンクを作成できません: {e}")


def test_symlinks_are_not_listed_or_downloadable(client: TestClient, share_dir: Path) -> None:
    secret = share_dir.parent / "secret.txt"
    secret.write_bytes(b"TOP-SECRET")
    (share_dir / "normal.txt").write_bytes(b"normal")
    _symlink_or_skip(share_dir / "link-outside.txt", secret)
    _symlink_or_skip(share_dir / "link-inside.txt", share_dir / "normal.txt")

    assert _names(client) == ["normal.txt"]
    for name in ("link-outside.txt", "link-inside.txt"):
        response = client.get(f"/api/files/{name}")
        assert response.status_code == 404
        assert b"TOP-SECRET" not in response.content


class _FakeEntry:
    """シンボリックリンクを作れない環境でも判定を確認するための DirEntry 代わり。"""

    def __init__(self, path: Path, symlink: bool) -> None:
        self.name = path.name
        self.path = str(path)
        self._symlink = symlink

    def is_symlink(self) -> bool:
        return self._symlink

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return not self._symlink or follow_symlinks

    def stat(self, follow_symlinks: bool = True) -> os.stat_result:
        return os.stat(self.path)


def test_symlink_entries_are_excluded_by_listing_rule(share_dir: Path) -> None:
    target = share_dir / "target.txt"
    target.write_bytes(b"x")

    assert storage._is_listable(_FakeEntry(target, symlink=False))  # type: ignore[arg-type]
    assert not storage._is_listable(_FakeEntry(target, symlink=True))  # type: ignore[arg-type]


def test_hidden_files_are_excluded(client: TestClient, share_dir: Path) -> None:
    (share_dir / ".hidden.txt").write_bytes(b"x")
    (share_dir / ".DS_Store").write_bytes(b"x")
    (share_dir / "visible.txt").write_bytes(b"x")
    hidden_attr = share_dir / "desktop.ini"
    hidden_attr.write_bytes(b"x")
    if os.name == "nt":
        import ctypes

        # Windowsの「隠しファイル」属性
        assert ctypes.windll.kernel32.SetFileAttributesW(str(hidden_attr), 0x2)
        expected = ["visible.txt"]
    else:
        expected = ["desktop.ini", "visible.txt"]

    assert sorted(_names(client)) == expected
    assert client.get("/api/files/.hidden.txt").status_code == 404
    if os.name == "nt":
        assert client.get("/api/files/desktop.ini").status_code == 404


def test_folders_are_not_downloadable(client: TestClient, share_dir: Path) -> None:
    (share_dir / "folder").mkdir()
    assert client.get("/api/files/folder").status_code == 404


def test_missing_file_returns_404(client: TestClient) -> None:
    response = client.get("/api/files/nothing.txt")
    assert response.status_code == 404
    assert response.json()["detail"] == "ファイルが見つかりません。"
