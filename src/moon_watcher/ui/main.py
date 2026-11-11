"""Main menu, server, stats, settings and journal screens."""

import time
from datetime import datetime, timezone
from html import escape

from moon_watcher import database as db
from moon_watcher.config import (
    DOMAIN_CHECK_INTERVAL,
    IGNORED_DOCKER_CONTAINERS,
    LOG_RETENTION_DAYS,
    MONITORED_DOMAINS,
    RESOURCE_ALERT_CHECKS,
    RESOURCE_CHECK_INTERVAL,
    SSL_CHECK_INTERVAL,
    THRESHOLD_CONTAINER_CPU,
    THRESHOLD_CONTAINER_MEM,
    THRESHOLD_CPU,
    THRESHOLD_DISK,
    THRESHOLD_RAM,
    UPTIME_CHECK_INTERVAL,
    UPTIME_SITES,
    WARMUP_INTERVAL,
    WARMUP_SITES,
)
from moon_watcher.monitors import tls
from moon_watcher.monitors.system import SystemMonitor, collect_host_metrics
from moon_watcher.services import Services
from moon_watcher.settings import MONITORS, PAUSE_CHOICES
from moon_watcher.ui.common import Screen, ago, back_btn, btn, keyboard, menu_btn


def pause_label(until: float | None) -> str:
    if until is None:
        return "off until resumed"
    return "paused until " + datetime.fromtimestamp(until, timezone.utc).strftime("%H:%M UTC")


def paused_lines(svc: Services) -> str:
    paused = svc.settings.paused()
    return "\n".join(f"⏸ {escape(MONITORS[m])}: {pause_label(u)}" for m, u in paused.items())


def main_menu(svc: Services) -> Screen:
    down = sum(1 for s in UPTIME_SITES if svc.uptime.is_down(s))
    text = (
        f"🌙 <b>Moon Watcher</b>\n\n"
        f"🔍 Uptime sites: {len(UPTIME_SITES)}" + (f"  (🔴 {down} down)" if down else "") + "\n"
        f"🔥 Warmup sites: {len(WARMUP_SITES)}\n"
        f"🌐 Domains: {len(MONITORED_DOMAINS)}\n"
        f"🔒 SSL certificates: {len(tls.TARGETS)}\n"
    )
    paused = paused_lines(svc)
    if paused:
        text += f"\n{paused}\n"
    text += "\nChoose an action:"
    return text, keyboard(
        [btn("🔥 Cache warmup", "w:0", "primary"), btn("🔍 Uptime", "u:0", "primary")],
        [btn("🐳 Docker", "k", "primary"), btn("🖥 Server", "sys", "primary")],
        [btn("🌐 Domains", "d:0"), btn("🔒 SSL", "s:0")],
        [btn("📊 Stats", "stats"), btn("⚙️ Settings", "set")],
    )


def system(svc: Services) -> Screen:
    text = SystemMonitor.format_report(collect_host_metrics())
    return text, keyboard([btn("🔄 Refresh", "sys", "primary")], [menu_btn()])


def _state(svc: Services, monitor: str, interval: int) -> str:
    paused = svc.settings.paused()
    return pause_label(paused[monitor]) if monitor in paused else f"every {interval} min"


