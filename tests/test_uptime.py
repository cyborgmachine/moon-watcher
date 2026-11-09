"""Uptime incident logic: confirmation, restart-safety, undelivered alerts."""

from datetime import timedelta
from unittest import mock

from moon_watcher.monitors import uptime as up

URL = "https://example.com"
ONLINE = {"status": "online", "status_code": 200, "error": None, "response_time": 0.1}
OFFLINE = {"status": "offline", "status_code": 0, "error": "Timeout", "response_time": 15.0}


class FakeDB:
    """In-memory stand-in for the incident-related parts of database.py."""

    def __init__(self, incident=None):
        self.incident = incident
        self.saved = []

    def save_uptime_check(self, *a, **k):
        self.saved.append(a)

    def get_open_incident(self, url):
        return dict(self.incident) if self.incident else None

    def open_downtime_incident(self, url):
        self.incident = {"id": 1, "alert_sent": False}
        return 1

    def close_downtime_incident(self, url):
        self.incident = None
        return timedelta(seconds=125)

    def mark_incident_alerted(self, incident_id):
        self.incident["alert_sent"] = True

    def log_event(self, *a, **k):
        pass


class FakeNotifier:
    def __init__(self, deliver=True):
        self.deliver = deliver
        self.sent = []

    def send_alert(self, text, site_url="", event_type="generic", cooldown=True):
        self.sent.append((event_type, cooldown))
        return self.deliver


def run(fake_db, notifier, probes, threshold=2):
    """Run one check_site() with the given sequence of probe results."""
    mon = up.UptimeMonitor(notifier)
    it = iter(probes)
    mon._probe = lambda url: next(it)
    with mock.patch.object(up, "db", fake_db), \
            mock.patch.object(up, "UPTIME_FAIL_THRESHOLD", threshold), \
            mock.patch.object(up.time, "sleep"):
        return mon.check_site(URL)


def test_blip_is_not_reported():
    db, n = FakeDB(), FakeNotifier()
    run(db, n, [OFFLINE, ONLINE])          # fails once, confirmation probe succeeds
    assert n.sent == [] and db.incident is None


def test_confirmed_outage_alerts_once_then_recovers():
    db, n = FakeDB(), FakeNotifier()
    run(db, n, [OFFLINE, OFFLINE])
    run(db, n, [OFFLINE, OFFLINE])         # still down: no second alert
    run(db, n, [ONLINE])
    assert n.sent == [("downtime", False), ("recovery", False)]   # transitions bypass the cooldown
    assert db.incident is None


def test_recovery_after_bot_restart_closes_incident():
    db = FakeDB({"id": 7, "alert_sent": True})     # incident opened by the previous bot process
    n = FakeNotifier()
    run(db, n, [ONLINE])                           # brand-new monitor instance, no in-memory state
    assert db.incident is None
    assert n.sent == [("recovery", False)]


def test_no_duplicate_alert_after_restart_while_still_down():
    db = FakeDB({"id": 7, "alert_sent": True})
    n = FakeNotifier()
    run(db, n, [OFFLINE, OFFLINE])
    assert n.sent == []


def test_undelivered_down_alert_is_retried():
    db, n = FakeDB(), FakeNotifier(deliver=False)
    run(db, n, [OFFLINE, OFFLINE])
    assert db.incident["alert_sent"] is False
    n.deliver = True
    run(db, n, [OFFLINE, OFFLINE])
    assert db.incident["alert_sent"] is True
    assert [t for t, _ in n.sent] == ["downtime", "downtime"]


def test_threshold_one_alerts_immediately():
    db, n = FakeDB(), FakeNotifier()
    run(db, n, [OFFLINE], threshold=1)
    assert [t for t, _ in n.sent] == ["downtime"]
