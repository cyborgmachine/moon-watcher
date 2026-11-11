"""SSL certificate checks: expiry date and whether browsers would trust the certificate."""

import logging
import socket
import ssl
import threading
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse

from moon_watcher.config import REQUEST_TIMEOUT, SSL_ALERT_DAYS, SSL_HOSTS, UPTIME_SITES, WARMUP_SITES
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.store import Saved

logger = logging.getLogger(__name__)


def split_target(target: str) -> tuple[str, int]:
    host, _, port = target.partition(":")
    return host, int(port) if port.isdigit() else 443


def make_target(host: str, port: int | None) -> str:
    host = host.lower()
    return host if port in (None, 443) else f"{host}:{port}"


def collect_targets(urls: list[str], extra: list[str]) -> list[str]:
    """Hosts of the https sites plus SSL_HOSTS, without duplicates, in config order."""
    targets: list[str] = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname:
            targets.append(make_target(parsed.hostname, parsed.port))
    for entry in extra:
        host, port = split_target(entry)
        targets.append(make_target(host, port))
    return list(dict.fromkeys(targets))


TARGETS = collect_targets(UPTIME_SITES + WARMUP_SITES, SSL_HOSTS)


def _issuer(cert: dict) -> str | None:
    fields = {key: value for rdn in cert.get("issuer", ()) for key, value in rdn}
    return fields.get("organizationName") or fields.get("commonName")


