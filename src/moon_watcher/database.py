"""PostgreSQL access: connection pool, migrations, queries."""

import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from moon_watcher.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")

POOL_SIZE = 10
POOL_WAIT = 10
CONNECT_TIMEOUT = 5
STATEMENT_TIMEOUT_MS = 15_000
CLEANUP_BATCH = 5000
# after a lost connection, fail fast for a while instead of waiting on every call
OUTAGE_BACKOFF = 30

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_slots = threading.BoundedSemaphore(POOL_SIZE)
_down_until = 0.0


class DatabaseBusy(Exception):
    pass


class DatabaseUnavailable(Exception):
    pass


def _mark_down() -> None:
    global _down_until
    _down_until = time.monotonic() + OUTAGE_BACKOFF


def is_available() -> bool:
    return time.monotonic() >= _down_until


def init_db() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(
        1,
        POOL_SIZE,
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=CONNECT_TIMEOUT,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )
    _apply_migrations()


def _apply_migrations() -> None:
    files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version    TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        conn.commit()
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
        for name in files:
            if name in applied:
                continue
            with open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8") as f:
                cur.execute(f.read())
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (name,))
            conn.commit()
            logger.info("Applied migration %s", name)


def close_db() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("Database pool closed")


def _rollback(conn) -> bool:
    try:
        conn.rollback()
        return True
    except psycopg2.Error:
        return False


@contextmanager
def get_conn():
    if _pool is None:
        raise RuntimeError("Database pool is not initialized, call init_db() first")
    if not is_available():
        raise DatabaseUnavailable("database is unreachable, will retry shortly")
    if not _slots.acquire(timeout=POOL_WAIT):
        raise DatabaseBusy("all database connections are busy")
    try:
        try:
            conn = _pool.getconn()
        except psycopg2.OperationalError:
            _mark_down()
            raise
        broken = False
        try:
            yield conn
        except Exception:
            broken = not _rollback(conn) or bool(conn.closed)
            if broken:
                _mark_down()
            raise
        finally:
            _pool.putconn(conn, close=broken or bool(conn.closed))
    finally:
        _slots.release()


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def log_event(
    level: str,
    category: str,
    message: str,
    site_url: str | None = None,
    metadata: dict | None = None,
) -> None:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO bot_events (level, category, message, site_url, metadata)
                   VALUES (%s, %s, %s, %s, %s)""",
                (level, category, message, site_url, psycopg2.extras.Json(metadata)),
            )
            conn.commit()
    except Exception as e:
        logger.error("Failed to log event to DB: %s", e)


def kv_get(key: str, default: Any = None) -> Any:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else default


def kv_set(key: str, value: Any) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, psycopg2.extras.Json(value)),
        )
        conn.commit()


# uptime

def save_uptime_check(
    site_url: str,
    status: str,
    status_code: int,
    response_time: float,
    error: str | None = None,
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO uptime_checks (site_url, status, status_code, response_time, error)
               VALUES (%s, %s, %s, %s, %s)""",
            (site_url, status, status_code, response_time, error),
        )
        conn.commit()


def last_uptime_status() -> dict[str, str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (site_url) site_url, status
               FROM uptime_checks
               WHERE checked_at > now() - interval '7 days'
               ORDER BY site_url, checked_at DESC"""
        )
        return dict(cur.fetchall())


def uptime_window(hours: int, site_url: str | None = None) -> dict[str, dict]:
    """Per-site check counts and uptime percent over the last `hours`."""
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            """SELECT site_url,
                      count(*)                                  AS checks,
                      count(*) FILTER (WHERE status = 'online') AS ok,
                      avg(response_time) FILTER (WHERE status = 'online') AS avg_response
               FROM uptime_checks
               WHERE checked_at > now() - make_interval(hours => %s)
                 AND (%s::text IS NULL OR site_url = %s)
               GROUP BY site_url""",
            (hours, site_url, site_url),
        )
        result = {}
        for row in cur.fetchall():
            row = dict(row)
            row["percent"] = row["ok"] / row["checks"] * 100 if row["checks"] else None
            result[row["site_url"]] = row
        return result


def get_recent_uptime_checks(site_url: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            """SELECT status, status_code, response_time, error, checked_at
               FROM uptime_checks WHERE site_url = %s
               ORDER BY checked_at DESC LIMIT %s""",
            (site_url, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def get_open_incidents() -> dict[str, dict]:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            "SELECT id, site_url, started_at, alert_sent FROM downtime_incidents WHERE ended_at IS NULL"
        )
        return {r["site_url"]: dict(r) for r in cur.fetchall()}


def open_downtime_incident(site_url: str, started_at: datetime) -> int:
    """Open an incident or return the one that is already open for this site."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO downtime_incidents (site_url, started_at) VALUES (%s, %s)
               ON CONFLICT (site_url) WHERE ended_at IS NULL DO NOTHING
               RETURNING id""",
            (site_url, started_at),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT id FROM downtime_incidents WHERE site_url = %s AND ended_at IS NULL",
                (site_url,),
            )
            row = cur.fetchone()
        conn.commit()
        return row[0]


def close_downtime_incident(site_url: str) -> timedelta | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE downtime_incidents
               SET ended_at = now()
               WHERE site_url = %s AND ended_at IS NULL
               RETURNING ended_at - started_at""",
            (site_url,),
        )
        row = cur.fetchone()
        conn.commit()
    return row[0] if row else None


