#!/usr/bin/env python3
"""Domain expiry monitoring via WHOIS."""

import logging
from datetime import datetime

import whois

from moon_watcher import database as db
from moon_watcher.monitors import rdap
from moon_watcher.config import DOMAIN_ALERT_DAYS, MONITORED_DOMAINS
from moon_watcher.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


def days_label(days: int | None) -> str:
    """Days until expiry for display: '0' is a real value, only negatives mean expired."""
    if days is None:
        return "unknown"
    return str(days) if days >= 0 else "EXPIRED"


def _naive(dt):
    # drop tzinfo so everything compares as naive datetimes
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        return dt.replace(tzinfo=None)
    return dt


def _first(value):
    # whois sometimes gives a list of dates, the first one is the real one
    return value[0] if isinstance(value, list) else value


def _as_list(value) -> list:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def _whois_record(domain_name: str) -> dict:
    info = whois.whois(domain_name)
    return {
        "registrar": info.registrar,
        "creation_date": _naive(_first(info.creation_date)),
        "expiration_date": _naive(_first(info.expiration_date)),
        "updated_date": _naive(_first(info.updated_date)),
        "name_servers": _as_list(info.name_servers),
        "status": _as_list(info.status),
    }


def lookup_domain(domain_name: str) -> dict:
    """RDAP first, whois as the fallback (many ccTLDs have no RDAP yet)."""
    problems = []
    rdap_record = None
    try:
        rdap_record = rdap.lookup(domain_name)
    except rdap.RdapUnavailable:
        pass
    except Exception as e:
        logger.warning("RDAP failed for %s: %s", domain_name, e)
        problems.append(f"RDAP: {e}")

    if rdap_record and rdap_record["expiration_date"]:
        return rdap_record

    try:
        return _whois_record(domain_name)
    except Exception as e:
        # RDAP answered but had no expiry date: still better than an error
        if rdap_record:
            return rdap_record
        problems.append(f"WHOIS: {e}")
        raise LookupError("; ".join(problems))


class DomainMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier

    def check_domain(self, domain_name: str) -> dict | None:
        try:
            rec = lookup_domain(domain_name)
            exp, cre, upd = rec["expiration_date"], rec["creation_date"], rec["updated_date"]
            registrar = rec["registrar"] or "Unknown"
            ns, st = rec["name_servers"], rec["status"]

            days = (exp - datetime.now()).days if exp else None

            # Domain was renewed: forget old thresholds so next year's alerts fire again
            if days is not None and days > max(DOMAIN_ALERT_DAYS):
                db.reset_domain_alerts(domain_name)

            if days is None:
                alert_status = "unknown"
            elif days < 0:
                alert_status = "expired"
            elif days <= min(DOMAIN_ALERT_DAYS):
                alert_status = "critical"
            elif days <= max(DOMAIN_ALERT_DAYS):
                alert_status = "warning"
            else:
                alert_status = "ok"

            db.save_domain_check(
                domain_name=domain_name,
                registrar=registrar,
                creation_date=cre,
                expiration_date=exp,
                updated_date=upd,
                days_until_expiry=days,
                alert_status=alert_status,
                name_servers=ns,
                whois_status=st,
            )
            db.log_event(
                "INFO", "domain",
                f"Checked: expires in {days} days, status={alert_status}",
                domain_name,
            )

            return {
                "domain_name": domain_name,
                "registrar": registrar,
                "creation_date": cre,
                "expiration_date": exp,
                "updated_date": upd,
                "days_until_expiry": days,
                "alert_status": alert_status,
                "name_servers": ns,
                "whois_status": st,
                "check_status": "success",
            }

        except Exception as e:
            logger.error("WHOIS check failed for %s: %s", domain_name, e)
            db.save_domain_check(domain_name=domain_name, alert_status="error", error=str(e)[:200])
            db.log_event("ERROR", "domain", f"WHOIS error: {e}"[:200], domain_name)
            return {
                "domain_name": domain_name,
                "check_status": "error",
                "error": str(e)[:200],
            }

    def check_all_domains(self) -> list[dict]:
        results = []
        for domain in MONITORED_DOMAINS:
            r = self.check_domain(domain)
            if r:
                results.append(r)
        return results

    def check_and_send_alerts(self) -> int:
        sent = 0
        for domain in MONITORED_DOMAINS:
            info = db.get_latest_domain_info(domain)
            if not info or info.get("alert_status") in (None, "ok", "unknown", "error"):
                continue

            days = info.get("days_until_expiry")
            if days is None:
                continue

            # Level 0 = already expired; otherwise the tightest matching threshold
            # (5 days left -> 7, not 30).
            if days < 0:
                level = 0
            else:
                level = next((t for t in sorted(DOMAIN_ALERT_DAYS) if days <= t), None)
            if level is None or not db.should_send_domain_alert(domain, level):
                continue

            # Record only after Telegram accepted the message, so a failed send is retried
            if self._send_domain_alert(
                domain, days, info.get("expiration_date"),
                info.get("registrar") or "Unknown", level,
            ):
                db.record_domain_alert(domain, level)
                sent += 1
        return sent

    def _send_domain_alert(self, domain, days, exp_date, registrar, threshold) -> bool:
        if days is not None and days < 0:
            icon, urgency = "🔴", "CRITICAL: DOMAIN EXPIRED!"
        elif threshold <= 3:
            icon, urgency = "🔴", "CRITICAL"
        elif threshold <= 7:
            icon, urgency = "🟠", "URGENT"
        elif threshold <= 14:
            icon, urgency = "🟡", "WARNING"
        else:
            icon, urgency = "⚠️", "REMINDER"

        exp_str = exp_date.strftime("%Y-%m-%d") if exp_date else "Unknown"
        msg = (
            f"{icon} <b>{urgency}: Domain expiring!</b>\n\n"
            f"🌐 <b>Domain:</b> {domain}\n"
            f"📅 <b>Expires:</b> {exp_str}\n"
            f"⏳ <b>Days left:</b> {days_label(days)}\n"
            f"🏢 <b>Registrar:</b> {registrar}"
        )
        return self.notifier.send_alert(
            msg, site_url=domain, event_type=f"domain_expiry_{threshold}", cooldown=False
        )

    @staticmethod
    def format_domain_report(domain_name: str) -> str:
        info = db.get_latest_domain_info(domain_name)
        if not info:
            return f"No data for {domain_name}"

        if info.get("error"):
            return f"❌ <b>Check failed</b>\n\n🌐 {domain_name}\n⚠️ {info['error']}"

        status_map = {
            "expired": ("🔴", "EXPIRED"),
            "critical": ("🔴", "CRITICAL"),
            "warning": ("🟡", "WARNING"),
            "ok": ("🟢", "OK"),
        }
        icon, label = status_map.get(info.get("alert_status", ""), ("⚪", "UNKNOWN"))
        days = info.get("days_until_expiry")

        def fmt_dt(v):
            return v.strftime("%Y-%m-%d") if v else "—"

        text = (
            f"{icon} <b>Domain info</b>\n\n"
            f"🌐 <b>Domain:</b> {domain_name}\n"
            f"📊 <b>Status:</b> {label}\n\n"
            f"<b>Dates:</b>\n"
            f"  Created: {fmt_dt(info.get('creation_date'))}\n"
            f"  Expires: {fmt_dt(info.get('expiration_date'))}\n"
            f"  Updated: {fmt_dt(info.get('updated_date'))}\n\n"
            f"⏳ <b>Days left:</b> {days_label(days)}\n"
            f"🏢 <b>Registrar:</b> {info.get('registrar', '—')}\n"
            f"🔍 <b>Last check:</b> {fmt_dt(info.get('checked_at'))}"
        )
        return text

    @staticmethod
    def format_all_domains_report() -> str:
        rows = db.get_all_domains_latest()
        if not rows:
            return "No domain data yet"

        icon_map = {"expired": "🔴", "critical": "🔴", "warning": "🟡", "ok": "🟢", "error": "❌"}
        lines = []
        for r in rows:
            ic = icon_map.get(r.get("alert_status", ""), "⚪")
            d = r.get("days_until_expiry")
            label = f"{d}d" if d is not None and d >= 0 else (r.get("alert_status") or "?").upper()
            lines.append(f"{ic} {r['domain_name'][:30]} — {label}")

        ok = sum(1 for r in rows if r.get("alert_status") == "ok")
        warn = sum(1 for r in rows if r.get("alert_status") == "warning")
        crit = sum(1 for r in rows if r.get("alert_status") in ("critical", "expired"))

        header = (
            f"📊 <b>Domain monitoring</b>\n\n"
            f"Total: {len(rows)}  🟢 {ok}  🟡 {warn}  🔴 {crit}\n\n"
        )
        return header + "\n".join(lines)
