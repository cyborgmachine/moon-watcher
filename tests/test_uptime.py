"""Uptime incident logic: confirmation, restart safety, undelivered alerts, dead database."""

import threading
import time
from datetime import datetime, timezone
from unittest import mock

from moon_watcher.monitors import uptime as up

URL = "https://example.com"
ONLINE = {"status": "online", "status_code": 200, "error": None, "response_time": 0.1}
OFFLINE = {"status": "offline", "status_code": 0, "error": "Timeout", "response_time": 15.0}


class FakeDB:
    """The incident part of database.py, kept in a dict."""

    def __init__(self, open_incident=None):
        self.open = {}
        if open_incident:
            self.open[URL] = {"id": 7, "site_url": URL, "started_at": datetime.now(timezone.utc),
                              **open_incident}
        self.down = False
        self.saved = 0
        self.next_id = 100

    def _check(self):
        if self.down:
            raise up.db.DatabaseUnavailable("down")

    def save_uptime_check(self, *a):
        self._check()
        self.saved += 1

    def get_open_incidents(self):
        self._check()
        return {k: dict(v) for k, v in self.open.items()}

    def open_downtime_incident(self, url, started_at):
        self._check()
        if url not in self.open:
            self.next_id += 1
            self.open[url] = {"id": self.next_id, "site_url": url, "started_at": started_at, "alert_sent": False}
        return self.open[url]["id"]

    def close_downtime_incident(self, url):
        self._check()
        self.open.pop(url, None)

    def mark_incident_alerted(self, incident_id):
        self._check()
        for inc in self.open.values():
            if inc["id"] == incident_id:
                inc["alert_sent"] = True

    def log_event(self, *a, **k):
        pass


class FakeNotifier:
    def __init__(self, deliver=True):
        self.deliver = deliver
        self.sent = []

    def send_alert(self, text, site_url="", event_type="generic", cooldown=True):
        self.sent.append(event_type)
        return self.deliver


def check(mon, fake_db, probes, threshold=2):
    it = iter(probes)
    mon._probe = lambda url: next(it)
    with mock.patch.object(up, "db", fake_db), \
            mock.patch.object(up, "UPTIME_FAIL_THRESHOLD", threshold), \
            mock.patch.object(up.time, "sleep"):
        return mon.check_site(URL)


def test_blip_is_not_reported():
    db, n = FakeDB(), FakeNotifier()
    check(up.UptimeMonitor(n), db, [OFFLINE, ONLINE])
    assert n.sent == [] and db.open == {}


def test_confirmed_outage_alerts_once_then_recovers():
    db, n = FakeDB(), FakeNotifier()
    mon = up.UptimeMonitor(n)
    check(mon, db, [OFFLINE, OFFLINE])
    check(mon, db, [OFFLINE, OFFLINE])
    assert db.open[URL]["alert_sent"] is True
    check(mon, db, [ONLINE])
    assert n.sent == ["downtime", "recovery"]
    assert db.open == {}


def test_recovery_after_bot_restart_closes_incident():
    db, n = FakeDB({"alert_sent": True}), FakeNotifier()
    check(up.UptimeMonitor(n), db, [ONLINE])
    assert db.open == {} and n.sent == ["recovery"]


def test_no_duplicate_alert_after_restart_while_still_down():
    db, n = FakeDB({"alert_sent": True}), FakeNotifier()
    check(up.UptimeMonitor(n), db, [OFFLINE, OFFLINE])
    assert n.sent == []


def test_undelivered_down_alert_is_retried():
    db, n = FakeDB(), FakeNotifier(deliver=False)
    mon = up.UptimeMonitor(n)
    check(mon, db, [OFFLINE, OFFLINE])
    assert db.open[URL]["alert_sent"] is False
    n.deliver = True
    check(mon, db, [OFFLINE, OFFLINE])
    assert db.open[URL]["alert_sent"] is True
    assert n.sent == ["downtime", "downtime"]


def test_threshold_one_alerts_immediately():
    db, n = FakeDB(), FakeNotifier()
    check(up.UptimeMonitor(n), db, [OFFLINE], threshold=1)
    assert n.sent == ["downtime"]


def test_alerts_work_while_database_is_down():
    db, n = FakeDB(), FakeNotifier()
    db.down = True
    mon = up.UptimeMonitor(n)
    check(mon, db, [OFFLINE, OFFLINE])
    check(mon, db, [OFFLINE, OFFLINE])
    check(mon, db, [ONLINE])
    assert n.sent == ["downtime", "recovery"]


def test_incident_reaches_database_once_it_is_back():
    db, n = FakeDB(), FakeNotifier()
    db.down = True
    mon = up.UptimeMonitor(n)
    check(mon, db, [OFFLINE, OFFLINE])
    db.down = False
    check(mon, db, [OFFLINE, OFFLINE])
    assert db.open[URL]["alert_sent"] is True
    assert n.sent == ["downtime"]


def test_recovery_during_db_outage_closes_incident_later():
    db, n = FakeDB(), FakeNotifier()
    mon = up.UptimeMonitor(n)
    check(mon, db, [OFFLINE, OFFLINE])
    db.down = True
    check(mon, db, [ONLINE])
    assert URL in db.open
    db.down = False
    check(mon, db, [ONLINE])
    assert db.open == {}
    assert n.sent == ["downtime", "recovery"]


def test_manual_and_scheduled_check_do_not_double_alert():
    db, n = FakeDB(), FakeNotifier()
    mon = up.UptimeMonitor(n)

    def slow_probe(url):
        time.sleep(0.05)
        return OFFLINE

    mon._probe = slow_probe
    with mock.patch.object(up, "db", db), mock.patch.object(up, "UPTIME_FAIL_THRESHOLD", 1):
        threads = [threading.Thread(target=mon.check_site, args=(URL,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert n.sent == ["downtime"]
    assert len(db.open) == 1


def test_check_many_runs_every_site():
    db, n = FakeDB(), FakeNotifier()
    mon = up.UptimeMonitor(n)
    mon._probe = lambda url: OFFLINE if "down" in url else ONLINE
    urls = [f"https://site{i}.test" for i in range(6)] + ["https://down.test"]
    with mock.patch.object(up, "db", db), mock.patch.object(up, "UPTIME_FAIL_THRESHOLD", 1):
        results = mon.check_many(urls)
    assert list(results) == urls
    assert results["https://down.test"]["status"] == "offline"
    assert mon.is_down("https://down.test") and not mon.is_down(urls[0])
