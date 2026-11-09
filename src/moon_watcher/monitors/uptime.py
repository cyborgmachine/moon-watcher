#!/usr/bin/env python3
"""Uptime checks and downtime incidents.

The incidents table is the source of truth for "is this site down", so a bot restart
can't cause a missed recovery or a duplicate alert.
"""

import logging
import time
from datetime import datetime, timezone
from html import escape

import requests

from moon_watcher import database as db
from moon_watcher.config import REQUEST_TIMEOUT, UPTIME_FAIL_THRESHOLD, UPTIME_RETRY_DELAY, USER_AGENT
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.utils.fmt import fmt_elapsed

logger = logging.getLogger(__name__)


class UptimeMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.notifier = notifier

    def _probe(self, url: str) -> dict:
        """One HTTP request. Never raises."""
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
        """Probe the site, re-probing failures to filter out blips."""
        result = self._probe(url)
        for attempt in range(2, UPTIME_FAIL_THRESHOLD + 1):
            if result["status"] == "online":
                break
            logger.info("%s failed (%s), confirming %d/%d",
                        url, result["error"] or result["status_code"], attempt, UPTIME_FAIL_THRESHOLD)
            time.sleep(UPTIME_RETRY_DELAY)
            result = self._probe(url)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        db.save_uptime_check(url, result["status"], result["status_code"],
                             result["response_time"], result["error"])
        self._update_incident(url, result, timestamp)
        return {**result, "timestamp": timestamp}

    def _update_incident(self, url: str, result: dict, timestamp: str) -> None:
        incident = db.get_open_incident(url)

        if result["status"] == "offline":
            if incident is None:
                incident = {"id": db.open_downtime_incident(url), "alert_sent": False}
                db.log_event("ERROR", "uptime",
                             f"Site DOWN: {result['error'] or result['status_code']}", url)
            # Retried on every check until Telegram actually accepts it
            if not incident["alert_sent"] and self._send_down_alert(url, result, timestamp):
                db.mark_incident_alerted(incident["id"])

        elif incident is not None:
            duration = db.close_downtime_incident(url)
            text = fmt_elapsed(duration.total_seconds()) if duration else "unknown"
            db.log_event("INFO", "uptime", f"Site RECOVERED after {text}", url)
            if incident["alert_sent"]:  # no point announcing recovery from an outage nobody heard about
                self._send_recovery_alert(url, text)

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
