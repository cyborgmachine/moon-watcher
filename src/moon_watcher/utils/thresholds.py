"""Sustained-threshold tracking: fire once after N consecutive breaches, recover once."""

import threading


class SustainedThreshold:
    def __init__(self, required: int):
        self.required = max(1, required)
        self._count: dict[str, int] = {}
        self._alerted: set[str] = set()
        self._lock = threading.Lock()

    def update(self, key: str, breached: bool) -> str | None:
        """Return 'fire' (alert now), 'recover' (back to normal after an alert) or None.

        After 'fire' the caller must call mark_alerted(key) once the alert was actually
        delivered; otherwise 'fire' is returned again on the next check.
        """
        with self._lock:
            if breached:
                self._count[key] = self._count.get(key, 0) + 1
                if self._count[key] >= self.required and key not in self._alerted:
                    return "fire"
                return None
            self._count.pop(key, None)
            if key in self._alerted:
                self._alerted.discard(key)
                return "recover"
            return None

    def mark_alerted(self, key: str) -> None:
        with self._lock:
            self._alerted.add(key)

    def breaches(self, key: str) -> int:
        with self._lock:
            return self._count.get(key, 0)

    def prune(self, valid_keys: set[str]) -> None:
        """Forget keys that no longer exist (e.g. a removed container)."""
        with self._lock:
            for k in [k for k in self._count if k not in valid_keys]:
                del self._count[k]
            self._alerted &= valid_keys

    def alerted(self) -> list[str]:
        with self._lock:
            return sorted(self._alerted)

    def restore(self, keys) -> None:
        """Bring back the "already alerted" keys after a restart, so recoveries still get reported."""
        with self._lock:
            self._alerted = set(keys or [])
