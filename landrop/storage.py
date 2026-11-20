from __future__ import annotations

import os
import secrets
import shutil
import stat
import unicodedata
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from landrop.filenames import numbered_name, sanitize_filename

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
    name: str
    size: int
    received: int = 0


def list_files(folder: Path) -> list[FileInfo]:
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
    if not name or _has_path_element(name):
        return None
    target = unicodedata.normalize("NFC", name)
    root = folder.resolve()
    with os.scandir(folder) as entries:
        for entry in entries:
            if unicodedata.normalize("NFC", entry.name) != target or not _is_listable(entry):
                continue
            path = Path(entry.path)
            path_escapes_folder = path.resolve().parent != root
            if path_escapes_folder:
                return None
            return path
    return None


def _has_path_element(name: str) -> bool:
    return name in (".", "..") or any(c in name for c in "/\\\x00")


def _is_listable(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith(".") or entry.is_symlink():
        return False
    if not entry.is_file(follow_symlinks=False):
        return False
    st = entry.stat(follow_symlinks=False)
    windows_attributes = getattr(st, "st_file_attributes", 0)
    if windows_attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
        return False
    macos_flags = getattr(st, "st_flags", 0)
    return not macos_flags & stat.UF_HIDDEN


def _hidden_part_name(name: str) -> str:
    return f".{name}.landrop-{secrets.token_hex(8)}.part"


@contextmanager
def _exclusive_part_file(part: Path) -> Iterator[BinaryIO]:
    part_file = part.open("xb", buffering=_WRITE_BUFFER_BYTES)
    try:
        yield part_file
    finally:
        part_file.close()
        part.unlink(missing_ok=True)


class UploadError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class Receiver:
    def __init__(self, folder: Path, max_size: int) -> None:
        self.folder = folder
        self.max_size = max_size
        self.in_progress: list[Incoming] = []
        self._reserved_bytes = 0

    def check_capacity(self, size: int) -> None:
        if size > self.max_size:
            raise UploadError(413, "ファイルサイズが上限を超えています。")
        free = shutil.disk_usage(self.folder).free
        if free - self._reserved_bytes - size < DISK_RESERVE_BYTES:
            raise UploadError(507, "PCの保存先の空き容量が不足しています。")

    async def receive(self, name: str, size: int, chunks: AsyncIterator[bytes]) -> str:
        safe_name = sanitize_filename(name)
        self.check_capacity(size)
        part = self._inside(self.folder / _hidden_part_name(safe_name))
        with _exclusive_part_file(part) as part_file:
            self._reserved_bytes += size
            incoming = Incoming(safe_name, size)
            self.in_progress.append(incoming)
            try:
                written = 0
                with part_file as f:
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
                self._reserved_bytes -= size
                self.in_progress.remove(incoming)

    def _commit(self, part: Path, name: str) -> Path:
        for number in range(_MAX_NUMBERED_NAMES):
            candidate = self._inside(self.folder / numbered_name(name, number))
            try:
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