def stats(svc: Services) -> Screen:
    warmups = db.get_all_warmup_stats()
    day = db.uptime_window(24)
    domains = [d.get("alert_status") for d in db.get_all_domains_latest()]

    percents = [day[s]["percent"] for s in UPTIME_SITES if s in day and day[s]["percent"] is not None]
    avg_up = f"{sum(percents) / len(percents):.2f}%" if percents else "—"
    down = sum(1 for s in UPTIME_SITES if svc.uptime.is_down(s))
    certs = [svc.certs.status_icon(t) for t in tls.TARGETS]

    text = (
        f"📊 <b>Overall Stats</b>\n\n"
        f"<b>🔍 Uptime:</b>\n"
        f"  Sites: {len(UPTIME_SITES)}  Up: {len(UPTIME_SITES) - down}/{len(UPTIME_SITES)}\n"
        f"  Avg uptime 24h: {avg_up}\n\n"
        f"<b>🔥 Warmup:</b>\n"
        f"  Sites: {len(WARMUP_SITES)}  Runs: {sum(w['total_runs'] for w in warmups)}  "
        f"Pages: {sum(w['total_pages'] or 0 for w in warmups)}\n\n"
        f"<b>🌐 Domains:</b>\n"
        f"  Total: {len(MONITORED_DOMAINS)}  🟢 {domains.count('ok')}  🟡 {domains.count('warning')}  "
        f"🔴 {domains.count('critical') + domains.count('expired')}\n\n"
        f"<b>🔒 SSL:</b>\n"
        f"  Hosts: {len(tls.TARGETS)}  🟢 {certs.count('🟢')}  🟡 {certs.count('🟡')}  🔴 {certs.count('🔴')}\n\n"
        f"<b>⚙️ Schedule:</b>\n"
        f"  Uptime: {_state(svc, 'uptime', UPTIME_CHECK_INTERVAL)}\n"
        f"  Warmup: {_state(svc, 'warmup', WARMUP_INTERVAL)}\n"
        f"  Server &amp; Docker: {_state(svc, 'resources', RESOURCE_CHECK_INTERVAL)}\n"
        f"  Domains: every {DOMAIN_CHECK_INTERVAL} min\n"
        f"  SSL: every {SSL_CHECK_INTERVAL} min"
    )
    return text, keyboard([menu_btn()])


def settings(svc: Services) -> Screen:
    paused = svc.settings.paused()
    rows = []
    for monitor, label in MONITORS.items():
        if monitor in paused:
            rows.append([btn(f"{label}: ⏸ {pause_label(paused[monitor])}", f"set:{monitor}", "danger")])
        else:
            rows.append([btn(f"{label}: ✅ ON", f"set:{monitor}", "success")])
    silent = svc.settings.silent
    rows.append([btn(f"🔕 Silent mode: {'✅ ON' if silent else '❌ OFF'}", "set:silent",
                     "success" if silent else None)])
    rows.append([btn("📋 Check journal", "jr")])
    rows.append([menu_btn()])

    text = (
        f"⚙️ <b>Settings</b>\n\n"
        f"⏱ Uptime every {UPTIME_CHECK_INTERVAL} min, warmup every {WARMUP_INTERVAL} min, "
        f"domains every {DOMAIN_CHECK_INTERVAL} min, SSL every {SSL_CHECK_INTERVAL} min\n"
        f"⏱ Resources every {RESOURCE_CHECK_INTERVAL} min (alert after {RESOURCE_ALERT_CHECKS} checks)\n"
        f"🗑 History kept for {LOG_RETENTION_DAYS} days\n\n"
        f"<b>Thresholds:</b>\n"
        f"🖥 Host — CPU {THRESHOLD_CPU}% | RAM {THRESHOLD_RAM}% | Disk {THRESHOLD_DISK}%\n"
        f"🐳 Container — CPU {THRESHOLD_CONTAINER_CPU}% | Mem {THRESHOLD_CONTAINER_MEM}%\n"
        f"🙈 Ignored containers: {len(IGNORED_DOCKER_CONTAINERS)}\n\n"
        f"<i>Silent mode: scheduled checks only send alerts, no reports.\n"
        f"Pauses survive a restart and are listed in the main menu.</i>"
    )
    return text, keyboard(*rows)


def pause_menu(svc: Services, monitor: str) -> Screen:
    label = MONITORS[monitor]
    rows = [[btn(text, f"pause:{monitor}:{minutes}")] for minutes, text in PAUSE_CHOICES]
    return (
        f"⏸ <b>Pause {escape(label)}</b>\n\nFor how long?",
        keyboard(*rows, [back_btn("set")]),
    )


def journal(svc: Services) -> Screen:
    lines = ["📋 <b>Check journal</b>\n"]
    now = time.monotonic()
    for job in svc.scheduler.jobs.values():
        if job.name == "heartbeat":
            continue
        if job.running:
            state = f"⏳ running for {(now - job.running_since) / 60:.0f} min"
        elif not job.enabled():
            state = "⏸ paused / nothing to check"
        else:
            state = f"next in {max(0, job.next_due - now) / 60:.0f} min"
        lines.append(f"<b>{escape(job.title)}</b>\n  last: {ago(job.last_finished)} · {state}")
        if job.last_error:
            lines.append(f"  ❌ {escape(job.last_error[:120])}")
    return "\n".join(lines), keyboard([btn("🔄 Refresh", "jr", "primary")], [back_btn("set"), menu_btn()])
