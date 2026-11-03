"""スマホからPCへの送信（自動改名・サイズ上限・空き容量・切断時の .part 削除）。"""

from __future__ import annotations

import asyncio
import json
import shutil
import unicodedata
from collections import namedtuple
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from landrop import storage
from landrop.app import create_app
from landrop.storage import Receiver, UploadError
from tests.conftest import asgi_request


def _upload(client: TestClient, name: str, data: bytes) -> tuple[int, dict]:
    response = client.post(f"/api/upload?name={quote(name)}", content=data)
    return response.status_code, response.json()


def _files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_upload_japanese_filename(client: TestClient, received_dir: Path) -> None:
    name = "写真 テスト😀 (旅行).jpg"
    status, body = _upload(client, name, b"photo")

    assert status == 201
    assert body["name"] == name
    assert (received_dir / name).read_bytes() == b"photo"


def test_upload_name_is_normalized_to_nfc(client: TestClient, received_dir: Path) -> None:
    nfd = unicodedata.normalize("NFD", "がぎぐ.txt")
    status, body = _upload(client, nfd, b"x")

    assert status == 201
    assert body["name"] == unicodedata.normalize("NFC", nfd)
    assert _files(received_dir) == [unicodedata.normalize("NFC", nfd)]


def test_same_name_is_renamed_instead_of_overwritten(client: TestClient, received_dir: Path) -> None:
    names = [_upload(client, "写真.jpg", f"data{i}".encode())[1]["name"] for i in range(3)]

    assert names == ["写真.jpg", "写真 (1).jpg", "写真 (2).jpg"]
    assert (received_dir / "写真.jpg").read_bytes() == b"data0"
    assert (received_dir / "写真 (2).jpg").read_bytes() == b"data2"


def test_existing_file_on_pc_is_not_overwritten(client: TestClient, received_dir: Path) -> None:
    (received_dir / "memo.txt").write_bytes(b"original")

    assert _upload(client, "memo.txt", b"new")[1]["name"] == "memo (1).txt"
    assert (received_dir / "memo.txt").read_bytes() == b"original"


def test_nfd_and_nfc_names_do_not_become_separate_files(client: TestClient, received_dir: Path) -> None:
    nfc = unicodedata.normalize("NFC", "パン.txt")
    _upload(client, nfc, b"1")
    _upload(client, unicodedata.normalize("NFD", nfc), b"2")

    assert _files(received_dir) == sorted([nfc, "パン (1).txt"])


def test_concurrent_uploads_get_unique_names(received_dir: Path) -> None:
    receiver = Receiver(received_dir, max_size=1024)

    async def chunks(index: int) -> AsyncIterator[bytes]:
        for part in (f"{index}-a".encode(), f"{index}-b".encode()):
            await asyncio.sleep(0)  # 他のアップロードと処理を入れ替える
            yield part

    async def run() -> list[str]:
        sizes = [len(f"{i}-a{i}-b") for i in range(8)]
        return await asyncio.gather(*(receiver.receive("同時.txt", sizes[i], chunks(i)) for i in range(8)))

    names = asyncio.run(run())

    assert len(set(names)) == 8
    assert sorted(names) == sorted(["同時.txt"] + [f"同時 ({i}).txt" for i in range(1, 8)])
    contents = sorted((received_dir / n).read_bytes() for n in names)
    assert contents == sorted(f"{i}-a{i}-b".encode() for i in range(8))
    assert not list(received_dir.glob("*.part"))


def test_path_elements_in_upload_name_are_removed(client: TestClient, received_dir: Path) -> None:
    assert _upload(client, "../../evil.txt", b"1")[1]["name"] == "evil.txt"
    assert _upload(client, "..\\..\\Windows\\win.ini", b"2")[1]["name"] == "win.ini"
    assert _upload(client, "C:\\Users\\a\\b.txt", b"3")[1]["name"] == "b.txt"

    assert _files(received_dir) == ["b.txt", "evil.txt", "win.ini"]
    assert not (received_dir.parent / "evil.txt").exists()


@pytest.mark.parametrize(
    ("name", "saved"),
    [("CON", "_CON"), ("nul.txt", "_nul.txt"), ("COM1.tar.gz", "_COM1.tar.gz"), ("lpt9", "_lpt9")],
)
def test_windows_reserved_names_are_renamed(
    client: TestClient, received_dir: Path, name: str, saved: str
) -> None:
    assert _upload(client, name, b"x")[1]["name"] == saved
    assert (received_dir / saved).read_bytes() == b"x"


def test_empty_name_gets_fallback(client: TestClient, received_dir: Path) -> None:
    assert _upload(client, "", b"x")[1]["name"] == "file"
    assert _upload(client, "...", b"x")[1]["name"] == "file (1)"


def test_empty_file_can_be_sent(client: TestClient, received_dir: Path) -> None:
    assert _upload(client, "empty.txt", b"") == (201, {"name": "empty.txt"})
    assert (received_dir / "empty.txt").read_bytes() == b""


def test_file_over_max_size_is_rejected(
    make_client: Callable[..., TestClient], received_dir: Path
) -> None:
    client = make_client(max_size=10)

    status, body = _upload(client, "big.bin", b"x" * 11)
    assert status == 413
    assert "上限" in body["detail"]
    assert _files(received_dir) == []

    assert _upload(client, "ok.bin", b"x" * 10)[0] == 201


