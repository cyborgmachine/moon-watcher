"""Domain expiry alerts: thresholds, renewal reset, 0-day / unknown display."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from moon_watcher.monitors import domain as dom


class FakeDB:
    def __init__(self, days):
        self.days = days
        self.status = "warning"
        self.error_streak = 0
        self.sent_levels = set()
        self.reset_calls = 0
        self.checks = []

    def get_latest_domain_info(self, d):
        return {"alert_status": self.status, "days_until_expiry": self.days, "error": "whois timeout",
                "expiration_date": datetime(2030, 1, 1), "registrar": None}

    def domain_error_streak(self, d, limit):
        return min(self.error_streak, limit)

    def clear_domain_alert(self, d, level):
        if level in self.sent_levels:
            self.sent_levels.discard(level)
            return True
        return False

    def should_send_domain_alert(self, d, level):
        return level not in self.sent_levels

    def record_domain_alert(self, d, level):
        self.sent_levels.add(level)

    def reset_domain_alerts(self, d):
        self.reset_calls += 1
        self.sent_levels.clear()

    def save_domain_check(self, **kw):
        self.checks.append(kw)

    def log_event(self, *a, **k):
        pass


class FakeNotifier:
    def __init__(self, deliver=True):
        self.deliver = deliver
        self.sent = []

    def send_alert(self, text, site_url="", event_type="generic", cooldown=True):
        self.sent.append(event_type)
        return self.deliver


def monitor(days, deliver=True):
    db, n = FakeDB(days), FakeNotifier(deliver)
    return dom.DomainMonitor(n), db, n


def run_alerts(mon, db):
    with mock.patch.object(dom, "db", db), \
            mock.patch.object(dom, "MONITORED_DOMAINS", ["example.com"]), \
            mock.patch.object(dom, "DOMAIN_ALERT_DAYS", [30, 14, 7, 3, 1]), \
            mock.patch.object(dom, "DOMAIN_ERROR_ALERT_AFTER", 3):
        return mon.check_and_send_alerts()


def test_days_label():
    assert dom.days_label(0) == "0"          # expires today is not "EXPIRED"
    assert dom.days_label(5) == "5"
    assert dom.days_label(-1) == "EXPIRED"
    assert dom.days_label(None) == "unknown"


def test_tightest_threshold_and_no_repeat():
    mon, db, n = monitor(5)
    assert run_alerts(mon, db) == 1 and db.sent_levels == {7}
    assert run_alerts(mon, db) == 0


def test_expired_gets_its_own_alert():
    mon, db, n = monitor(1)
    run_alerts(mon, db)
    db.days = -2
    assert run_alerts(mon, db) == 1 and db.sent_levels == {1, 0}


def test_failed_send_is_not_recorded_and_retried():
    mon, db, n = monitor(5, deliver=False)
    assert run_alerts(mon, db) == 0 and db.sent_levels == set()
    n.deliver = True
    assert run_alerts(mon, db) == 1


def test_renewal_resets_alert_history():
    mon, db, n = monitor(5)
    exp = datetime.now() + timedelta(days=400)
    fake_whois = SimpleNamespace(expiration_date=exp, creation_date=None, updated_date=None,
                                 name_servers=[], status=[], registrar="X")
    with mock.patch.object(dom, "db", db), \
            mock.patch.object(dom, "DOMAIN_ALERT_DAYS", [30, 14, 7, 3, 1]), \
            mock.patch.object(dom.rdap, "lookup", side_effect=dom.rdap.RdapUnavailable("com")), \
            mock.patch.object(dom.whois, "whois", return_value=fake_whois):
        mon.check_domain("example.com")
    assert db.reset_calls == 1


def test_failing_lookups_are_reported_once_and_cleared():
    mon, db, n = monitor(None)
    db.status = "error"
    db.error_streak = 2
    assert run_alerts(mon, db) == 0
    db.error_streak = 3
    assert run_alerts(mon, db) == 1
    assert run_alerts(mon, db) == 0
    assert n.sent == ["domain_lookup_failing"]

    db.status, db.days = "ok", 300
    run_alerts(mon, db)
    assert n.sent == ["domain_lookup_failing", "domain_lookup_ok"]
    run_alerts(mon, db)
    assert len(n.sent) == 2
