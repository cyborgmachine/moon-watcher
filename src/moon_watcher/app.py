"""Moon Watcher: Telegram bot for monitoring websites, domains, Docker and VPS resources."""

import asyncio
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from moon_watcher import __version__, jobs, store
from moon_watcher import database as db
from moon_watcher.config import (
    ADMIN_IDS,
    BOT_TOKEN,
    DOCKER_MONITORING,
    DOMAIN_CHECK_INTERVAL,
    MONITORED_DOMAINS,
    RESOURCE_CHECK_INTERVAL,
    SSL_CHECK_INTERVAL,
    SSL_MONITORING,
    UPTIME_CHECK_INTERVAL,
    UPTIME_SITES,
    WARMUP_INTERVAL,
    WARMUP_SITES,
)
from moon_watcher.handlers import cmd_start, error_handler, on_callback
from moon_watcher.monitors.docker_monitor import DockerMonitor
from moon_watcher.monitors.domain import DomainMonitor
from moon_watcher.monitors import tls
from moon_watcher.monitors.system import SystemMonitor
from moon_watcher.monitors.tls import CertMonitor
from moon_watcher.monitors.uptime import UptimeMonitor
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.scheduler import Scheduler
from moon_watcher.services import Services
from moon_watcher.settings import Settings
from moon_watcher.ui.main import paused_lines
from moon_watcher.utils.fmt import fmt_elapsed

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HEALTH_FILE = "/tmp/bot_healthy"
# no tick from the event loop or the scheduler for this long = the process is wedged
FROZEN_AFTER = 300
# a heartbeat older than this at start means the bot was down, not just restarting
OFFLINE_REPORT_AFTER = 180


class Watchdog:
    """Keeps the health file fresh and exits if the process hangs.

    Docker doesn't restart unhealthy containers by itself, exiting is what makes
    `restart: unless-stopped` kick in.
    """

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.loop_tick = time.monotonic()

    async def beat(self) -> None:
        while True:
            self.loop_tick = time.monotonic()
            await asyncio.sleep(10)

    def start(self) -> None:
        threading.Thread(target=self._run, name="watchdog", daemon=True).start()

    def _run(self) -> None:
        while True:
            now = time.monotonic()
            frozen = []
            if now - self.loop_tick > FROZEN_AFTER:
                frozen.append("telegram event loop")
            if not self.scheduler.is_alive() or now - self.scheduler.last_tick > FROZEN_AFTER:
                frozen.append("scheduler")
            if frozen:
                logger.critical("%s stopped responding, exiting so Docker restarts the bot", " and ".join(frozen))
                os._exit(1)
            try:
                with open(HEALTH_FILE, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
            time.sleep(15)


def startup_message(svc: Services, last_beat: float | None) -> str:
    lines = [f"🌙 <b>Moon Watcher started</b> v{__version__}", ""]
    if last_beat and time.time() - last_beat > OFFLINE_REPORT_AFTER:
        since = datetime.fromtimestamp(last_beat, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines += [
            f"⚠️ <b>The bot was offline for {fmt_elapsed(time.time() - last_beat)}</b> (since {since}).",
            "Nothing was monitored during that time.",
            "",
        ]
    lines += [
        f"🔍 Uptime: {len(UPTIME_SITES)} sites / {UPTIME_CHECK_INTERVAL} min",
        f"🔥 Warmup: {len(WARMUP_SITES)} sites / {WARMUP_INTERVAL} min",
        f"🌐 Domains: {len(MONITORED_DOMAINS)} / {DOMAIN_CHECK_INTERVAL} min",
        f"🔒 SSL: {len(tls.TARGETS)} hosts / {SSL_CHECK_INTERVAL} min" if SSL_MONITORING else "🔒 SSL: off",
        f"🖥 Resources: host{' + Docker' if DOCKER_MONITORING else ''} / {RESOURCE_CHECK_INTERVAL} min",
    ]
    paused = paused_lines(svc)
    if paused:
        lines += ["", paused]
    return "\n".join(lines)


def build_services() -> Services:
    notifier = TelegramNotifier(BOT_TOKEN, ADMIN_IDS)
    notifier.load_state()
    settings = Settings()
    settings.load()

    svc = Services(
        notifier=notifier,
        settings=settings,
        scheduler=Scheduler(),
        uptime=UptimeMonitor(notifier),
        domains=DomainMonitor(notifier),
        system=SystemMonitor(notifier),
        docker=DockerMonitor(notifier),
        certs=CertMonitor(notifier),
    )
    svc.scheduler.on_stall = partial(jobs.stalled, svc)
    svc.scheduler.on_unstall = partial(jobs.unstalled, svc)
    svc.system.load_state()
    svc.docker.load_state()
    svc.certs.load_state()
    jobs.register(svc)
    return svc


def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set")
        sys.exit(1)
    if not ADMIN_IDS:
        logger.error("ADMIN_USER_IDS is not set, check .env")
        sys.exit(1)
    if not WARMUP_SITES and not UPTIME_SITES and not MONITORED_DOMAINS and not DOCKER_MONITORING:
        logger.error("Nothing to monitor: configure sites/domains or enable DOCKER_MONITORING")
        sys.exit(1)

    db.init_db()
    db.log_event("INFO", "system", "Bot starting")
    last_beat = store.load(jobs.HEARTBEAT_KEY)
    svc = build_services()
    watchdog = Watchdog(svc.scheduler)

    async def post_init(application: Application) -> None:
        application.bot_data["watchdog_beat"] = asyncio.create_task(watchdog.beat())

    async def post_shutdown(application: Application) -> None:
        db.log_event("INFO", "system", "Bot stopped")
        db.close_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["services"] = svc
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_error_handler(error_handler)

    svc.notifier.broadcast(startup_message(svc, last_beat))
    svc.scheduler.start()
    watchdog.start()

    logger.info("Bot ready, polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
