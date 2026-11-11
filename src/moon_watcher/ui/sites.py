"""Warmup, uptime, domain and SSL screens."""

import logging
from html import escape

from moon_watcher import database as db
from moon_watcher.config import MONITORED_DOMAINS, SSL_CHECK_INTERVAL, SSL_MONITORING, UPTIME_SITES, WARMUP_SITES
from moon_watcher.monitors import tls
from moon_watcher.monitors.domain import DomainMonitor
from moon_watcher.services import Services
from moon_watcher.ui.common import (
    PAGE_SIZE,
    Screen,
    back_btn,
    btn,
    find,
    gone,
    item_id,
    keyboard,
    menu_btn,
    pager,
    utc,
)
from moon_watcher.utils.paging import paginate

logger = logging.getLogger(__name__)

NO_HISTORY = "\n<i>History is unavailable while the database is down.</i>"


def _try_db(fn, *args, default=None):
    try:
        return fn(*args), True
    except Exception as e:
        logger.warning("%s: %s", fn.__name__, e)
        return default, False


def warmup_list(svc: Services, page: int) -> Screen:
    chunk, page, pages = paginate(WARMUP_SITES, page, PAGE_SIZE)
    runs = {s["site_url"]: s for s in db.get_all_warmup_stats()}
    rows = []
    for site in chunk:
        icon = "⚠️" if site in svc.warmup_problems else ("✅" if site in runs else "⚪")
        rows.append([btn(f"{icon} {site[:40]}", f"ws:{item_id(site)}")])
    return (
        f"🔥 <b>Cache Warmup</b>\n\nSites: {len(WARMUP_SITES)}",
        keyboard(*rows, pager("w", page, pages),
                 [btn("🔥 Warm up ALL sites", "wa", "danger")], [menu_btn()]),
    )


def warmup_site(svc: Services, iid: str) -> Screen:
    site = find(WARMUP_SITES, iid)
    if site is None:
        return gone("w")
    s = db.get_warmup_stats(site)
    rate = s["successful_pages"] / s["total_pages"] * 100 if s["total_pages"] else 0
    text = (
        f"🔥 <b>Cache Warmup</b>\n\n"
        f"🌐 {escape(site)}\n\n"
        f"Runs: {s['total_runs']}\n"
        f"Pages: {s['total_pages']}  ✅ {s['successful_pages']} ({rate:.1f}%)  ❌ {s['failed_pages']}\n"
        f"Last: {utc(s['last_warmup'])}"
    )
    return text, keyboard([btn("🔥 Warm up NOW", f"wn:{iid}", "success")], [back_btn("w"), menu_btn()])


def _uptime_icon(svc: Services, site: str, last: dict[str, str]) -> str:
    if svc.uptime.is_down(site):
        return "🔴"
    return {"online": "🟢", "offline": "🟡"}.get(last.get(site, ""), "⚪")


def uptime_list(svc: Services, page: int) -> Screen:
    chunk, page, pages = paginate(UPTIME_SITES, page, PAGE_SIZE)
    last, ok = _try_db(db.last_uptime_status, default={})
    rows = [[btn(f"{_uptime_icon(svc, s, last)} {s[:40]}", f"us:{item_id(s)}")] for s in chunk]
    down = sum(1 for s in UPTIME_SITES if svc.uptime.is_down(s))
    text = f"🔍 <b>Uptime Monitoring</b>\n\nSites: {len(UPTIME_SITES)}"
    if down:
        text += f"\n🔴 Down: {down}"
    if not svc.settings.is_enabled("uptime"):
        text += "\n⏸ Scheduled checks are paused"
    if not ok:
        text += NO_HISTORY
    return text, keyboard(*rows, pager("u", page, pages), [btn("🔍 Check ALL sites", "ua", "primary")], [menu_btn()])


def _percent(window: dict, site: str) -> str:
    row = window.get(site)
    return f"{row['percent']:.2f}%" if row and row["percent"] is not None else "—"


