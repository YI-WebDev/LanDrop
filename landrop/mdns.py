from __future__ import annotations

import logging
import re
import socket
import threading
import unicodedata

_log = logging.getLogger(__name__)
_MAX_LABEL = 40


def local_name(pc_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", pc_name).encode("ascii", "ignore").decode("ascii").lower()
    label = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    label = f"landrop-{label}" if label else "landrop"
    return label[:_MAX_LABEL].rstrip("-") + ".local"


class Advertiser:
    def __init__(self, name: str, ip: str, port: int) -> None:
        self.name = name
        self._ip = ip
        self._port = port
        self._zeroconf = None
        self._closed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._register, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _register(self) -> None:
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError:
            _log.warning("zeroconf がないため、%s の名前は使えません。", self.name)
            return
        try:
            zc = Zeroconf(interfaces=[self._ip], ip_version=IPVersion.V4Only)
        except OSError as e:
            _log.warning("%s の名前を告知できませんでした: %s", self.name, e)
            return
        with self._lock:
            if self._closed:
                zc.close()
                return
            self._zeroconf = zc
        info = ServiceInfo(
            "_http._tcp.local.",
            "LanDrop._http._tcp.local.",
            addresses=[socket.inet_aton(self._ip)],
            port=self._port,
            server=self.name + ".",
        )
        try:
            zc.register_service(info, allow_name_change=True)
        except Exception as e:  # noqa: BLE001
            _log.warning("%s の名前を告知できませんでした: %s", self.name, e)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            zc, self._zeroconf = self._zeroconf, None
        if zc is not None:
            try:
                zc.close()
            except Exception:  # noqa: BLE001
                pass