def mark_incident_alerted(incident_id: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE downtime_incidents SET alert_sent = TRUE WHERE id = %s", (incident_id,))
        conn.commit()


def count_downtime_incidents(site_url: str, days: int) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM downtime_incidents
               WHERE site_url = %s AND alert_sent AND started_at > now() - make_interval(days => %s)""",
            (site_url, days),
        )
        return cur.fetchone()[0]


# warmup

def start_warmup_run(site_url: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO warmup_runs (site_url) VALUES (%s) RETURNING id", (site_url,))
        row = cur.fetchone()
        conn.commit()
        return row[0]


def finish_warmup_run(
    run_id: int,
    total_pages: int,
    successful: int,
    failed: int,
    duration_sec: float,
    avg_load_time: float,
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE warmup_runs
               SET total_pages = %s, successful = %s, failed = %s,
                   duration_sec = %s, avg_load_time = %s, finished_at = now()
               WHERE id = %s""",
            (total_pages, successful, failed, duration_sec, avg_load_time, run_id),
        )
        conn.commit()


def get_warmup_stats(site_url: str) -> dict[str, Any]:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM v_warmup_stats WHERE site_url = %s", (site_url,))
        row = cur.fetchone()
    if row:
        return dict(row)
    return {
        "site_url": site_url,
        "total_runs": 0,
        "successful_pages": 0,
        "failed_pages": 0,
        "total_pages": 0,
        "last_warmup": None,
    }


def get_all_warmup_stats() -> list[dict]:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM v_warmup_stats ORDER BY site_url")
        return [dict(r) for r in cur.fetchall()]


# domains

def save_domain_check(
    domain_name: str,
    registrar: str | None = None,
    creation_date: datetime | None = None,
    expiration_date: datetime | None = None,
    updated_date: datetime | None = None,
    days_until_expiry: int | None = None,
    alert_status: str | None = None,
    name_servers: list[str] | None = None,
    whois_status: list[str] | None = None,
    error: str | None = None,
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO domain_checks
               (domain_name, registrar, creation_date, expiration_date,
                updated_date, days_until_expiry, alert_status,
                name_servers, whois_status, error)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                domain_name, registrar, creation_date, expiration_date,
                updated_date, days_until_expiry, alert_status,
                name_servers, whois_status, error,
            ),
        )
        conn.commit()


def get_latest_domain_info(domain_name: str) -> dict | None:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM v_domain_latest WHERE domain_name = %s", (domain_name,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_domains_latest() -> list[dict]:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute("SELECT * FROM v_domain_latest ORDER BY domain_name")
        return [dict(r) for r in cur.fetchall()]


def domain_error_streak(domain_name: str, limit: int) -> int:
    """How many of the latest checks in a row gave no expiry date (looks at most `limit` back)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT alert_status FROM domain_checks
               WHERE domain_name = %s ORDER BY checked_at DESC LIMIT %s""",
            (domain_name, limit),
        )
        streak = 0
        for (status,) in cur.fetchall():
            if status not in ("error", "unknown"):
                break
            streak += 1
        return streak


def should_send_domain_alert(domain_name: str, alert_level: int) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM domain_alerts WHERE domain_name = %s AND alert_level = %s",
            (domain_name, alert_level),
        )
        return cur.fetchone() is None


