"""Uptime checks and downtime incidents.

Open incidents live in memory and are mirrored to the database. The mirror is
what makes a restart safe; the memory copy is what keeps alerts going out
while the database is down.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape

import requests

from moon_watcher import database as db
from moon_watcher.config import (
    REQUEST_TIMEOUT,
    UPTIME_CONCURRENCY,
    UPTIME_FAIL_THRESHOLD,
    UPTIME_RETRY_DELAY,
    USER_AGENT,
)
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.utils.fmt import fmt_elapsed

logger = logging.getLogger(__name__)

_FAILED = object()


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        logger.warning("%s failed: %s", fn.__name__, e)
        return _FAILED


class UptimeMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.notifier = notifier
        self._incidents: dict[str, dict] = {}
        self._unclosed: set[str] = set()
        self._synced = False
        self._lock = threading.Lock()
        self._site_locks: dict[str, threading.Lock] = {}

    def _site_lock(self, url: str) -> threading.Lock:
        with self._lock:
            return self._site_locks.setdefault(url, threading.Lock())

    def _probe(self, url: str) -> dict:
        t0 = time.time()
        status, status_code, error = "offline", 0, None
        try:
            r = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, verify=True)
            status_code = r.status_code
            status = "online" if r.status_code < 400 else "offline"
        except requests.exceptions.Timeout:
            error = "Timeout"
        except requests.exceptions.SSLError:
            error = "SSL error"
        except requests.exceptions.ConnectionError:
            error = "Connection error"
        except requests.exceptions.RequestException as e:
            error = str(e)[:120]
        except Exception as e:
            error = f"Unexpected: {e}"[:120]
        return {
            "status": status,
            "status_code": status_code,
            "error": error,
            "response_time": time.time() - t0,
        }

    def check_site(self, url: str) -> dict:
        """Probe the site, re-probing failures to filter out blips.

        Manual and scheduled checks of the same site wait for each other.
        """
        with self._site_lock(url):
            result = self._probe(url)
            for attempt in range(2, UPTIME_FAIL_THRESHOLD + 1):
                if result["status"] == "online":
                    break
                logger.info("%s failed (%s), confirming %d/%d",
                            url, result["error"] or result["status_code"], attempt, UPTIME_FAIL_THRESHOLD)
                time.sleep(UPTIME_RETRY_DELAY)
                result = self._probe(url)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._sync_from_db()
            self._update_incident(url, result, timestamp)
            _try(db.save_uptime_check, url, result["status"], result["status_code"],
                 result["response_time"], result["error"])
            return {**result, "timestamp": timestamp}

    def check_many(self, urls: list[str]) -> dict[str, dict]:
        if not urls:
            return {}
        with ThreadPoolExecutor(max_workers=min(UPTIME_CONCURRENCY, len(urls))) as pool:
            return dict(zip(urls, pool.map(self._check_safely, urls), strict=True))

    def _check_safely(self, url: str) -> dict:
        try:
            return self.check_site(url)
        except Exception as e:
            logger.exception("Uptime check of %s crashed", url)
            return {"status": "error", "status_code": 0, "error": str(e)[:120], "response_time": 0.0}

    def is_down(self, url: str) -> bool:
        return url in self._incidents

    def _sync_from_db(self) -> None:
        """Pick up incidents left open by the previous process. Retried until the DB answers."""
        if self._synced:
            return
        stored = _try(db.get_open_incidents)
        if stored is _FAILED:
            return
        with self._lock:
            for url, row in stored.items():
                mine = self._incidents.get(url)
                if mine is None:
                    self._incidents[url] = {
                        "id": row["id"],
                        "started_at": row["started_at"],
                        "alert_sent": row["alert_sent"],
                        "alert_saved": row["alert_sent"],
                    }
                else:
                    mine["id"] = row["id"]
                    mine["started_at"] = min(mine["started_at"], row["started_at"])
                    mine["alert_sent"] = mine["alert_sent"] or row["alert_sent"]
                    mine["alert_saved"] = row["alert_sent"]
            self._synced = True

    def _close_leftovers(self) -> None:
        for url in list(self._unclosed):
            if _try(db.close_downtime_incident, url) is not _FAILED:
                self._unclosed.discard(url)

    def _update_incident(self, url: str, result: dict, timestamp: str) -> None:
        """Alerts go out first, the database is written after, so a dead DB can't delay them."""
        self._close_leftovers()
        incident = self._incidents.get(url)

        if result["status"] == "offline":
            is_new = incident is None
            if is_new:
                incident = {
                    "id": None,
                    "started_at": datetime.now(timezone.utc),
                    "alert_sent": False,
                    "alert_saved": False,
                }
                self._incidents[url] = incident

            if not incident["alert_sent"] and self._send_down_alert(url, result, timestamp):
                incident["alert_sent"] = True

            if incident["id"] is None and url not in self._unclosed:
                new_id = _try(db.open_downtime_incident, url, incident["started_at"])
                if new_id is not _FAILED:
                    incident["id"] = new_id
            needs_saving = incident["alert_sent"] and not incident["alert_saved"] and incident["id"] is not None
            if needs_saving and _try(db.mark_incident_alerted, incident["id"]) is not _FAILED:
                incident["alert_saved"] = True
            if is_new:
                db.log_event("ERROR", "uptime", f"Site DOWN: {result['error'] or result['status_code']}", url)
            return

        if incident is None:
            return
        del self._incidents[url]
        duration = fmt_elapsed((datetime.now(timezone.utc) - incident["started_at"]).total_seconds())
        if incident["alert_sent"]:
            self._send_recovery_alert(url, duration)

        if _try(db.close_downtime_incident, url) is _FAILED:
            self._unclosed.add(url)
        db.log_event("INFO", "uptime", f"Site RECOVERED after {duration}", url)

    def _send_down_alert(self, url: str, result: dict, ts: str) -> bool:
        error_line = f"\n<b>Error:</b> {escape(result['error'])}" if result["error"] else ""
        code_line = f"\n<b>HTTP:</b> {result['status_code']}" if result["status_code"] else ""
        confirmed = f"\n🔁 <b>Confirmed by:</b> {UPTIME_FAIL_THRESHOLD} checks" if UPTIME_FAIL_THRESHOLD > 1 else ""
        msg = (
            f"🚨 <b>ALERT: Site is DOWN!</b>\n\n"
            f"🌐 <b>URL:</b> {escape(url)}\n"
            f"🔴 <b>Status:</b> OFFLINE{code_line}{error_line}{confirmed}\n"
            f"⏱ <b>Checked:</b> {ts}\n"
            f"⏳ <b>Response:</b> {result['response_time']:.2f}s"
        )
        return self.notifier.send_alert(msg, site_url=url, event_type="downtime", cooldown=False)

    def _send_recovery_alert(self, url: str, duration: str) -> bool:
        msg = (
            f"✅ <b>RECOVERY: Site is back online!</b>\n\n"
            f"🌐 <b>URL:</b> {escape(url)}\n"
            f"🟢 <b>Status:</b> ONLINE\n"
            f"📉 <b>Downtime:</b> {duration}"
        )
        return self.notifier.send_alert(msg, site_url=url, event_type="recovery", cooldown=False)
