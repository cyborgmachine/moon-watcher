"""The work the scheduler runs, also started by hand from the bot buttons."""

import logging
import time
from functools import partial
from html import escape

from moon_watcher import database as db
from moon_watcher import store
from moon_watcher.config import (
    DOCKER_MONITORING,
    DOMAIN_CHECK_INTERVAL,
    LOG_RETENTION_DAYS,
    METRICS_RETENTION_DAYS,
    MONITORED_DOMAINS,
    RESOURCE_CHECK_INTERVAL,
    SSL_CHECK_INTERVAL,
    SSL_MONITORING,
    UPTIME_CHECK_INTERVAL,
    UPTIME_SITES,
    WARMUP_INTERVAL,
    WARMUP_SITES,
)
from moon_watcher.monitors.cache_warmer import CacheWarmer, format_time
from moon_watcher.monitors import tls
from moon_watcher.monitors.domain import DomainMonitor
from moon_watcher.scheduler import Job
from moon_watcher.services import Services
from moon_watcher.settings import MONITORS

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "heartbeat"
MINUTE = 60


def register(svc: Services) -> None:
    s = svc.settings
    add = svc.scheduler.add
    add(Job("resources", "Server & Docker check", partial(resources, svc),
            interval=RESOURCE_CHECK_INTERVAL * MINUTE, first_delay=10, stall_after=10 * MINUTE,
            enabled=lambda: s.is_enabled("resources")))
    add(Job("uptime", "Uptime check", partial(uptime, svc),
            interval=UPTIME_CHECK_INTERVAL * MINUTE, first_delay=15, stall_after=15 * MINUTE,
            enabled=lambda: bool(UPTIME_SITES) and s.is_enabled("uptime")))
    add(Job("domains", "Domain check", partial(domains, svc),
            interval=DOMAIN_CHECK_INTERVAL * MINUTE, first_delay=60, stall_after=30 * MINUTE,
            enabled=lambda: bool(MONITORED_DOMAINS)))
    add(Job("ssl", "SSL check", partial(certificates, svc),
            interval=SSL_CHECK_INTERVAL * MINUTE, first_delay=30, stall_after=15 * MINUTE,
            enabled=lambda: SSL_MONITORING and bool(tls.TARGETS)))
    add(Job("warmup", "Cache warmup", partial(warmup, svc),
            interval=WARMUP_INTERVAL * MINUTE, first_delay=120,
            enabled=lambda: bool(WARMUP_SITES) and s.is_enabled("warmup")))
    add(Job("cleanup", "Old data cleanup", cleanup,
            interval=6 * 60 * MINUTE, first_delay=10 * MINUTE, stall_after=30 * MINUTE))
    add(Job("heartbeat", "Heartbeat", partial(heartbeat, svc),
            interval=MINUTE, stall_after=5 * MINUTE))


def _reply(svc: Services, chat_id: int | None, text: str) -> None:
    if chat_id is None:
        svc.notifier.broadcast(text)
    else:
        svc.notifier.send_message(chat_id, text)


def _progress_reporter(svc: Services, chat_id: int, url: str):
    def report(cur, total, pct):
        if cur % 50 == 0:
            svc.notifier.send_message(chat_id, f"⏳ {escape(url)}\nProgress: {cur}/{total} ({pct:.1f}%)")
    return report


