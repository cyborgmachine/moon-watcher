"""Telegram command and button handlers.

Button data looks like "name:arg:arg". Screens and anything else that may touch
the database run in a worker thread so a slow DB can't freeze the bot.
"""

import asyncio
import logging
import threading
from html import escape

import psycopg2
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import ContextTypes

from moon_watcher import database as db
from moon_watcher import jobs
from moon_watcher.config import ADMIN_IDS, MONITORED_DOMAINS, UPTIME_SITES, WARMUP_SITES
from moon_watcher.monitors import tls
from moon_watcher.services import Services
from moon_watcher.settings import MONITORS
from moon_watcher.ui import docker as docker_ui
from moon_watcher.ui import main as main_ui
from moon_watcher.ui import sites
from moon_watcher.ui.common import Screen, btn, find, keyboard, menu_btn, page_arg

logger = logging.getLogger(__name__)

DB_ERRORS = (db.DatabaseUnavailable, db.DatabaseBusy, psycopg2.Error)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["services"]


async def render(data: str, fn, *args) -> Screen:
    try:
        return await asyncio.to_thread(fn, *args)
    except DB_ERRORS as e:
        logger.warning("Screen %s: database error: %s", fn.__name__, e)
        text = ("⚠️ <b>The database is not reachable right now</b>\n\n"
                "Alerts keep working. Try again in a minute.")
    except Exception:
        logger.exception("Screen %s failed", fn.__name__)
        text = "⚠️ <b>Something went wrong</b>, details are in the bot logs."
    return text, keyboard([btn("🔄 Retry", data, "primary")], [menu_btn()])


async def edit(query, screen: Screen) -> None:
    text, markup = screen
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(f"❌ Access denied.\nYour User ID: {user.id}")
        return
    text, markup = await render("menu", main_ui.main_menu, services(context))
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


def in_background(svc: Services, chat_id: int, fn, *args) -> None:
    def run():
        try:
            fn(svc, chat_id, *args)
        except Exception as e:
            logger.exception("%s failed", fn.__name__)
            svc.notifier.send_message(chat_id, f"❌ Failed: {escape(str(e)[:300])}")
    threading.Thread(target=run, daemon=True).start()


def notice(text: str) -> Screen:
    return text, keyboard([menu_btn()])


async def run_job(query, svc: Services, job: str, started: str, busy: str, *args) -> None:
    if svc.scheduler.trigger(job, query.message.chat_id, *args):
        await edit(query, notice(started))
    else:
        await edit(query, notice(busy))


async def warmup_all(query, svc, args):
    await run_job(query, svc, "warmup", f"🔥 Starting warmup for {len(WARMUP_SITES)} sites…",
                  "⚠️ Warmup is already running, please wait.")


async def warmup_one(query, svc, args):
    site = find(WARMUP_SITES, args[0] if args else "")
    if site is None:
        return await show(query, svc, "w:0", sites.warmup_list, 0)
    await run_job(query, svc, "warmup", f"🔥 Starting warmup…\n🌐 {escape(site)}",
                  "⚠️ Warmup is already running, please wait.", site)


async def uptime_all(query, svc, args):
    await run_job(query, svc, "uptime", f"🔍 Checking {len(UPTIME_SITES)} sites…",
                  "⚠️ Uptime check is already running, please wait.")


async def uptime_one(query, svc, args):
    site = find(UPTIME_SITES, args[0] if args else "")
    if site is None:
        return await show(query, svc, "u:0", sites.uptime_list, 0)
    await edit(query, notice(f"🔍 Checking {escape(site)}…"))
    in_background(svc, query.message.chat_id, jobs.check_site, site)


async def domains_all(query, svc, args):
    await run_job(query, svc, "domains", f"🔍 Checking {len(MONITORED_DOMAINS)} domains…",
                  "⚠️ Domain check is already running, please wait.")


