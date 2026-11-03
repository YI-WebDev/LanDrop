"""フォルダの一覧とダウンロード対象の解決、受信ファイルの保存。"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from landrop.filenames import numbered_name, sanitize_filename

# 受信後もPCに最低限残しておく空き容量
DISK_RESERVE_BYTES = 256 * 1024 * 1024
_WRITE_BUFFER_BYTES = 1024 * 1024
_MAX_NUMBERED_NAMES = 10_000


@dataclass(frozen=True)
class FileInfo:
    name: str
    size: int
    modified: float


@dataclass
class Incoming:
    """受信中のファイル。PCの画面に進み具合を表示する。"""

    name: str
    size: int
    received: int = 0


def list_files(folder: Path) -> list[FileInfo]:
    """フォルダ直下のファイルを新しい順に返す。"""
    files = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not _is_listable(entry):
                continue
            st = entry.stat(follow_symlinks=False)
            name = unicodedata.normalize("NFC", entry.name)
            files.append(FileInfo(name=name, size=st.st_size, modified=st.st_mtime))
    files.sort(key=lambda f: (-f.modified, f.name))
    return files


def find_file(folder: Path, name: str) -> Path | None:
    """一覧に表示されるファイルの中から、名前が一致するものを返す。

    パス要素を含む名前は受け付けず、ディレクトリの実際の中身と照合するため、
    「../」や絶対パス、NTFSの代替データストリーム（a.txt:stream）では一致しない。
    """
    if not name or _has_path_element(name):
        return None
    target = unicodedata.normalize("NFC", name)
    root = folder.resolve()
    with os.scandir(folder) as entries:
        for entry in entries:
            if unicodedata.normalize("NFC", entry.name) != target or not _is_listable(entry):
                continue
            path = Path(entry.path)
            # 念のため、解決後のパスがフォルダ直下にあることを確認する
            if path.resolve().parent != root:
                return None
            return path
    return None


def _has_path_element(name: str) -> bool:
    return name in (".", "..") or any(c in name for c in "/\\\x00")


def _is_listable(entry: os.DirEntry[str]) -> bool:
    """シンボリックリンク・隠しファイル・フォルダを除いた通常のファイルかどうか。"""
    if entry.name.startswith(".") or entry.is_symlink():
        return False
    if not entry.is_file(follow_symlinks=False):
        return False
    st = entry.stat(follow_symlinks=False)
    # Windowsの「隠しファイル」「システムファイル」属性
    attributes = getattr(st, "st_file_attributes", 0)
    if attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
        return False
    # macOSの「隠す」フラグ
    return not getattr(st, "st_flags", 0) & stat.UF_HIDDEN


class UploadError(Exception):
    """受信を中止したときの理由。status_code はHTTPレスポンスにそのまま使う。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class Receiver:
    """受信データを .part ファイルへ少しずつ書き込み、完了後に正式な名前へ変更する。"""

    def __init__(self, folder: Path, max_size: int) -> None:
        self.folder = folder
        self.max_size = max_size
        self.in_progress: list[Incoming] = []
        # 同時に受信中のファイルが使う予定の容量
        self._reserved_bytes = 0

    def check_capacity(self, size: int) -> None:
        if size > self.max_size:
            raise UploadError(413, "ファイルサイズが上限を超えています。")
        free = shutil.disk_usage(self.folder).free
        if free - self._reserved_bytes - size < DISK_RESERVE_BYTES:
            raise UploadError(507, "PCの保存先の空き容量が不足しています。")

    async def receive(self, name: str, size: int, chunks: AsyncIterator[bytes]) -> str:
        """データを受信して保存し、保存したファイル名を返す。"""
        safe_name = sanitize_filename(name)
        self.check_capacity(size)
        # 先頭を「.」にして、受信中のファイルが一覧に出ないようにする
        part = self._inside(self.folder / f".{safe_name}.landrop-{secrets.token_hex(8)}.part")
        # 作成に失敗したときに他人の .part を消さないよう、try の外で排他的に作成する
        f = part.open("xb", buffering=_WRITE_BUFFER_BYTES)
        self._reserved_bytes += size
        incoming = Incoming(safe_name, size)
        self.in_progress.append(incoming)
        try:
            written = 0
            with f:
                async for chunk in chunks:
                    written += len(chunk)
                    if written > size:
                        raise UploadError(400, "受信したデータが申告されたサイズより大きいです。")
                    f.write(chunk)
                    incoming.received = written
            if written != size:
                raise UploadError(400, "受信したデータが途中で終わりました。")
            return self._commit(part, safe_name).name
        finally:
            # 切断・エラー・停止のいずれでも .part を残さない（正常完了時は移動済み）
            self._reserved_bytes -= size
            self.in_progress.remove(incoming)
            part.unlink(missing_ok=True)

    def _commit(self, part: Path, name: str) -> Path:
        """同名ファイルを上書きしないよう、空いている名前を排他的に確保してから移動する。"""
        for number in range(_MAX_NUMBERED_NAMES):
            candidate = self._inside(self.folder / numbered_name(name, number))
            try:
                # "x" は既に存在すれば失敗するため、同時アップロードでも同じ名前を取り合わない
                candidate.open("xb").close()
            except FileExistsError:
                continue
            except PermissionError:
                if candidate.exists():
                    continue
                raise
            try:
                os.replace(part, candidate)
            except BaseException:
                candidate.unlink(missing_ok=True)
                raise
            return candidate
        raise UploadError(409, "同じ名前のファイルが多すぎるため保存できません。")

    def _inside(self, path: Path) -> Path:
        if path.resolve().parent != self.folder.resolve():
            raise UploadError(400, "ファイル名が不正です。")
        return path