def warmup(svc: Services, chat_id: int | None = None, site: str | None = None) -> None:
    sites = [site] if site else WARMUP_SITES
    manual = chat_id is not None
    report: list[str] = []
    problems: list[str] = []

    for n, url in enumerate(sites, 1):
        if manual and len(sites) > 1:
            svc.notifier.send_message(chat_id, f"🔥 Warming {n}/{len(sites)}: {escape(url)}")

        progress = _progress_reporter(svc, chat_id, url) if manual and site else None
        try:
            r = CacheWarmer(url).run_warmup(progress_callback=progress)
        except Exception as e:
            logger.exception("Warmup of %s failed", url)
            r = {"success": False, "message": str(e)}

        if not r["success"]:
            problems.append(url)
            report.append(f"❌ {escape(url)}: {escape(r.get('message') or 'failed')}")
        elif site:
            rate = r["successful"] / r["total_urls"] * 100 if r["total_urls"] else 0
            report.append(
                f"🌐 {escape(url)}\n"
                f"Pages: {r['total_urls']}  ✅ {r['successful']} ({rate:.1f}%)  ❌ {r['failed']}\n"
                f"Time: {format_time(r['duration'])}  Avg load: {r['avg_load_time']:.2f}s"
            )
        else:
            icon = "⚠️" if r["failed"] else "✅"
            failed = f", {r['failed']} failed" if r["failed"] else ""
            report.append(f"{icon} {escape(url)}: {r['successful']}/{r['total_urls']} pages{failed}")

        if n < len(sites):
            time.sleep(5)

    body = "\n".join(report)
    title = "⚠️ <b>Warmup finished with problems</b>" if problems else "✅ <b>Warmup finished</b>"
    if manual:
        svc.notifier.send_message(chat_id, f"{title}\n\n{body}")
        return

    # scheduled run: in silent mode only say something when the set of broken sites changes
    before, svc.warmup_problems = svc.warmup_problems, set(problems)
    if not svc.settings.silent or (problems and set(problems) != before):
        svc.notifier.broadcast(f"{title}\n\n{body}")
    elif before and not problems:
        svc.notifier.broadcast("✅ <b>Warmup works again</b> for all sites")


def uptime(svc: Services, chat_id: int | None = None) -> None:
    results = svc.uptime.check_many(UPTIME_SITES)
    if chat_id is None and svc.settings.silent:
        return  # down / recovery alerts are sent by the monitor itself

    icons = {"online": "🟢", "offline": "🔴"}
    lines = [f"{icons.get(r['status'], '⚠️')} {escape(url[:50])}" for url, r in results.items()]
    down = sum(1 for r in results.values() if r["status"] != "online")
    title = f"⚠️ <b>{down} of {len(results)} sites have problems</b>" if down else "✅ <b>All sites are up</b>"
    _reply(svc, chat_id, f"{title}\n\n" + "\n".join(lines))


def check_site(svc: Services, chat_id: int, url: str) -> None:
    r = svc.uptime.check_site(url)
    icon = "🟢" if r["status"] == "online" else "🔴"
    text = (
        f"{icon} <b>Check result</b>\n\n"
        f"🌐 {escape(url)}\n"
        f"Status: <b>{r['status'].upper()}</b>\n"
        f"Code: {r['status_code']}\n"
        f"Response: {r['response_time']:.2f}s"
    )
    if r["error"]:
        text += f"\nError: {escape(r['error'])}"
    svc.notifier.send_message(chat_id, text)


def domains(svc: Services, chat_id: int | None = None) -> None:
    svc.domains.check_all_domains()
    svc.domains.check_and_send_alerts()
    if chat_id is not None:
        svc.notifier.send_message(chat_id, DomainMonitor.format_all_domains_report())


def check_domain(svc: Services, chat_id: int, domain: str) -> None:
    svc.domains.check_domain(domain)
    svc.domains.check_and_send_alerts()
    svc.notifier.send_message(chat_id, DomainMonitor.format_domain_report(domain))


def certificates(svc: Services, chat_id: int | None = None) -> None:
    svc.certs.check_all(tls.TARGETS)
    if chat_id is not None:
        svc.notifier.send_message(chat_id, svc.certs.format_report(tls.TARGETS))


def check_certificate(svc: Services, chat_id: int, target: str) -> None:
    svc.certs.check(target)
    svc.notifier.send_message(chat_id, svc.certs.format_detail(target))


def resources(svc: Services) -> None:
    try:
        svc.system.check()
    except Exception:
        logger.exception("Host resource check failed")
    if DOCKER_MONITORING:
        svc.docker.check()


def cleanup() -> None:
    db.cleanup_old_records(LOG_RETENTION_DAYS, METRICS_RETENTION_DAYS)


def heartbeat(svc: Services) -> None:
    store.save(HEARTBEAT_KEY, time.time())
    for monitor in svc.settings.expire_pauses():
        svc.notifier.broadcast(f"▶️ <b>{escape(MONITORS[monitor])}</b> is back on, the pause has ended")


def stalled(svc: Services, job: Job, running_for: float) -> None:
    svc.notifier.broadcast(
        f"⏳ <b>{escape(job.title)} is stuck</b>\n\n"
        f"Running for {running_for / 60:.0f} min. Scheduled runs are skipped until it finishes."
    )


def unstalled(svc: Services, job: Job) -> None:
    svc.notifier.broadcast(f"✅ <b>{escape(job.title)}</b> finished and runs on schedule again")