def uptime_site(svc: Services, iid: str) -> Screen:
    site = find(UPTIME_SITES, iid)
    if site is None:
        return gone("u")
    buttons = keyboard([btn("🔍 Check NOW", f"un:{iid}", "success")], [back_btn("u"), menu_btn()])
    last, ok = _try_db(db.last_uptime_status, default={})
    icon = _uptime_icon(svc, site, last)
    status = "DOWN" if svc.uptime.is_down(site) else (last.get(site) or "unknown").upper()
    text = f"🔍 <b>Uptime Monitor</b>\n\n🌐 {escape(site)}\n{icon} Status: <b>{status}</b>\n"
    if not ok:
        return text + NO_HISTORY, buttons

    day = db.uptime_window(24, site)
    week = db.uptime_window(24 * 7, site)
    avg = (day.get(site) or {}).get("avg_response")
    text += (
        f"\nUptime 24h: {_percent(day, site)}   7d: {_percent(week, site)}\n"
        f"Avg response 24h: {f'{avg:.2f}s' if avg is not None else '—'}\n"
        f"Incidents 7d: {db.count_downtime_incidents(site, 7)}\n"
    )
    recent = db.get_recent_uptime_checks(site, 5)
    if recent:
        text += "\n<b>Recent:</b>\n"
        for r in recent:
            ri = "🟢" if r["status"] == "online" else "🔴"
            ts = r["checked_at"].strftime("%H:%M:%S") if r["checked_at"] else "?"
            text += f"{ri} {ts} — {r['response_time']:.2f}s\n"
    return text, buttons


DOMAIN_ICONS = {"expired": "🔴", "critical": "🔴", "warning": "🟡", "ok": "🟢", "error": "❌"}


def domain_list(svc: Services, page: int) -> Screen:
    chunk, page, pages = paginate(MONITORED_DOMAINS, page, PAGE_SIZE)
    latest = {d["domain_name"]: d for d in db.get_all_domains_latest()}
    rows = []
    for domain in chunk:
        icon = DOMAIN_ICONS.get((latest.get(domain) or {}).get("alert_status"), "⚪")
        rows.append([btn(f"{icon} {domain[:35]}", f"ds:{item_id(domain)}")])

    statuses = [latest[d].get("alert_status") for d in MONITORED_DOMAINS if d in latest]
    text = (
        f"🌐 <b>Domain Monitoring</b>\n\n"
        f"Total: {len(MONITORED_DOMAINS)}\n"
        f"🟢 OK: {statuses.count('ok')}  🟡 Warning: {statuses.count('warning')}  "
        f"🔴 Critical: {statuses.count('critical') + statuses.count('expired')}"
    )
    if statuses.count("error"):
        text += f"  ❌ Errors: {statuses.count('error')}"
    return text, keyboard(*rows, pager("d", page, pages), [btn("🔍 Check ALL domains", "da", "primary")], [menu_btn()])


def domain_site(svc: Services, iid: str) -> Screen:
    domain = find(MONITORED_DOMAINS, iid)
    if domain is None:
        return gone("d")
    return (
        DomainMonitor.format_domain_report(domain),
        keyboard([btn("🔍 Check NOW", f"dn:{iid}", "success")], [back_btn("d"), menu_btn()]),
    )


def ssl_list(svc: Services, page: int) -> Screen:
    targets = tls.TARGETS
    chunk, page, pages = paginate(targets, page, PAGE_SIZE)
    rows = [
        [btn(f"{svc.certs.status_icon(t)} {t[:35]} — {svc.certs.short_status(t)}", f"ss:{item_id(t)}")]
        for t in chunk
    ]
    icons = [svc.certs.status_icon(t) for t in targets]
    text = (
        f"🔒 <b>SSL certificates</b>\n\n"
        f"Hosts: {len(targets)}\n"
        f"🟢 {icons.count('🟢')}  🟡 {icons.count('🟡')}  🔴 {icons.count('🔴')}"
    )
    if icons.count("⚠️"):
        text += f"  ⚠️ {icons.count('⚠️')}"
    if not targets:
        text += "\n\nNo https sites in UPTIME_SITES / WARMUP_SITES and SSL_HOSTS is empty."
    elif not SSL_MONITORING:
        text += "\n\n⏸ Scheduled checks are off (SSL_MONITORING=false)."
    else:
        text += f"\n\nChecked every {SSL_CHECK_INTERVAL} min."
    return text, keyboard(*rows, pager("s", page, pages), [btn("🔍 Check ALL", "sa", "primary")], [menu_btn()])


def ssl_site(svc: Services, iid: str) -> Screen:
    target = find(tls.TARGETS, iid)
    if target is None:
        return gone("s:0")
    return (
        svc.certs.format_detail(target),
        keyboard([btn("🔍 Check NOW", f"sn:{iid}", "success")], [back_btn("s:0"), menu_btn()]),
    )
