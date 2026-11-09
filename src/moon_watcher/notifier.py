#!/usr/bin/env python3
"""Sends messages and alerts to the admin chats."""

import logging
import threading
import time

import requests

from moon_watcher.config import ALERT_COOLDOWN

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, admin_ids: list[int]):
        self._bot_token = bot_token
        self.admin_ids = admin_ids
        self._api_base = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and admin_ids)
        self._session = requests.Session()
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            return False
        try:
            resp = self._session.post(
                f"{self._api_base}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            # Never log the exception verbatim: the URL contains the bot token
            logger.error("Telegram send failed for chat %s: %s", chat_id, type(e).__name__)
            return False

    def broadcast(self, text: str, parse_mode: str = "HTML") -> int:
        """Send a message to ALL admin IDs. Returns count of successes."""
        return sum(1 for admin_id in self.admin_ids if self.send_message(admin_id, text, parse_mode))

    def send_alert(
        self,
        text: str,
        site_url: str = "",
        event_type: str = "generic",
        cooldown: bool = True,
    ) -> bool:
        """Send to all admins, return True if delivered.

        cooldown=True skips the same (site_url, event_type) for ALERT_COOLDOWN seconds.
        State changes (down, recovered, expiring) pass cooldown=False so they always go out.
        """
        key = (site_url, event_type)
        if cooldown:
            with self._lock:
                last = self._cooldowns.get(key)
            if last is not None and time.monotonic() - last < ALERT_COOLDOWN:
                logger.debug("Alert suppressed (cooldown): %s %s", site_url, event_type)
                return False

        if self.broadcast(text) == 0:
            return False  # not delivered — don't start a cooldown, caller may retry
        if cooldown:
            with self._lock:
                self._cooldowns[key] = time.monotonic()
        return True
