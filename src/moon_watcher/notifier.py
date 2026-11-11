"""Sends messages and alerts to the admin chats."""

import html
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from moon_watcher.config import ALERT_COOLDOWN
from moon_watcher.store import Saved

logger = logging.getLogger(__name__)

MAX_MESSAGE = 4000
SEPARATOR = "\n\n· · ·\n\n"
DELIVERY_TIMEOUT = 180


@dataclass
class Alert:
    text: str
    key: str = ""
    event: str = "generic"
    cooldown: bool = True


@dataclass
class _Pending:
    alert: Alert
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False


def plain_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def pack(texts: list[str], limit: int = MAX_MESSAGE) -> list[list[int]]:
    """Group message indexes into chunks that fit into one Telegram message."""
    chunks: list[list[int]] = []
    size = 0
    for i, text in enumerate(texts):
        extra = len(text) + len(SEPARATOR)
        if chunks and size + extra <= limit:
            chunks[-1].append(i)
            size += extra
        else:
            chunks.append([i])
            size = extra
    return chunks


class TelegramNotifier:
    batch_window = 3.0

    def __init__(self, bot_token: str, admin_ids: list[int]):
        self.admin_ids = admin_ids
        self.enabled = bool(bot_token and admin_ids)
        self._api_base = f"https://api.telegram.org/bot{bot_token}"
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._cooldown_store = Saved("alert_cooldowns")
        self._cooldowns: dict[str, float] = {}
        self._queue: queue.Queue[_Pending] = queue.Queue()
        self._sender: threading.Thread | None = None

    def load_state(self) -> None:
        self._cooldowns = self._cooldown_store.load({})

    def _post(self, payload: dict) -> requests.Response:
        return self._session.post(f"{self._api_base}/sendMessage", json=payload, timeout=10)

    def send_message(self, chat_id: int, text: str, parse_mode: str | None = "HTML") -> bool:
        if not self.enabled:
            return False
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(3):
            try:
                resp = self._post(payload)
            except requests.RequestException as e:
                # the exception text contains the URL, and the URL contains the token
                logger.error("Telegram send failed for chat %s: %s", chat_id, type(e).__name__)
                time.sleep(2 * (attempt + 1))
                continue

            if resp.ok:
                return True

            try:
                body = resp.json()
            except ValueError:
                body = {}
            description = body.get("description", "")

            if resp.status_code == 429:
                wait = (body.get("parameters") or {}).get("retry_after", 5)
                logger.warning("Telegram rate limit, waiting %ss", wait)
                time.sleep(min(wait, 60) + 0.5)
                continue
            if resp.status_code == 400 and "parse entities" in description and "parse_mode" in payload:
                logger.warning("Telegram rejected the markup, sending as plain text: %s", description)
                payload.pop("parse_mode")
                payload["text"] = plain_text(payload["text"])
                continue

            logger.error("Telegram send failed for chat %s: %s %s", chat_id, resp.status_code, description)
            return False
        return False

    def broadcast(self, text: str, parse_mode: str | None = "HTML") -> int:
        return sum(1 for admin_id in self.admin_ids if self.send_message(admin_id, text, parse_mode))

    def send_alert(self, text: str, site_url: str = "", event_type: str = "generic", cooldown: bool = True) -> bool:
        """Send an alert to every admin and wait until it is delivered.

        cooldown=True drops the same (site_url, event_type) for ALERT_COOLDOWN seconds;
        state changes (down, recovered, expiring) pass cooldown=False.
        """
        return self.send_alerts([Alert(text, site_url, event_type, cooldown)])[0]

    def send_alerts(self, alerts: list[Alert]) -> list[bool]:
        """Alerts arriving within a few seconds of each other go out as one message."""
        results = [False] * len(alerts)
        pending: list[tuple[int, _Pending]] = []
        for i, alert in enumerate(alerts):
            if alert.cooldown and self._cooling_down(alert):
                logger.debug("Alert suppressed (cooldown): %s %s", alert.key, alert.event)
                continue
            pending.append((i, _Pending(alert)))

        if not pending:
            return results
        self._ensure_sender()
        for _, p in pending:
            self._queue.put(p)

        deadline = time.monotonic() + DELIVERY_TIMEOUT
        for i, p in pending:
            p.done.wait(max(0.0, deadline - time.monotonic()))
            results[i] = p.ok
            if p.ok and p.alert.cooldown:
                self._start_cooldown(p.alert)
        return results

    def _cooldown_key(self, alert: Alert) -> str:
        return f"{alert.key}|{alert.event}"

    def _cooling_down(self, alert: Alert) -> bool:
        with self._lock:
            last = self._cooldowns.get(self._cooldown_key(alert))
        return last is not None and time.time() - last < ALERT_COOLDOWN

    def _start_cooldown(self, alert: Alert) -> None:
        now = time.time()
        with self._lock:
            self._cooldowns[self._cooldown_key(alert)] = now
            self._cooldowns = {k: t for k, t in self._cooldowns.items() if now - t < ALERT_COOLDOWN}
            snapshot = dict(self._cooldowns)
        self._cooldown_store.save(snapshot)

    def _ensure_sender(self) -> None:
        with self._lock:
            if self._sender is None or not self._sender.is_alive():
                self._sender = threading.Thread(target=self._send_loop, name="alert-sender", daemon=True)
                self._sender.start()

    def _send_loop(self) -> None:
        while True:
            batch = [self._queue.get()]
            deadline = time.monotonic() + self.batch_window
            while (left := deadline - time.monotonic()) > 0:
                try:
                    batch.append(self._queue.get(timeout=left))
                except queue.Empty:
                    break
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._deliver(batch)
            except Exception:
                logger.exception("Alert delivery crashed")
            finally:
                for p in batch:
                    p.done.set()

    def _deliver(self, batch: list[_Pending]) -> None:
        if len(batch) == 1:
            batch[0].ok = self.broadcast(batch[0].alert.text) > 0
            return

        texts = [p.alert.text for p in batch]
        chunks = pack(texts)
        for n, chunk in enumerate(chunks):
            body = SEPARATOR.join(texts[i] for i in chunk)
            if n == 0:
                body = f"🔔 <b>{len(batch)} events</b>{SEPARATOR}{body}"
            ok = self.broadcast(body) > 0
            for i in chunk:
                batch[i].ok = ok
