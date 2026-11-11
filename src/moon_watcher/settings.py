"""Switches you can flip from Telegram. Saved to the database so a restart keeps them."""

import threading
import time

from moon_watcher.store import Saved

MONITORS = {
    "uptime": "🔍 Uptime monitoring",
    "warmup": "🔥 Auto-warmup",
    "resources": "🖥 Server & Docker alerts",
}
PAUSE_CHOICES = [(60, "1 hour"), (360, "6 hours"), (1440, "24 hours"), (0, "Until I resume")]


class Settings:
    def __init__(self):
        self._store = Saved("settings")
        self._lock = threading.Lock()
        # monitor -> unix time the pause ends, None = paused until resumed by hand
        self._paused: dict[str, float | None] = {}
        self.silent = True

    def load(self) -> None:
        data = self._store.load({})
        with self._lock:
            self._paused = {k: v for k, v in (data.get("paused") or {}).items() if k in MONITORS}
            self.silent = data.get("silent", True)

    def _save(self) -> None:
        with self._lock:
            data = {"paused": dict(self._paused), "silent": self.silent}
        self._store.save(data)

    def is_enabled(self, monitor: str) -> bool:
        with self._lock:
            if monitor not in self._paused:
                return True
            until = self._paused[monitor]
            return until is not None and time.time() >= until

    def paused(self) -> dict[str, float | None]:
        with self._lock:
            now = time.time()
            return {k: v for k, v in self._paused.items() if v is None or v > now}

    def pause(self, monitor: str, minutes: int) -> None:
        with self._lock:
            self._paused[monitor] = time.time() + minutes * 60 if minutes else None
        self._save()

    def resume(self, monitor: str) -> None:
        with self._lock:
            self._paused.pop(monitor, None)
        self._save()

    def set_silent(self, value: bool) -> None:
        self.silent = value
        self._save()

    def expire_pauses(self) -> list[str]:
        """Drop pauses that ran out; returns the monitors that just came back."""
        now = time.time()
        with self._lock:
            done = [k for k, v in self._paused.items() if v is not None and v <= now]
            for k in done:
                del self._paused[k]
        if done:
            self._save()
        return done
