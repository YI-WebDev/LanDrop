from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEVICE_COOKIE_NAME = "landrop_device"
DEVICE_TTL_SECONDS = 30 * 24 * 60 * 60
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 60 * 60
MAX_DEVICES = 20
MAX_KEYS_PER_DEVICE = 4
HANDOFF_TTL_SECONDS = 120
_LAST_SEEN_WRITE_INTERVAL_SECONDS = 60 * 60

ALREADY_REMEMBERED = ""

_log = logging.getLogger(__name__)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def device_label(user_agent: str) -> str:
    ua = user_agent.lower()
    for keyword, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("macintosh", "Mac"),
        ("windows", "Windows"),
    ):
        if keyword in ua:
            return label
    return "端末"


@dataclass
class Device:
    id: str
    label: str
    created: float
    last_seen: float
    key_hashes: list[str] = field(default_factory=list)

    def move_key_to_most_recently_used(self, key_hash: str) -> None:
        if self.key_hashes[-1] != key_hash:
            self.key_hashes.remove(key_hash)
            self.key_hashes.append(key_hash)

    def add_key(self, key_hash: str) -> None:
        self.key_hashes = [*self.key_hashes, key_hash][-MAX_KEYS_PER_DEVICE:]


def _parse_device(item: object) -> Device | None:
    if not isinstance(item, dict):
        return None
    number = (int, float)
    values = {
        "id": item.get("id"),
        "label": item.get("label"),
        "created": item.get("created"),
        "last_seen": item.get("last_seen"),
        "key_hashes": item.get("key_hashes"),
    }
    if not (
        isinstance(values["id"], str)
        and isinstance(values["label"], str)
        and isinstance(values["created"], number)
        and not isinstance(values["created"], bool)
        and isinstance(values["last_seen"], number)
        and not isinstance(values["last_seen"], bool)
        and isinstance(values["key_hashes"], list)
        and all(isinstance(h, str) for h in values["key_hashes"])
    ):
        return None
    return Device(**values)  # type: ignore[arg-type]


class DeviceStore:
    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        self._handoffs: dict[str, tuple[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _log.warning("覚えている端末の一覧を読み込めませんでした: %s", e)
            return
        items = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(items, list):
            _log.warning("覚えている端末の一覧の形式が違うため、読み込みませんでした。")
            return
        now = self._clock()
        for item in items:
            device = _parse_device(item)
            if device is None:
                _log.warning("覚えている端末の一覧に読めない項目があったため、読み飛ばしました。")
            elif now - device.last_seen < DEVICE_TTL_SECONDS:
                self._devices[device.id] = device

    def _save(self) -> None:
        if self._path is None:
            return
        body = json.dumps({"devices": [asdict(d) for d in self._devices.values()]}, ensure_ascii=False, indent=1)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            _log.warning("覚えている端末の一覧を保存できませんでした: %s", e)

    def _expired(self, device: Device, now: float) -> bool:
        return now - device.last_seen >= DEVICE_TTL_SECONDS

    def register(self, label: str) -> str:
        secret = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            device = Device(id=uuid.uuid4().hex, label=label, created=now, last_seen=now, key_hashes=[_hash(secret)])
            self._devices[device.id] = device
            if len(self._devices) > MAX_DEVICES:
                oldest = min(self._devices.values(), key=lambda d: d.last_seen)
                del self._devices[oldest.id]
            self._save()
        return secret

    def authenticate(self, secret: str) -> str | None:
        if not secret:
            return None
        hashed = _hash(secret)
        now = self._clock()
        with self._lock:
            device = next((d for d in self._devices.values() if hashed in d.key_hashes), None)
            if device is None:
                return None
            if self._expired(device, now):
                del self._devices[device.id]
                self._save()
                return None
            device.move_key_to_most_recently_used(hashed)
            last_seen_is_stale = (
                now - device.last_seen >= _LAST_SEEN_WRITE_INTERVAL_SECONDS
            )
            if last_seen_is_stale:
                device.last_seen = now
                self._save()
            return device.id

    def create_handoff(self, device_id: str) -> str | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            if device_id not in self._devices:
                return None
            self._handoffs = {h: v for h, v in self._handoffs.items() if v[1] > now}
            self._handoffs[_hash(token)] = (device_id, now + HANDOFF_TTL_SECONDS)
        return token

    def redeem_handoff(self, token: str, current_device: str | None = None) -> str | None:
        if not token:
            return None
        now = self._clock()
        with self._lock:
            entry = self._handoffs.pop(_hash(token), None)
            if entry is None or entry[1] <= now:
                return None
            device = self._devices.get(entry[0])
            if device is None:
                return None
            already_holds_key = current_device == device.id
            if already_holds_key:
                return ALREADY_REMEMBERED
            secret = secrets.token_urlsafe(32)
            device.add_key(_hash(secret))
            device.last_seen = now
            self._save()
        return secret

    def forget(self, device_id: str) -> bool:
        with self._lock:
            if self._devices.pop(device_id, None) is None:
                return False
            self._handoffs = {h: v for h, v in self._handoffs.items() if v[0] != device_id}
            self._save()
        return True

    def list(self) -> list[dict[str, object]]:
        now = self._clock()
        with self._lock:
            devices = [d for d in self._devices.values() if not self._expired(d, now)]
        devices.sort(key=lambda d: d.last_seen, reverse=True)
        return [
            {
                "id": d.id,
                "label": d.label,
                "created": d.created,
                "last_seen": d.last_seen,
                "expires": d.last_seen + DEVICE_TTL_SECONDS,
            }
            for d in devices
        ]
