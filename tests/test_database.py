"""Runs against a real PostgreSQL. Skipped unless TEST_DB_HOST is set, e.g.

    TEST_DB_HOST=127.0.0.1 TEST_DB_PORT=5432 TEST_DB_USER=watcher TEST_DB_PASSWORD=... pytest tests/test_database.py

The database named in TEST_DB_NAME (default moon_watcher_test) is wiped.
"""

import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from moon_watcher import database as db

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DB_HOST"), reason="TEST_DB_HOST not set")


@pytest.fixture
def conn_params():
    return {
        "host": os.getenv("TEST_DB_HOST"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "dbname": os.getenv("TEST_DB_NAME", "moon_watcher_test"),
        "user": os.getenv("TEST_DB_USER", "watcher"),
        "password": os.getenv("TEST_DB_PASSWORD", ""),
    }


@pytest.fixture(autouse=True)
def fresh_db(conn_params, monkeypatch):
    with psycopg2.connect(**conn_params) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    for name, key in (("DB_HOST", "host"), ("DB_PORT", "port"), ("DB_NAME", "dbname"),
                      ("DB_USER", "user"), ("DB_PASSWORD", "password")):
        monkeypatch.setattr(db, name, conn_params[key])
    monkeypatch.setattr(db, "_down_until", 0.0)
    db.init_db()
    yield
    db.close_db()


def sql(query, params=()):
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows


def test_migrations_are_applied_once():
    db.close_db()
    db.init_db()
    versions = [r[0] for r in sql("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == ["001_init.sql", "002_state_and_open_incidents.sql"]


def test_one_open_incident_per_site():
    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    first = db.open_downtime_incident("https://a", started)
    assert db.open_downtime_incident("https://a", datetime.now(timezone.utc)) == first
    assert set(db.get_open_incidents()) == {"https://a"}

    db.mark_incident_alerted(first)
    assert db.get_open_incidents()["https://a"]["alert_sent"] is True
    assert db.close_downtime_incident("https://a") >= timedelta(minutes=5)
    assert db.get_open_incidents() == {}
    assert db.open_downtime_incident("https://a", datetime.now(timezone.utc)) != first
    assert db.count_downtime_incidents("https://a", 7) == 1


def test_uptime_window_and_last_status():
    for status in ("online", "online", "online", "offline"):
        db.save_uptime_check("https://a", status, 200, 0.5)
    sql("UPDATE uptime_checks SET checked_at = now() - interval '3 days' WHERE status = 'offline'")
    db.save_uptime_check("https://b", "offline", 0, 15, "Timeout")

    day = db.uptime_window(24)
    assert day["https://a"]["percent"] == 100.0
    assert day["https://b"]["percent"] == 0.0
    assert db.uptime_window(24 * 7, "https://a")["https://a"]["percent"] == 75.0
    assert set(db.uptime_window(24, "https://a")) == {"https://a"}
    assert db.last_uptime_status() == {"https://a": "online", "https://b": "offline"}


def test_domain_error_streak_and_alert_markers():
    db.save_domain_check("x.dev", alert_status="ok")
    db.save_domain_check("x.dev", alert_status="unknown")
    db.save_domain_check("x.dev", alert_status="error", error="timeout")
    assert db.domain_error_streak("x.dev", 5) == 2
    assert db.domain_error_streak("x.dev", 1) == 1

    db.record_domain_alert("x.dev", 7)
    db.record_domain_alert("x.dev", -1)
    db.reset_domain_alerts("x.dev")
    assert db.should_send_domain_alert("x.dev", 7)
    assert not db.should_send_domain_alert("x.dev", -1)
    assert db.clear_domain_alert("x.dev", -1) is True
    assert db.clear_domain_alert("x.dev", -1) is False


def test_kv_roundtrip():
    assert db.kv_get("nothing", {"a": 1}) == {"a": 1}
    db.kv_set("settings", {"paused": {"uptime": None}, "silent": False})
    db.kv_set("settings", {"paused": {}, "silent": True})
    assert db.kv_get("settings") == {"paused": {}, "silent": True}


def test_cleanup_in_batches(monkeypatch):
    monkeypatch.setattr(db, "CLEANUP_BATCH", 3)
    for _ in range(7):
        db.save_host_metrics({"cpu_percent": 1, "ram_percent": 1, "swap_percent": 0, "disk_percent": 1, "load1": 0})
    db.save_uptime_check("https://a", "online", 200, 0.1)
    sql("UPDATE host_metrics SET recorded_at = now() - interval '5 days'")
    deleted = db.cleanup_old_records(days=30, metrics_days=3)
    assert deleted["host_metrics"] == 7 and deleted["uptime_checks"] == 0


def test_lost_connection_fails_fast_then_recovers(monkeypatch):
    db.kv_get("warm up the pool")
    with psycopg2.connect(host=db.DB_HOST, port=db.DB_PORT, dbname=db.DB_NAME,
                          user=db.DB_USER, password=db.DB_PASSWORD) as admin, admin.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")

    with pytest.raises(psycopg2.OperationalError):
        db.kv_get("x")
    assert not db.is_available()
    with pytest.raises(db.DatabaseUnavailable):
        db.kv_get("x")

    monkeypatch.setattr(db, "_down_until", 0.0)
    assert db.kv_get("x", "back") == "back"