async def domain_one(query, svc, args):
    domain = find(MONITORED_DOMAINS, args[0] if args else "")
    if domain is None:
        return await show(query, svc, "d:0", sites.domain_list, 0)
    await edit(query, notice(f"🔍 Checking domain {escape(domain)}…"))
    in_background(svc, query.message.chat_id, jobs.check_domain, domain)


async def ssl_all(query, svc, args):
    await run_job(query, svc, "ssl", f"🔍 Checking {len(tls.TARGETS)} SSL certificates…",
                  "⚠️ SSL check is already running, please wait.")


async def ssl_one(query, svc, args):
    target = find(tls.TARGETS, args[0] if args else "")
    if target is None:
        return await show(query, svc, "s:0", sites.ssl_list, 0)
    await edit(query, notice(f"🔍 Checking the SSL certificate of {escape(target)}…"))
    in_background(svc, query.message.chat_id, jobs.check_certificate, target)


async def settings_action(query, svc: Services, args):
    target = args[0] if args else None
    if target == "silent":
        await asyncio.to_thread(svc.settings.set_silent, not svc.settings.silent)
    elif target in MONITORS:
        if svc.settings.is_enabled(target):
            return await edit(query, main_ui.pause_menu(svc, target))
        await asyncio.to_thread(svc.settings.resume, target)
        await asyncio.to_thread(db.log_event, "INFO", "system", f"{target} resumed")
    await edit(query, await render("set", main_ui.settings, svc))


async def pause_action(query, svc: Services, args):
    if len(args) == 2 and args[0] in MONITORS and args[1].isdigit():
        await asyncio.to_thread(svc.settings.pause, args[0], int(args[1]))
        await asyncio.to_thread(db.log_event, "INFO", "system", f"{args[0]} paused for {args[1]} min")
    await edit(query, await render("set", main_ui.settings, svc))


async def show(query, svc, data: str, fn, *args):
    await edit(query, await render(data, fn, svc, *args))


async def noop(query, svc, args):
    pass


def screen(fn, arg=None):
    async def route(query, svc, args):
        if arg == "page":
            extra = (page_arg(args),)
        elif arg == "id":
            extra = (args[0] if args else "",)
        elif arg == "args":
            extra = (args,)
        else:
            extra = ()
        await show(query, svc, query.data, fn, *extra)
    return route


ROUTES = {
    "menu": screen(main_ui.main_menu),
    "w": screen(sites.warmup_list, "page"),
    "ws": screen(sites.warmup_site, "id"),
    "wa": warmup_all,
    "wn": warmup_one,
    "u": screen(sites.uptime_list, "page"),
    "us": screen(sites.uptime_site, "id"),
    "ua": uptime_all,
    "un": uptime_one,
    "d": screen(sites.domain_list, "page"),
    "ds": screen(sites.domain_site, "id"),
    "da": domains_all,
    "dn": domain_one,
    "s": screen(sites.ssl_list, "page"),
    "ss": screen(sites.ssl_site, "id"),
    "sa": ssl_all,
    "sn": ssl_one,
    "k": screen(docker_ui.docker_menu),
    "kl": screen(docker_ui.docker_list, "args"),
    "kc": screen(docker_ui.container, "args"),
    "kg": screen(docker_ui.container_logs, "args"),
    "sys": screen(main_ui.system),
    "stats": screen(main_ui.stats),
    "set": settings_action,
    "pause": pause_action,
    "jr": screen(main_ui.journal),
    "noop": noop,
}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Access denied", show_alert=True)
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    name, *args = (query.data or "").split(":")
    # buttons from messages sent by an older version land on the main menu
    route = ROUTES.get(name, ROUTES["menu"])
    await route(query, services(context), args)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Network/timeout error, will retry: %s", context.error)
        return
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    logger.error("Unhandled exception:", exc_info=context.error)
    await asyncio.to_thread(db.log_event, "ERROR", "system", str(context.error)[:500])