def fetch_certificate(target: str, context: ssl.SSLContext | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    """Connect and read the certificate. Never raises.

    status is "ok", "invalid" (the server answered, but the certificate would not be trusted)
    or "error" (could not connect at all).
    """
    host, port = split_target(target)
    context = context or ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                context.wrap_socket(sock, server_hostname=host) as conn:
            cert = conn.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return {"status": "invalid", "error": e.verify_message or str(e)}
    except TimeoutError:
        return {"status": "error", "error": "Timeout"}
    except OSError as e:
        return {"status": "error", "error": str(e)[:200] or type(e).__name__}

    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    return {"status": "ok", "not_after": not_after.isoformat(), "issuer": _issuer(cert)}


def days_left(result: dict) -> int | None:
    if not result.get("not_after"):
        return None
    return (datetime.fromisoformat(result["not_after"]) - datetime.now(timezone.utc)).days


def _date(result: dict) -> str:
    if not result.get("not_after"):
        return "—"
    return datetime.fromisoformat(result["not_after"]).strftime("%Y-%m-%d %H:%M UTC")


def _days_text(days: int) -> str:
    if days <= 0:
        return "today"
    return "in 1 day" if days == 1 else f"in {days} days"


class CertMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self._lock = threading.Lock()
        # target -> last result, shown in the bot
        self.results: dict[str, dict] = {}
        # target -> {"not_after", "levels", "invalid"}: which alerts already went out for this certificate
        self._sent: dict[str, dict] = {}
        self._results_store = Saved("ssl_results")
        self._sent_store = Saved("ssl_alerts")

    def load_state(self) -> None:
        self.results = self._results_store.load({})
        self._sent = self._sent_store.load({})

    def check_all(self, targets: list[str]) -> None:
        for target in targets:
            self.check(target)

    def check(self, target: str) -> dict:
        result = fetch_certificate(target)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        if result["status"] == "error":
            logger.warning("SSL check of %s failed: %s", target, result["error"])

        with self._lock:
            self.results[target] = result
            self._alert(target, result)
            results, sent = dict(self.results), dict(self._sent)
        self._results_store.save(results)
        self._sent_store.save(sent)
        return result

    def _alert(self, target: str, result: dict) -> None:
        sent = self._sent.setdefault(target, {"not_after": None, "levels": [], "invalid": False})

        if result["status"] == "invalid":
            if not sent["invalid"] and self._send(self._invalid_text(target, result), target, "ssl_invalid"):
                sent["invalid"] = True
            return
        if result["status"] != "ok":
            return  # unreachable sites are reported by the uptime check

        if sent["invalid"] and self._send(self._valid_again_text(target, result), target, "ssl_valid"):
            sent["invalid"] = False

        if result["not_after"] != sent["not_after"]:
            sent["not_after"] = result["not_after"]
            sent["levels"] = []

        days = days_left(result)
        level = next((t for t in sorted(SSL_ALERT_DAYS) if days <= t), None)
        if level is None or level in sent["levels"]:
            return
        if self._send(self._expiry_text(target, result, days), target, f"ssl_expiry_{level}"):
            sent["levels"].append(level)

    def _send(self, text: str, target: str, event: str) -> bool:
        return self.notifier.send_alert(text, site_url=f"ssl:{target}", event_type=event, cooldown=False)

    @staticmethod
    def _expiry_text(target: str, result: dict, days: int) -> str:
        icon = "🔴" if days <= 3 else "🟠" if days <= 7 else "🟡"
        return (
            f"{icon} <b>SSL certificate expires {_days_text(days)}</b>\n\n"
            f"🌐 <b>Host:</b> {escape(target)}\n"
            f"📅 <b>Valid until:</b> {_date(result)}\n"
            f"🏢 <b>Issuer:</b> {escape(result.get('issuer') or 'unknown')}\n\n"
            f"If the certificate renews automatically, the renewal is probably not working."
        )

    @staticmethod
    def _invalid_text(target: str, result: dict) -> str:
        return (
            f"🚨 <b>SSL certificate problem</b>\n\n"
            f"🌐 <b>Host:</b> {escape(target)}\n"
            f"❌ {escape(result['error'])}\n\n"
            f"Browsers show a security warning on this site."
        )

    @staticmethod
    def _valid_again_text(target: str, result: dict) -> str:
        return (
            f"✅ <b>SSL certificate is valid again</b>\n\n"
            f"🌐 <b>Host:</b> {escape(target)}\n"
            f"📅 <b>Valid until:</b> {_date(result)}"
        )

    def status_icon(self, target: str) -> str:
        result = self.results.get(target)
        if not result:
            return "⚪"
        if result["status"] == "invalid":
            return "🔴"
        if result["status"] != "ok":
            return "⚠️"
        days = days_left(result)
        if days <= 7:
            return "🔴"
        if days <= max(SSL_ALERT_DAYS, default=14):
            return "🟡"
        return "🟢"

    def short_status(self, target: str) -> str:
        result = self.results.get(target)
        if not result:
            return "not checked"
        if result["status"] == "ok":
            return f"{days_left(result)}d"
        return result["status"]

    def format_detail(self, target: str) -> str:
        result = self.results.get(target)
        lines = [f"{self.status_icon(target)} <b>SSL certificate</b>\n", f"🌐 <b>Host:</b> {escape(target)}"]
        if not result:
            lines.append("\nNot checked yet.")
            return "\n".join(lines)

        if result["status"] == "ok":
            lines += [
                f"📅 <b>Valid until:</b> {_date(result)}",
                f"⏳ <b>Days left:</b> {days_left(result)}",
                f"🏢 <b>Issuer:</b> {escape(result.get('issuer') or 'unknown')}",
            ]
        elif result["status"] == "invalid":
            lines.append(f"❌ <b>Not trusted:</b> {escape(result['error'])}")
        else:
            lines.append(f"⚠️ <b>Could not connect:</b> {escape(result['error'])}")

        checked = datetime.fromisoformat(result["checked_at"]).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"🔍 <b>Last check:</b> {checked}")
        return "\n".join(lines)

    def format_report(self, targets: list[str]) -> str:
        lines = [f"🔒 <b>SSL certificates</b> ({len(targets)})\n"]
        lines += [f"{self.status_icon(t)} {escape(t)} — {self.short_status(t)}" for t in targets]
        return "\n".join(lines)