def record_domain_alert(domain_name: str, alert_level: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO domain_alerts (domain_name, alert_level)
               VALUES (%s, %s)
               ON CONFLICT (domain_name, alert_level) DO UPDATE SET sent_at = now()""",
            (domain_name, alert_level),
        )
        conn.commit()


def clear_domain_alert(domain_name: str, alert_level: int) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM domain_alerts WHERE domain_name = %s AND alert_level = %s",
            (domain_name, alert_level),
        )
        conn.commit()
        return cur.rowcount > 0


def reset_domain_alerts(domain_name: str) -> None:
    """Forget expiry alerts after a renewal (the lookup-failure marker stays)."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM domain_alerts WHERE domain_name = %s AND alert_level >= 0", (domain_name,))
        conn.commit()


# metrics

def save_host_metrics(m: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO host_metrics (cpu_percent, ram_percent, swap_percent, disk_percent, load1)
               VALUES (%s, %s, %s, %s, %s)""",
            (m["cpu_percent"], m["ram_percent"], m["swap_percent"], m["disk_percent"], m["load1"]),
        )
        conn.commit()


def get_host_metrics_summary(hours: int = 24) -> dict | None:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            """SELECT count(*)            AS samples,
                      avg(cpu_percent)    AS cpu_avg,  max(cpu_percent)  AS cpu_max,
                      avg(ram_percent)    AS ram_avg,  max(ram_percent)  AS ram_max,
                      max(disk_percent)   AS disk_max
               FROM host_metrics
               WHERE recorded_at > now() - make_interval(hours => %s)""",
            (hours,),
        )
        row = cur.fetchone()
        return dict(row) if row and row["samples"] else None


def save_container_metrics(rows: list[dict]) -> None:
    if not rows:
        return
    values = []
    for r in rows:
        s = r.get("stats") or {}
        values.append((
            r["name"], r["state"], r.get("health"),
            s.get("cpu_percent"), s.get("mem_used"), s.get("mem_limit"), s.get("mem_percent"),
            s.get("net_rx"), s.get("net_tx"), s.get("pids"), r.get("restart_count"),
        ))
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO container_metrics
               (container_name, state, health, cpu_percent, mem_used, mem_limit,
                mem_percent, net_rx, net_tx, pids, restart_count)
               VALUES %s""",
            values,
        )
        conn.commit()


def get_container_metrics_summary(name: str, hours: int = 24) -> dict | None:
    with get_conn() as conn, _dict_cursor(conn) as cur:
        cur.execute(
            """SELECT count(cpu_percent)   AS samples,
                      avg(cpu_percent)     AS cpu_avg,  max(cpu_percent)  AS cpu_max,
                      avg(mem_used)        AS mem_avg,  max(mem_used)     AS mem_max,
                      max(restart_count) - min(restart_count) AS restarts
               FROM container_metrics
               WHERE container_name = %s
                 AND recorded_at > now() - make_interval(hours => %s)""",
            (name, hours),
        )
        row = cur.fetchone()
        return dict(row) if row and row["samples"] else None


# housekeeping

def _delete_older_than(table: str, column: str, days: int) -> int:
    total = 0
    while True:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""DELETE FROM {table} WHERE ctid IN (
                        SELECT ctid FROM {table}
                        WHERE {column} < now() - make_interval(days => %s)
                        LIMIT %s)""",
                (days, CLEANUP_BATCH),
            )
            deleted = cur.rowcount
            conn.commit()
        total += deleted
        if deleted < CLEANUP_BATCH:
            return total


def cleanup_old_records(days: int, metrics_days: int) -> dict[str, int]:
    """Delete old rows in small batches so no single statement hits the timeout."""
    tables = [
        ("uptime_checks", "checked_at", days),
        ("warmup_runs", "started_at", days),
        ("domain_checks", "checked_at", days),
        ("bot_events", "created_at", days),
        ("host_metrics", "recorded_at", metrics_days),
        ("container_metrics", "recorded_at", metrics_days),
    ]
    deleted = {table: _delete_older_than(table, column, keep) for table, column, keep in tables}
    total = sum(deleted.values())
    if total:
        log_event("INFO", "system", f"Cleanup: deleted {total} old rows")
    logger.info("Cleanup: %s", deleted)
    return deleted
