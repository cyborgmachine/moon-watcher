"""Screens and button routing, without Telegram or a database."""

import asyncio
import re
from unittest import mock

import pytest

from moon_watcher import handlers
from moon_watcher import settings as st
from moon_watcher.monitors.tls import CertMonitor
from moon_watcher.scheduler import Job, Scheduler
from moon_watcher.services import Services
from moon_watcher.ui import common
from moon_watcher.ui import main as main_ui
from moon_watcher.ui import sites

SITES = ["https://a.example", "https://b.example", "https://c.example"]


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(st.Saved, "load", lambda self, default: default)
    monkeypatch.setattr(st.Saved, "save", lambda self, value: None)
    for module in (sites, main_ui):
        monkeypatch.setattr(module, "UPTIME_SITES", SITES)
    uptime = mock.Mock()
    uptime.is_down.side_effect = lambda url: url == SITES[1]
    scheduler = Scheduler()
    scheduler.add(Job("uptime", "Uptime check", lambda *a: None, interval=900))
    return Services(
        notifier=mock.Mock(), settings=st.Settings(), scheduler=scheduler,
        uptime=uptime, domains=mock.Mock(), system=mock.Mock(), docker=mock.Mock(),
        certs=CertMonitor(mock.Mock()),
    )


def buttons(screen):
    return [b for row in screen[1].inline_keyboard for b in row]


def test_item_ids_are_stable_and_findable():
    iid = common.item_id(SITES[2])
    assert iid == common.item_id(SITES[2]) and len(iid) == 10
    assert common.find(SITES, iid) == SITES[2]
    assert common.find(SITES, "nope") is None


def test_pager():
    assert common.pager("u", 0, 1) == []
    assert [b.callback_data for b in common.pager("u", 1, 3)] == ["u:0", "noop", "u:2"]


def test_main_menu_shows_down_sites_and_pauses(svc):
    svc.settings.pause("uptime", 0)
    text, _ = main_ui.main_menu(svc)
    assert "1 down" in text
    assert "Uptime monitoring: off until resumed" in text


def test_settings_offers_pause_and_resume(svc):
    svc.settings.pause("warmup", 60)
    labels = {b.callback_data: b.text for b in buttons(main_ui.settings(svc))}
    assert "paused until" in labels["set:warmup"]
    assert "ON" in labels["set:uptime"]
    pause = [b.callback_data for b in buttons(main_ui.pause_menu(svc, "uptime"))]
    assert pause[:4] == ["pause:uptime:60", "pause:uptime:360", "pause:uptime:1440", "pause:uptime:0"]


def db_down(*args):
    raise sites.db.DatabaseUnavailable("down")


def test_uptime_list_works_without_database(svc, monkeypatch):
    monkeypatch.setattr(sites.db, "last_uptime_status", db_down)
    text, markup = sites.uptime_list(svc, 0)
    assert "unavailable" in text
    labels = [b.text for b in buttons((text, markup))]
    assert any(label.startswith("🔴") and SITES[1][:40] in label for label in labels)


def test_journal(svc):
    text, _ = main_ui.journal(svc)
    assert "Uptime check" in text and "never" in text


def test_callback_data_fits_telegram_limit(svc, monkeypatch):
    monkeypatch.setattr(sites, "UPTIME_SITES", [f"https://{'x' * 200}.example/{i}" for i in range(20)])
    monkeypatch.setattr(sites.db, "last_uptime_status", lambda: {})
    monkeypatch.setattr(sites.tls, "TARGETS", [f"{'y' * 60}{i}.example" for i in range(20)])
    screens = (sites.uptime_list(svc, 1), sites.ssl_list(svc, 2), main_ui.settings(svc), main_ui.main_menu(svc))
    for screen in screens:
        assert all(len(b.callback_data.encode()) <= 64 for b in buttons(screen))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = mock.Mock(id=1)
        self.message = mock.Mock(chat_id=1)
        self.answer = mock.AsyncMock()
        self.edit_message_text = mock.AsyncMock()


def press(svc, data):
    query = FakeQuery(data)
    update = mock.Mock(callback_query=query)
    context = mock.Mock(bot_data={"services": svc})
    with mock.patch.object(handlers, "ADMIN_IDS", [1]):
        asyncio.run(handlers.on_callback(update, context))
    return query.edit_message_text.call_args


def test_unknown_button_opens_main_menu(svc):
    args = press(svc, "uptime_site_3")
    assert "Moon Watcher" in args[0][0]


def test_database_error_shows_retry(svc, monkeypatch):
    monkeypatch.setattr(sites.db, "get_all_domains_latest", db_down)
    args = press(svc, "d:0")
    assert "database is not reachable" in args[0][0]
    assert args[1]["reply_markup"].inline_keyboard[0][0].callback_data == "d:0"


def test_pause_button_pauses(svc):
    with mock.patch.object(handlers.db, "log_event"):
        press(svc, "pause:uptime:60")
    assert not svc.settings.is_enabled("uptime")


def test_ssl_screens(svc, monkeypatch):
    monkeypatch.setattr(sites.tls, "TARGETS", ["a.example", "b.example:8443"])
    svc.certs.results["a.example"] = {"status": "invalid", "error": "certificate has expired",
                                      "checked_at": "2026-11-11T10:00:00+00:00"}
    text = press(svc, "s:0")[0][0]
    assert "Hosts: 2" in text and "🔴 1" in text
    detail = press(svc, "ss:" + common.item_id("a.example"))[0][0]
    assert "certificate has expired" in detail


def test_check_all_does_not_start_twice(svc):
    svc.scheduler.jobs["uptime"].running_since = 1.0
    args = press(svc, "ua")
    assert "already running" in args[0][0]


def test_screens_are_valid_telegram_html(svc):
    svc.settings.pause("resources", 0)
    for text, _ in (main_ui.main_menu(svc), main_ui.settings(svc), main_ui.journal(svc),
                    main_ui.pause_menu(svc, "resources")):
        assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#)", text), text