def test_content_length_is_required(client: TestClient, received_dir: Path) -> None:
    def body() -> Iterator[bytes]:
        yield b"chunked"

    response = client.post("/api/upload?name=a.txt", content=body())

    assert response.status_code == 411
    assert _files(received_dir) == []


def test_rejected_when_disk_space_is_low(
    client: TestClient, received_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda _: usage(10**12, 10**12, storage.DISK_RESERVE_BYTES + 5)
    )

    status, body = _upload(client, "a.bin", b"x" * 10)

    assert status == 507
    assert "空き容量" in body["detail"]
    assert _files(received_dir) == []


def test_missing_received_folder_returns_error(client: TestClient, received_dir: Path) -> None:
    received_dir.rmdir()

    status, body = _upload(client, "a.txt", b"x")

    assert status == 500
    assert "書き込めません" in body["detail"]


def test_concurrent_reservations_count_against_free_space(
    received_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: usage(10**12, 0, storage.DISK_RESERVE_BYTES + 150)
    )
    receiver = Receiver(received_dir, max_size=1000)
    started = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        started.set()
        await asyncio.sleep(0.05)
        yield b"x" * 100

    async def run() -> None:
        first = asyncio.create_task(receiver.receive("a.bin", 100, slow()))
        await started.wait()
        # 1つ目が100バイトを予約中のため、残り50バイトでは2つ目の100バイトは入らない
        with pytest.raises(UploadError) as error:
            receiver.check_capacity(100)
        assert error.value.status_code == 507
        await first

    asyncio.run(run())
    assert _files(received_dir) == ["a.bin"]


def _part_files(folder: Path) -> list[Path]:
    return list(folder.glob("*.part"))


def test_disconnect_removes_part_file(make_config: Callable, received_dir: Path) -> None:
    app = create_app(make_config())
    seen_part: list[bool] = []

    def on_receive(call: int) -> None:
        if call == 3:
            # 切断を伝える直前には .part が書き込み途中で存在している
            seen_part.append(bool(_part_files(received_dir)))

    asgi_request(
        app,
        "/api/upload",
        query="name=" + quote("途中で切れる.mp4"),
        headers={"content-length": "300"},
        messages=[
            {"type": "http.request", "body": b"a" * 100, "more_body": True},
            {"type": "http.request", "body": b"b" * 100, "more_body": True},
            {"type": "http.disconnect"},
        ],
        on_receive=on_receive,
    )

    assert seen_part == [True]
    assert _part_files(received_dir) == []
    assert _files(received_dir) == []


def _status_and_body(sent: list[dict]) -> tuple[int, dict]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body)


def test_body_shorter_than_content_length_is_discarded(make_config: Callable, received_dir: Path) -> None:
    sent = asgi_request(
        create_app(make_config()),
        "/api/upload",
        query="name=a.txt",
        headers={"content-length": "100"},
        messages=[{"type": "http.request", "body": b"x" * 50, "more_body": False}],
    )

    assert _status_and_body(sent)[0] == 400
    assert _files(received_dir) == []


def test_body_longer_than_content_length_is_discarded(make_config: Callable, received_dir: Path) -> None:
    sent = asgi_request(
        create_app(make_config()),
        "/api/upload",
        query="name=a.txt",
        headers={"content-length": "10"},
        messages=[
            {"type": "http.request", "body": b"x" * 8, "more_body": True},
            {"type": "http.request", "body": b"x" * 8, "more_body": False},
        ],
    )

    assert _status_and_body(sent)[0] == 400
    assert _files(received_dir) == []


def test_server_stop_during_upload_removes_part_file(received_dir: Path) -> None:
    receiver = Receiver(received_dir, max_size=1000)

    async def endless() -> AsyncIterator[bytes]:
        while True:
            yield b"x" * 10
            await asyncio.sleep(0.01)

    async def run() -> None:
        task = asyncio.create_task(receiver.receive("a.bin", 1000, endless()))
        await asyncio.sleep(0.05)
        assert _part_files(received_dir)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert _files(received_dir) == []


def test_progress_is_visible_while_receiving(received_dir: Path) -> None:
    receiver = Receiver(received_dir, max_size=1000)
    observed: list[list[tuple[str, int, int]]] = []

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 10
            observed.append([(i.name, i.size, i.received) for i in receiver.in_progress])

    asyncio.run(receiver.receive("動画.mp4", 30, chunks()))

    assert observed == [[("動画.mp4", 30, 10)], [("動画.mp4", 30, 20)], [("動画.mp4", 30, 30)]]
    assert receiver.in_progress == []


def test_part_file_is_hidden_from_listings(received_dir: Path) -> None:
    receiver = Receiver(received_dir, max_size=1000)
    listed: list[list[str]] = []

    async def chunks() -> AsyncIterator[bytes]:
        listed.append([f.name for f in storage.list_files(received_dir)])
        yield b"x"

    asyncio.run(receiver.receive("a.txt", 1, chunks()))

    # 受信中の .part は先頭が「.」なので、一覧に出ない
    assert listed == [[]]
    assert [f.name for f in storage.list_files(received_dir)] == ["a.txt"]
