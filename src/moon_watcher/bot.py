#!/usr/bin/env python3
"""Moon Watcher: Telegram bot for monitoring websites, domains, Docker and VPS resources."""

import asyncio
import logging
import signal
import sys
import threading
import time

import schedule
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.error import BadRequest, NetworkError, TimedOut

from moon_watcher import __version__
from moon_watcher import database as db
from moon_watcher.monitors.cache_warmer import CacheWarmer, format_time
from moon_watcher.config import (
    ADMIN_IDS,
    BOT_TOKEN,
    DOCKER_MONITORING,
    DOMAIN_CHECK_INTERVAL,
    IGNORED_DOCKER_CONTAINERS,
    LOG_RETENTION_DAYS,
    MONITORED_DOMAINS,
    RESOURCE_ALERT_CHECKS,
    RESOURCE_CHECK_INTERVAL,
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
from moon_watcher.monitors.docker_monitor import (
    LIST_FILTERS,
    STATUS_ICON,
    DockerMonitor,
    classify,
    filter_containers,
)
from moon_watcher.monitors.domain import DomainMonitor
from html import escape
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.monitors.system import SystemMonitor, collect_host_metrics
from moon_watcher.monitors.uptime import UptimeMonitor
from moon_watcher.utils.paging import paginate

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_warmup_lock = threading.Lock()
_uptime_lock = threading.Lock()
_domain_lock = threading.Lock()
_resource_lock = threading.Lock()

warmup_enabled = True
uptime_monitoring_enabled = True
resource_monitoring_enabled = True
silent_mode = True  # suppress success messages from scheduled checks (alerts still go through)

notifier: TelegramNotifier
uptime_monitor: UptimeMonitor
domain_monitor: DomainMonitor
system_monitor: SystemMonitor
docker_monitor: DockerMonitor
_scheduler_thread: threading.Thread | None = None


def btn(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    """InlineKeyboardButton with an optional style (primary / success / danger)."""
    kwargs = {"text": text, "callback_data": callback_data}
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        # older PTB without style support
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Network/timeout error, will retry: %s", context.error)
        return
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    logger.error("Unhandled exception:", exc_info=context.error)
    db.log_event("ERROR", "system", str(context.error)[:500])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            f"❌ Access denied.\nYour User ID: {update.effective_user.id}"
        )
        return
    await _send_main_menu(update.message.reply_text)


async def _send_main_menu(reply_fn, edit: bool = False):
    keyboard = [
        [
            btn("🔥 Cache warmup", "warmup_menu", "primary"),
            btn("🔍 Uptime", "uptime_menu", "primary"),
        ],
        [
            btn("🐳 Docker", "docker_menu", "primary"),
            btn("🖥 Server", "system_menu", "primary"),
        ],
        [
            btn("🌐 Domains", "domains_menu"),
            btn("📊 Stats", "general_stats"),
        ],
        [btn("⚙️ Settings", "settings")],
    ]
    text = (
        f"🌙 <b>Moon Watcher</b>\n\n"
        f"🔥 Warmup sites: {len(WARMUP_SITES)}\n"
        f"🔍 Uptime sites: {len(UPTIME_SITES)}\n"
        f"🌐 Domains: {len(MONITORED_DOMAINS)}\n"
        f"⏱ Warmup interval: {WARMUP_INTERVAL} min\n"
        f"⏱ Uptime interval: {UPTIME_CHECK_INTERVAL} min\n"
        f"⏱ Domain check: {DOMAIN_CHECK_INTERVAL} min\n\n"
        f"Choose an action:"
    )
    await reply_fn(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Access denied", show_alert=True)
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    data = query.data
    handlers = {
        "warmup_menu": show_warmup_menu,
        "uptime_menu": show_uptime_menu,
        "domains_menu": show_domains_menu,
        "docker_menu": show_docker_menu,
        "system_menu": show_system_menu,
        "toggle_resources": toggle_resources,
        "general_stats": show_general_stats,
        "settings": show_settings,
        "warmup_all": handle_warmup_all,
        "check_all": handle_check_all,
        "check_all_domains": handle_check_all_domains,
        "toggle_warmup": toggle_warmup,
        "toggle_uptime": toggle_uptime,
        "toggle_silent": toggle_silent_mode,
        "check_journal": show_check_journal,
        "back_to_menu": handle_back,
        "noop": _noop,
    }

    if data in handlers:
        await handlers[data](query)
    elif data.startswith("warmup_site_"):
        await show_warmup_site(query, int(data.split("_")[2]))
    elif data.startswith("uptime_site_"):
        await show_uptime_site(query, int(data.split("_")[2]))
    elif data.startswith("dk_"):
        _, key, page = data.split("_")
        await show_docker_list(query, key, int(page))
    elif data.startswith("dc_"):
        await show_container_detail(query, data[3:])
    elif data.startswith("dl_"):
        await show_container_logs(query, data[3:])
    elif data.startswith("domain_"):
        await show_domain_detail(query, int(data.split("_")[1]))
    elif data.startswith("warmup_now_"):
        await handle_warmup_now(query, int(data.split("_")[2]))
    elif data.startswith("check_now_"):
        await handle_check_now(query, int(data.split("_")[2]))
    elif data.startswith("check_domain_"):
        await handle_check_domain(query, int(data.split("_")[2]))


async def _noop(query):
    # the page counter button, nothing to do
    pass


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def show_warmup_menu(query):
    keyboard = []
    for i, site in enumerate(WARMUP_SITES):
        stats = db.get_warmup_stats(site)
        icon = "✅" if stats["total_runs"] > 0 else "⚪"
        keyboard.append([btn(f"{icon} {site[:40]}", f"warmup_site_{i}")])
    keyboard.append([btn("🔥 Warm up ALL sites", "warmup_all", "danger")])
    keyboard.append([btn("◀️ Back", "back_to_menu")])
    await _edit(query, f"🔥 <b>Cache Warmup</b>\n\nSites: {len(WARMUP_SITES)}", InlineKeyboardMarkup(keyboard))


async def show_uptime_menu(query):
    keyboard = []
    for i, site in enumerate(UPTIME_SITES):
        stats = db.get_uptime_stats(site)
        icon = {"online": "🟢", "offline": "🔴"}.get(stats.get("last_status", ""), "⚪")
        keyboard.append([btn(f"{icon} {site[:40]}", f"uptime_site_{i}")])
    keyboard.append([btn("🔍 Check ALL sites", "check_all", "primary")])
    keyboard.append([btn("◀️ Back", "back_to_menu")])
    await _edit(query, f"🔍 <b>Uptime Monitoring</b>\n\nSites: {len(UPTIME_SITES)}", InlineKeyboardMarkup(keyboard))


async def show_domains_menu(query):
    keyboard = []
    for i, domain in enumerate(MONITORED_DOMAINS):
        info = db.get_latest_domain_info(domain)
        icon = "⚪"
        if info:
            icon = {"expired": "🔴", "critical": "🔴", "warning": "🟡", "ok": "🟢"}.get(
                info.get("alert_status", ""), "⚪"
            )
        keyboard.append([btn(f"{icon} {domain[:35]}", f"domain_{i}")])
    keyboard.append([btn("🔍 Check ALL domains", "check_all_domains", "primary")])
    keyboard.append([btn("◀️ Back", "back_to_menu")])

    all_d = db.get_all_domains_latest()
    ok = sum(1 for d in all_d if d.get("alert_status") == "ok")
    warn = sum(1 for d in all_d if d.get("alert_status") == "warning")
    crit = sum(1 for d in all_d if d.get("alert_status") in ("critical", "expired"))

    text = (
        f"🌐 <b>Domain Monitoring</b>\n\n"
        f"Total: {len(MONITORED_DOMAINS)}\n"
        f"🟢 OK: {ok}  🟡 Warning: {warn}  🔴 Critical: {crit}\n\n"
        f"Select a domain:"
    )
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


DOCKER_PAGE_SIZE = 8


async def _docker_infos(query):
    """Container list for the docker screens. On failure shows an error and returns None."""
    if not DOCKER_MONITORING:
        await _edit(query, "🐳 Docker monitoring is disabled (DOCKER_MONITORING=false).",
                    InlineKeyboardMarkup([[btn("◀️ Back", "back_to_menu")]]))
        return None

    # no stats here, they take a while to collect and these screens don't show them
    infos = await asyncio.to_thread(docker_monitor.snapshot, False)
    if infos is None:
        await _edit(
            query,
            f"❌ <b>Cannot reach Docker</b>\n\n<code>{escape(docker_monitor.last_error or '')}</code>\n\n"
            "Check that the socket is mounted and DOCKER_GID is correct.",
            InlineKeyboardMarkup([[btn("🔄 Retry", "docker_menu", "primary")], [btn("◀️ Back", "back_to_menu")]]),
        )
    return infos


def _docker_ctx(rest: str) -> tuple[str, str, int]:
    """Splits '<id>_<filter>_<page>' from a button. Buttons from older messages only have the id."""
    parts = rest.split("_")
    key = parts[1] if len(parts) > 1 and parts[1] in LIST_FILTERS else "a"
    try:
        page = max(0, int(parts[2]))
    except (IndexError, ValueError):
        page = 0
    return parts[0], key, page


async def show_docker_menu(query):
    infos = await _docker_infos(query)
    if infos is None:
        return

    counts = {key: len(filter_containers(infos, key)) for key in LIST_FILTERS}

    def filter_btn(key):
        icon, label, _ = LIST_FILTERS[key]
        style = "danger" if key == "p" and counts[key] else None
        return btn(f"{icon} {label} ({counts[key]})", f"dk_{key}_0", style)

    keyboard = [
        [filter_btn("p"), filter_btn("r")],
        [filter_btn("s"), filter_btn("a")],
        [btn("🏠 Menu", "back_to_menu"), btn("🔄 Refresh", "docker_menu", "primary")],
    ]
    await _edit(query, DockerMonitor.format_summary(infos), InlineKeyboardMarkup(keyboard))


async def show_docker_list(query, key, page):
    if key not in LIST_FILTERS:
        key = "a"
    infos = await _docker_infos(query)
    if infos is None:
        return

    items = filter_containers(infos, key)
    chunk, page, pages = paginate(items, page, DOCKER_PAGE_SIZE)

    keyboard = []
    for i in chunk:
        icon = STATUS_ICON[classify(i)]
        keyboard.append([btn(f"{icon} {i['name'][:35]}", f"dc_{i['id']}_{key}_{page}")])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀️", f"dk_{key}_{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("▶️", f"dk_{key}_{page + 1}"))
        keyboard.append(nav)

    keyboard.append([
        btn("◀️ Back", "docker_menu"),
        btn("🏠 Menu", "back_to_menu"),
        btn("🔄 Refresh", f"dk_{key}_{page}", "primary"),
    ])
    text = DockerMonitor.format_list(chunk, key, page, pages, len(items))
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_container_detail(query, rest):
    short_id, key, page = _docker_ctx(rest)
    ctx = f"{key}_{page}"
    back = [btn("◀️ Back", f"dk_{ctx}"), btn("🏠 Menu", "back_to_menu")]

    info = await asyncio.to_thread(docker_monitor.get_container, short_id)
    if info is None:
        await _edit(query, "❌ Container not found (it may have been removed or recreated).",
                    InlineKeyboardMarkup([back]))
        return
    keyboard = [
        [btn("🔄 Refresh", f"dc_{short_id}_{ctx}", "primary"), btn("📜 Logs", f"dl_{short_id}_{ctx}")],
        back,
    ]
    await _edit(query, await asyncio.to_thread(DockerMonitor.format_detail, info),
                InlineKeyboardMarkup(keyboard))


async def show_container_logs(query, rest):
    short_id, key, page = _docker_ctx(rest)
    logs = await asyncio.to_thread(docker_monitor.get_logs, short_id, 30)
    if logs is None:
        text = "❌ Could not read logs."
    elif not logs.strip():
        text = "📜 No logs."
    else:
        lines = [ln[:200] for ln in logs.strip().splitlines()]
        body = escape("\n".join(lines))[-3500:]
        text = f"📜 <b>Last 30 log lines</b>\n\n<pre>{body}</pre>"
    keyboard = [[btn("◀️ Back", f"dc_{short_id}_{key}_{page}"), btn("🏠 Menu", "back_to_menu")]]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_system_menu(query):
    await _edit(query, "🖥 Collecting server stats…")
    m = await asyncio.to_thread(collect_host_metrics)
    text = await asyncio.to_thread(SystemMonitor.format_report, m)
    keyboard = [
        [btn("🔄 Refresh", "system_menu", "primary")],
        [btn("◀️ Back", "back_to_menu")],
    ]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_warmup_site(query, idx):
    if idx >= len(WARMUP_SITES):
        return
    site = WARMUP_SITES[idx]
    s = db.get_warmup_stats(site)
    rate = (s["successful_pages"] / s["total_pages"] * 100) if s["total_pages"] else 0
    text = (
        f"🔥 <b>Cache Warmup</b>\n\n"
        f"🌐 {site}\n\n"
        f"Runs: {s['total_runs']}\n"
        f"Pages: {s['total_pages']}  ✅ {s['successful_pages']} ({rate:.1f}%)  ❌ {s['failed_pages']}\n"
        f"Last: {s['last_warmup'] or 'never'}"
    )
    keyboard = [
        [btn("🔥 Warm up NOW", f"warmup_now_{idx}", "success")],
        [btn("◀️ Back", "warmup_menu")],
    ]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_uptime_site(query, idx):
    if idx >= len(UPTIME_SITES):
        return
    site = UPTIME_SITES[idx]
    s = db.get_uptime_stats(site)
    icon = {"online": "🟢", "offline": "🔴"}.get(s.get("last_status", ""), "⚪")
    incidents = db.count_downtime_incidents(site)

    text = (
        f"🔍 <b>Uptime Monitor</b>\n\n"
        f"🌐 {site}\n"
        f"{icon} Status: <b>{(s.get('last_status') or 'unknown').upper()}</b>\n\n"
        f"Checks: {s['total_checks']}  ✅ {s['successful_checks']}  ❌ {s['failed_checks']}\n"
        f"Incidents: {incidents}\n"
        f"Uptime: {s['uptime_percentage']:.2f}%\n"
        f"Last: {s.get('last_check') or 'never'}\n"
    )

    recent = db.get_recent_uptime_checks(site, 5)
    if recent:
        text += "\n<b>Recent:</b>\n"
        for r in recent:
            ri = "🟢" if r["status"] == "online" else "🔴"
            ts = r["checked_at"].strftime("%H:%M:%S") if r["checked_at"] else "?"
            text += f"{ri} {ts} — {r['response_time']:.2f}s\n"

    keyboard = [
        [btn("🔍 Check NOW", f"check_now_{idx}", "success")],
        [btn("◀️ Back", "uptime_menu")],
    ]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_domain_detail(query, idx):
    if idx >= len(MONITORED_DOMAINS):
        return
    domain = MONITORED_DOMAINS[idx]
    text = DomainMonitor.format_domain_report(domain)
    keyboard = [
        [btn("🔍 Check NOW", f"check_domain_{idx}", "success")],
        [btn("◀️ Back", "domains_menu")],
    ]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_general_stats(query):
    w_stats = db.get_all_warmup_stats()
    u_stats = db.get_all_uptime_stats()
    d_latest = db.get_all_domains_latest()

    total_runs = sum(s.get("total_runs", 0) for s in w_stats)
    total_pages = sum(s.get("total_pages", 0) for s in w_stats)
    total_checks = sum(s.get("total_checks", 0) for s in u_stats)
    online = sum(1 for s in u_stats if s.get("last_status") == "online")
    avg_up = (sum(float(s.get("uptime_percentage", 100)) for s in u_stats) / len(u_stats)) if u_stats else 100

    d_ok = sum(1 for d in d_latest if d.get("alert_status") == "ok")
    d_warn = sum(1 for d in d_latest if d.get("alert_status") == "warning")
    d_crit = sum(1 for d in d_latest if d.get("alert_status") in ("critical", "expired"))

    text = (
        f"📊 <b>Overall Stats</b>\n\n"
        f"<b>🔥 Warmup:</b>\n"
        f"  Sites: {len(WARMUP_SITES)}  Runs: {total_runs}  Pages: {total_pages}\n\n"
        f"<b>🔍 Uptime:</b>\n"
        f"  Sites: {len(UPTIME_SITES)}  Online: {online}/{len(UPTIME_SITES)}\n"
        f"  Checks: {total_checks}  Avg uptime: {avg_up:.2f}%\n\n"
        f"<b>🌐 Domains:</b>\n"
        f"  Total: {len(MONITORED_DOMAINS)}  🟢 {d_ok}  🟡 {d_warn}  🔴 {d_crit}\n\n"
        f"<b>⚙️ Schedule:</b>\n"
        f"  Warmup: {'ON' if warmup_enabled else 'OFF'} / {WARMUP_INTERVAL} min\n"
        f"  Uptime: {'ON' if uptime_monitoring_enabled else 'OFF'} / {UPTIME_CHECK_INTERVAL} min\n"
        f"  Domains: {DOMAIN_CHECK_INTERVAL} min"
    )
    keyboard = [[btn("◀️ Back", "back_to_menu")]]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_settings(query):
    keyboard = [
        [btn(
            f"🔥 Auto-warmup: {'✅ ON' if warmup_enabled else '❌ OFF'}",
            "toggle_warmup",
            "success" if warmup_enabled else "danger",
        )],
        [btn(
            f"🔍 Monitoring: {'✅ ON' if uptime_monitoring_enabled else '❌ OFF'}",
            "toggle_uptime",
            "success" if uptime_monitoring_enabled else "danger",
        )],
        [btn(
            f"🖥 Resource alerts: {'✅ ON' if resource_monitoring_enabled else '❌ OFF'}",
            "toggle_resources",
            "success" if resource_monitoring_enabled else "danger",
        )],
        [btn(
            f"🔕 Silent mode: {'✅ ON' if silent_mode else '❌ OFF'}",
            "toggle_silent",
            "success" if silent_mode else "danger",
        )],
        [btn("📋 Check journal", "check_journal")],
        [btn("◀️ Back", "back_to_menu")],
    ]
    text = (
        f"⚙️ <b>Settings</b>\n\n"
        f"🔥 Warmup sites: {len(WARMUP_SITES)}\n"
        f"🔍 Uptime sites: {len(UPTIME_SITES)}\n"
        f"🌐 Domains: {len(MONITORED_DOMAINS)}\n\n"
        f"⏱ Warmup interval: {WARMUP_INTERVAL} min\n"
        f"⏱ Uptime interval: {UPTIME_CHECK_INTERVAL} min\n"
        f"⏱ Domain check: {DOMAIN_CHECK_INTERVAL} min\n"
        f"⏱ Resource check: {RESOURCE_CHECK_INTERVAL} min "
        f"(alert after {RESOURCE_ALERT_CHECKS} checks)\n"
        f"🗑 Log retention: {LOG_RETENTION_DAYS} days\n\n"
        f"<b>Thresholds:</b>\n"
        f"🖥 Host — CPU {THRESHOLD_CPU}% | RAM {THRESHOLD_RAM}% | Disk {THRESHOLD_DISK}%\n"
        f"🐳 Container — CPU {THRESHOLD_CONTAINER_CPU}% | Mem {THRESHOLD_CONTAINER_MEM}%\n"
        f"🙈 Ignored containers: {len(IGNORED_DOCKER_CONTAINERS)}\n\n"
        f"<i>Silent mode — scheduled checks send only alerts,\n"
        f"no success messages. Manual checks always report.</i>"
    )
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def toggle_warmup(query):
    global warmup_enabled
    warmup_enabled = not warmup_enabled
    db.log_event("INFO", "system", f"Auto-warmup {'enabled' if warmup_enabled else 'disabled'}")
    await show_settings(query)


async def toggle_uptime(query):
    global uptime_monitoring_enabled
    uptime_monitoring_enabled = not uptime_monitoring_enabled
    db.log_event("INFO", "system", f"Uptime monitoring {'enabled' if uptime_monitoring_enabled else 'disabled'}")
    await show_settings(query)


async def toggle_resources(query):
    global resource_monitoring_enabled
    resource_monitoring_enabled = not resource_monitoring_enabled
    db.log_event("INFO", "system", f"Resource monitoring {'enabled' if resource_monitoring_enabled else 'disabled'}")
    await show_settings(query)


async def toggle_silent_mode(query):
    global silent_mode
    silent_mode = not silent_mode
    db.log_event("INFO", "system", f"Silent mode {'enabled' if silent_mode else 'disabled'}")
    await show_settings(query)


async def show_check_journal(query):
    times = db.get_last_check_times()

    def fmt(dt):
        if dt is None:
            return "never"
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    text = (
        f"📋 <b>Check journal</b>\n\n"
        f"🔍 Uptime check: {fmt(times['uptime'])}\n"
        f"🔥 Cache warmup: {fmt(times['warmup'])}\n"
        f"🌐 Domain check: {fmt(times['domain'])}\n\n"
        f"🔕 Silent mode: {'ON' if silent_mode else 'OFF'}\n"
        f"🗑 Log retention: {LOG_RETENTION_DAYS} days"
    )
    keyboard = [[btn("◀️ Back", "settings")]]
    await _edit(query, text, InlineKeyboardMarkup(keyboard))


async def handle_back(query):
    await _send_main_menu(lambda text, **kw: query.edit_message_text(text, **kw))


async def handle_warmup_now(query, idx):
    if idx >= len(WARMUP_SITES):
        return
    if _warmup_lock.locked():
        await _edit(query, "⚠️ Warmup is already running — please wait.")
        return
    site = WARMUP_SITES[idx]
    await _edit(query, f"🔥 Starting warmup...\n🌐 {site}")
    threading.Thread(target=_run_warmup, args=(query.message.chat_id, site), daemon=True).start()


async def handle_warmup_all(query):
    if _warmup_lock.locked():
        await _edit(query, "⚠️ Warmup is already running — please wait.")
        return
    await _edit(query, f"🔥 Starting warmup for {len(WARMUP_SITES)} sites...")
    threading.Thread(target=_run_warmup_all, args=(query.message.chat_id,), daemon=True).start()


async def handle_check_now(query, idx):
    if idx >= len(UPTIME_SITES):
        return
    site = UPTIME_SITES[idx]
    await _edit(query, f"🔍 Checking {site}...")
    threading.Thread(target=_run_uptime_check, args=(query.message.chat_id, site, True), daemon=True).start()


async def handle_check_all(query):
    if _uptime_lock.locked():
        await _edit(query, "⚠️ Uptime check is already running — please wait.")
        return
    await _edit(query, f"🔍 Checking {len(UPTIME_SITES)} sites...")
    threading.Thread(target=_run_uptime_check_all, args=(query.message.chat_id,), daemon=True).start()


async def handle_check_domain(query, idx):
    if idx >= len(MONITORED_DOMAINS):
        return
    domain = MONITORED_DOMAINS[idx]
    await _edit(query, f"🔍 Checking domain {domain}...")
    threading.Thread(target=_run_domain_check, args=(query.message.chat_id, domain), daemon=True).start()


async def handle_check_all_domains(query):
    await _edit(query, f"🔍 Checking {len(MONITORED_DOMAINS)} domains...")
    threading.Thread(target=_run_domain_check_all, args=(query.message.chat_id,), daemon=True).start()


def _send(chat_id: int, text: str):
    notifier.send_message(chat_id, text)


def _run_warmup(chat_id: int, site_url: str):
    with _warmup_lock:
        try:
            warmer = CacheWarmer(site_url)

            def progress(cur, total, pct):
                if cur % 50 == 0:
                    _send(chat_id, f"⏳ {site_url}\nProgress: {cur}/{total} ({pct:.1f}%)")

            result = warmer.run_warmup(progress_callback=progress)
            if result["success"]:
                rate = result["successful"] / result["total_urls"] * 100 if result["total_urls"] else 0
                msg = (
                    f"✅ <b>Warmup complete</b>\n\n"
                    f"🌐 {site_url}\n"
                    f"Pages: {result['total_urls']}  ✅ {result['successful']} ({rate:.1f}%)  "
                    f"❌ {result['failed']}\n"
                    f"Time: {format_time(result['duration'])}  "
                    f"Avg load: {result['avg_load_time']:.2f}s"
                )
            else:
                msg = f"❌ Warmup failed\n🌐 {site_url}\n{result.get('message', '')}"
            _send(chat_id, msg)
        except Exception as e:
            logger.error("Warmup error %s: %s", site_url, e, exc_info=True)
            _send(chat_id, f"❌ Warmup error: {e}\n🌐 {site_url}")


def _run_warmup_all(chat_id: int, is_scheduled: bool = False):
    with _warmup_lock:
        quiet = silent_mode and is_scheduled
        report: list[str] = []
        problems = 0  # sites that could not be warmed at all (page-level 404s are not counted)
        for i, site in enumerate(WARMUP_SITES, 1):
            if not quiet:
                _send(chat_id, f"🔥 Warming {i}/{len(WARMUP_SITES)}: {escape(site)}")
            try:
                r = CacheWarmer(site).run_warmup()
            except Exception as e:
                logger.error("Warmup error %s: %s", site, e)
                problems += 1
                report.append(f"❌ {escape(site)}: {escape(str(e))}")
            else:
                if r["success"]:
                    icon = "✅" if not r["failed"] else "⚠️"
                    report.append(
                        f"{icon} {escape(site)}: {r['successful']}/{r['total_urls']} pages"
                        + (f", {r['failed']} failed" if r["failed"] else "")
                    )
                else:
                    problems += 1
                    report.append(f"❌ {escape(site)}: {escape(r.get('message') or 'failed')}")
            if i < len(WARMUP_SITES):
                time.sleep(5)

        body = "\n".join(report)
        if problems:
            _send(chat_id, f"⚠️ <b>Warmup finished with problems</b>\n\n{body}")
        elif not quiet:
            _send(chat_id, f"✅ <b>Warmup finished</b>\n\n{body}")


def _run_uptime_check(chat_id: int, site_url: str, manual: bool = False):
    try:
        result = uptime_monitor.check_site(site_url)
        if manual:
            icon = "🟢" if result["status"] == "online" else "🔴"
            msg = (
                f"{icon} <b>Check result</b>\n\n"
                f"🌐 {site_url}\n"
                f"Status: <b>{result['status'].upper()}</b>\n"
                f"Code: {result['status_code']}\n"
                f"Response: {result['response_time']:.2f}s"
            )
            if result["error"]:
                msg += f"\nError: {result['error']}"
            _send(chat_id, msg)
    except Exception as e:
        logger.error("Uptime check error %s: %s", site_url, e)
        if manual:
            _send(chat_id, f"❌ Check error: {e}\n🌐 {site_url}")


def _run_uptime_check_all(chat_id: int, is_scheduled: bool = False):
    with _uptime_lock:
        results = []
        failures = []
        for site in UPTIME_SITES:
            _run_uptime_check(chat_id, site, False)
            s = db.get_uptime_stats(site)
            status = s.get("last_status", "unknown")
            icon = "🟢" if status == "online" else "🔴"
            results.append(f"{icon} {site[:40]}")
            if status != "online":
                failures.append(f"🔴 {site[:40]}")
            time.sleep(2)
        quiet = silent_mode and is_scheduled
        if quiet:
            if failures:
                _send(chat_id, "⚠️ <b>Issues detected</b>\n\n" + "\n".join(failures))
        else:
            _send(chat_id, "✅ <b>All checks done</b>\n\n" + "\n".join(results))


def _run_domain_check(chat_id: int, domain: str):
    with _domain_lock:
        try:
            domain_monitor.check_domain(domain)
            text = DomainMonitor.format_domain_report(domain)
            _send(chat_id, text)
        except Exception as e:
            logger.error("Domain check error %s: %s", domain, e)
            _send(chat_id, f"❌ Domain check error: {e}\n🌐 {domain}")


def _run_domain_check_all(chat_id: int):
    with _domain_lock:
        try:
            domain_monitor.check_all_domains()
            text = DomainMonitor.format_all_domains_report()
            _send(chat_id, text)
        except Exception as e:
            logger.error("Domain check error: %s", e)
            _send(chat_id, f"❌ Error: {e}")


def _scheduled_warmup():
    if not warmup_enabled or _warmup_lock.locked():
        return
    if not ADMIN_IDS:
        logger.warning("No ADMIN_IDS configured — skipping scheduled warmup")
        return
    logger.info("Scheduled warmup starting")
    threading.Thread(target=_run_warmup_all, args=(ADMIN_IDS[0], True), daemon=True).start()


def _scheduled_uptime():
    if not uptime_monitoring_enabled or _uptime_lock.locked():
        return
    if not ADMIN_IDS:
        logger.warning("No ADMIN_IDS configured — skipping scheduled uptime check")
        return
    logger.info("Scheduled uptime check")
    threading.Thread(target=_run_uptime_check_all, args=(ADMIN_IDS[0], True), daemon=True).start()


def _scheduled_domain():
    if _domain_lock.locked():
        return
    if not ADMIN_IDS:
        logger.warning("No ADMIN_IDS configured — skipping scheduled domain check")
        return
    logger.info("Scheduled domain check")
    threading.Thread(target=_domain_scheduled_work, daemon=True).start()


def _domain_scheduled_work():
    with _domain_lock:
        try:
            domain_monitor.check_all_domains()
            domain_monitor.check_and_send_alerts()
        except Exception as e:
            logger.error("Scheduled domain check error: %s", e)


def _scheduled_resources():
    if not resource_monitoring_enabled or _resource_lock.locked():
        return
    threading.Thread(target=_resource_scheduled_work, daemon=True).start()


def _resource_scheduled_work():
    with _resource_lock:
        try:
            system_monitor.check()
        except Exception as e:
            logger.error("Host resource check error: %s", e, exc_info=True)
        if DOCKER_MONITORING:
            try:
                docker_monitor.check()
            except Exception as e:
                logger.error("Docker check error: %s", e, exc_info=True)


def _scheduled_log_rotation():
    """Delete old database records to prevent unbounded growth."""
    logger.info("Running log rotation (%d days retention)", LOG_RETENTION_DAYS)
    try:
        db.cleanup_old_records(LOG_RETENTION_DAYS)
    except Exception as e:
        logger.error("Log rotation error: %s", e)


def _run_scheduler():
    schedule.every(WARMUP_INTERVAL).minutes.do(_scheduled_warmup)
    schedule.every(UPTIME_CHECK_INTERVAL).minutes.do(_scheduled_uptime)
    schedule.every(DOMAIN_CHECK_INTERVAL).minutes.do(_scheduled_domain)
    schedule.every(RESOURCE_CHECK_INTERVAL).minutes.do(_scheduled_resources)
    schedule.every().day.at("03:00").do(_scheduled_log_rotation)

    logger.info(
        "Scheduler started: warmup/%dm  uptime/%dm  domains/%dm  resources/%dm  log-rotation/daily@03:00",
        WARMUP_INTERVAL, UPTIME_CHECK_INTERVAL, DOMAIN_CHECK_INTERVAL, RESOURCE_CHECK_INTERVAL,
    )
    while True:
        try:
            schedule.run_pending()
        except Exception:  # a failing job must never kill the scheduler thread
            logger.exception("Scheduler error")
        time.sleep(30)


HEALTH_FILE = "/tmp/bot_healthy"


def _touch_health():
    """Create/update a file so the Docker HEALTHCHECK can verify the process is alive."""
    try:
        with open(HEALTH_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


async def _health_loop() -> None:
    """Touch the health file; it goes stale if the event loop or scheduler dies."""
    while True:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            _touch_health()
        await asyncio.sleep(30)


async def _post_init(application: Application) -> None:
    application.bot_data["health_task"] = asyncio.create_task(_health_loop())


def main():
    global notifier, uptime_monitor, domain_monitor, system_monitor, docker_monitor, _scheduler_thread

    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        sys.exit(1)

    if not ADMIN_IDS:
        logger.error("ADMIN_USER_IDS is not set — check .env")
        sys.exit(1)

    if not WARMUP_SITES and not UPTIME_SITES and not MONITORED_DOMAINS and not DOCKER_MONITORING:
        logger.error("Nothing to monitor: configure sites/domains or enable DOCKER_MONITORING")
        sys.exit(1)

    db.init_db()
    db.log_event("INFO", "system", "Bot starting")

    notifier = TelegramNotifier(BOT_TOKEN, ADMIN_IDS)
    uptime_monitor = UptimeMonitor(notifier)
    domain_monitor = DomainMonitor(notifier)
    system_monitor = SystemMonitor(notifier)
    docker_monitor = DockerMonitor(notifier)

    def _shutdown(signum, frame):
        logger.info("Received signal %s — shutting down", signum)
        db.log_event("INFO", "system", "Bot shutting down (signal)")
        db.close_db()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _scheduler_thread = threading.Thread(target=_run_scheduler, daemon=True)
    _scheduler_thread.start()

    # first resource check right after start
    threading.Timer(10, _scheduled_resources).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(_post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(error_handler)

    notifier.broadcast(
        f"🌙 <b>Moon Watcher started</b> v{__version__}\n\n"
        f"🔥 Warmup: {len(WARMUP_SITES)} sites / {WARMUP_INTERVAL} min\n"
        f"🔍 Uptime: {len(UPTIME_SITES)} sites / {UPTIME_CHECK_INTERVAL} min\n"
        f"🌐 Domains: {len(MONITORED_DOMAINS)} / {DOMAIN_CHECK_INTERVAL} min\n"
        f"🖥 Resources: host + Docker / {RESOURCE_CHECK_INTERVAL} min"
        + ("" if DOCKER_MONITORING else " (Docker OFF)")
    )

    logger.info("Bot ready — polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
