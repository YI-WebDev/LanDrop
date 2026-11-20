"""一度QRコードで接続したスマホを覚えておき、次の起動からQRコードなしで通す。

Cookieに入れる値（端末の鍵）はそのまま保存せず、SHA-256のハッシュだけをファイルに残す。
"""

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
# 最後に使ってからこの日数が過ぎた端末は忘れる
DEVICE_TTL_SECONDS = 30 * 24 * 60 * 60
# Cookieの有効期限。期限の判定はサーバー側で行うので、ブラウザには長めに持たせる
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 60 * 60
# 覚えておく端末の数。超えたら、いちばん長く使っていないものから忘れる
MAX_DEVICES = 20
# 1台の端末が持てる鍵の数（IPアドレスで開いたとき用、名前で開いたとき用など、開くアドレスごとに1つ）
MAX_KEYS_PER_DEVICE = 4
# 名前のアドレスへ移るための使い捨ての鍵の有効期間
HANDOFF_TTL_SECONDS = 120
# 最後に使った時刻を書き出す間隔（毎回のリクエストでファイルに書かない）
_TOUCH_INTERVAL_SECONDS = 60 * 60

# redeem_handoff の戻り値。移った先ですでに同じ端末の鍵を持っているので、新しい鍵は要らない
ALREADY_REMEMBERED = ""

_log = logging.getLogger(__name__)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def device_label(user_agent: str) -> str:
    """User-Agent から、PCの画面で見分けるための大まかな端末の種類を決める。"""
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
    # 最後に使ったものほど後ろ。上限を超えたら、いちばん長く使っていない鍵から消す
    key_hashes: list[str] = field(default_factory=list)


def _parse_device(item: object) -> Device | None:
    """ファイルの1件を確かめて読む。型が合わないものは読み飛ばす。"""
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
    """覚えている端末の一覧。複数のスレッド（サーバーとタイマー）から使われる。"""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        # 使い捨ての鍵のハッシュ → (端末のid, 期限)
        self._handoffs: dict[str, tuple[str, float]] = {}
        self._load()

    # ---------- 読み書き ----------

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            # 壊れていたら、覚えていないものとして始める（もう一度QRコードを読めば済む）
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
        """一時ファイルに書いてから置き換える。書けなくても、この起動の間は覚えている。"""
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

    # ---------- 操作 ----------

    def register(self, label: str) -> str:
        """新しい端末を覚え、Cookieに入れる鍵を返す。"""
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
        """鍵が覚えている端末のものなら、その端末のidを返す。"""
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
            # 使った鍵を後ろへ回す（メモリの上だけ。ファイルには最後に使った時刻と一緒に書き出す）
            if device.key_hashes[-1] != hashed:
                device.key_hashes.remove(hashed)
                device.key_hashes.append(hashed)
            if now - device.last_seen >= _TOUCH_INTERVAL_SECONDS:
                device.last_seen = now
                self._save()
            return device.id

    def create_handoff(self, device_id: str) -> str | None:
        """別のアドレス（名前）で開き直すための、使い捨ての鍵を作る。"""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            if device_id not in self._devices:
                return None
            self._handoffs = {h: v for h, v in self._handoffs.items() if v[1] > now}
            self._handoffs[_hash(token)] = (device_id, now + HANDOFF_TTL_SECONDS)
        return token

    def redeem_handoff(self, token: str, current_device: str | None = None) -> str | None:
        """使い捨ての鍵と引き換えに、同じ端末の新しい鍵を返す。鍵は一度しか使えない。

        current_device: 移った先のアドレスですでに持っている鍵の端末。同じ端末なら鍵を増やさず
        ALREADY_REMEMBERED を返す（何度移っても鍵が増えて、ほかのアドレスの鍵が押し出されないように）。
        """
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
            if current_device == device.id:
                return ALREADY_REMEMBERED
            secret = secrets.token_urlsafe(32)
            device.key_hashes = [*device.key_hashes, _hash(secret)][-MAX_KEYS_PER_DEVICE:]
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
