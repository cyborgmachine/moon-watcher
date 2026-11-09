#!/usr/bin/env python3
"""PostgreSQL access: connection pool, migrations, queries."""

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from moon_watcher.config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_db(min_conn: int = 2, max_conn: int = 10) -> None:
    """Create the connection pool and ensure schema exists."""
    global _pool
    if _pool is not None:
        return

    _pool = psycopg2.pool.ThreadedConnectionPool(
        min_conn,
        max_conn,
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    _apply_migrations()


def _apply_migrations() -> None:
    """Apply numbered SQL files from migrations/ that are not yet recorded in schema_migrations."""
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


@contextmanager
def get_conn():
    """Borrow a connection from the pool (auto-return on exit)."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialized — call init_db() first")
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


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


def get_uptime_stats(site_url: str) -> dict[str, Any]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_uptime_stats WHERE site_url = %s", (site_url,))
        row = cur.fetchone()
        if row:
            return dict(row)
    return {
        "site_url": site_url,
        "total_checks": 0,
        "successful_checks": 0,
        "failed_checks": 0,
        "uptime_percentage": 100.0,
        "last_status": "unknown",
        "last_check": None,
    }


def get_all_uptime_stats() -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_uptime_stats ORDER BY site_url")
        return [dict(r) for r in cur.fetchall()]


def get_recent_uptime_checks(site_url: str, limit: int = 5) -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT status, status_code, response_time, error, checked_at
               FROM uptime_checks WHERE site_url = %s
               ORDER BY checked_at DESC LIMIT %s""",
            (site_url, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def open_downtime_incident(site_url: str) -> int:
    """Start a new downtime incident, return its id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO downtime_incidents (site_url) VALUES (%s) RETURNING id""",
            (site_url,),
        )
        row = cur.fetchone()
        conn.commit()
        return row[0]


def close_downtime_incident(site_url: str) -> timedelta | None:
    """Close the open incident for a site, return how long it lasted."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE downtime_incidents
               SET ended_at = now()
               WHERE site_url = %s AND ended_at IS NULL
               RETURNING ended_at - started_at AS dur""",
            (site_url,),
        )
        row = cur.fetchone()
        conn.commit()
        if row and row[0] is not None:
            return row[0]
    return None


def get_open_incident(site_url: str) -> dict | None:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT * FROM downtime_incidents
               WHERE site_url = %s AND ended_at IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (site_url,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def mark_incident_alerted(incident_id: int) -> None:
    """Remember that the DOWN alert for this incident was actually delivered."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE downtime_incidents SET alert_sent = TRUE WHERE id = %s", (incident_id,))
        conn.commit()


def count_downtime_incidents(site_url: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM downtime_incidents WHERE site_url = %s",
            (site_url,),
        )
        return cur.fetchone()[0]


def start_warmup_run(site_url: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO warmup_runs (site_url) VALUES (%s) RETURNING id",
            (site_url,),
        )
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
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_warmup_stats ORDER BY site_url")
        return [dict(r) for r in cur.fetchall()]


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
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM v_domain_latest WHERE domain_name = %s",
            (domain_name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_domains_latest() -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_domain_latest ORDER BY domain_name")
        return [dict(r) for r in cur.fetchall()]


def should_send_domain_alert(domain_name: str, alert_level: int) -> bool:
    """Check if we already sent an alert for this domain at this threshold."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM domain_alerts
               WHERE domain_name = %s AND alert_level = %s""",
            (domain_name, alert_level),
        )
        return cur.fetchone() is None  # True = NOT yet sent


def record_domain_alert(domain_name: str, alert_level: int) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO domain_alerts (domain_name, alert_level)
               VALUES (%s, %s)
               ON CONFLICT (domain_name, alert_level) DO UPDATE SET sent_at = now()""",
            (domain_name, alert_level),
        )
        conn.commit()


def reset_domain_alerts(domain_name: str) -> None:
    """Forget sent alerts after a renewal."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM domain_alerts WHERE domain_name = %s", (domain_name,))
        conn.commit()


def save_host_metrics(m: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO host_metrics (cpu_percent, ram_percent, swap_percent, disk_percent, load1)
               VALUES (%s, %s, %s, %s, %s)""",
            (m["cpu_percent"], m["ram_percent"], m["swap_percent"], m["disk_percent"], m["load1"]),
        )
        conn.commit()


def get_host_metrics_summary(hours: int = 24) -> dict | None:
    """Average / peak values over the last `hours` hours."""
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
    """Bulk-insert one row per container (stats may be missing for stopped ones)."""
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
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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


def get_recent_events(
    category: str | None = None,
    level: str | None = None,
    limit: int = 20,
) -> list[dict]:
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        conditions = []
        params: list = []
        if category:
            conditions.append("category = %s")
            params.append(category)
        if level:
            conditions.append("level = %s")
            params.append(level)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(
            f"SELECT * FROM bot_events {where} ORDER BY created_at DESC LIMIT %s",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def get_last_check_times() -> dict[str, datetime | None]:
    """Return timestamps of the last uptime check, warmup run, and domain check."""
    result: dict[str, datetime | None] = {
        "uptime": None,
        "warmup": None,
        "domain": None,
    }
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT max(checked_at) FROM uptime_checks")
        row = cur.fetchone()
        if row and row[0]:
            result["uptime"] = row[0]

        cur.execute("SELECT max(finished_at) FROM warmup_runs")
        row = cur.fetchone()
        if row and row[0]:
            result["warmup"] = row[0]

        cur.execute("SELECT max(checked_at) FROM domain_checks")
        row = cur.fetchone()
        if row and row[0]:
            result["domain"] = row[0]
    return result


def cleanup_old_records(days: int = 30) -> dict[str, int]:
    """Delete records older than `days` from large tables. Returns row counts."""
    deleted: dict[str, int] = {}
    tables = [
        ("uptime_checks", "checked_at"),
        ("warmup_runs", "started_at"),
        ("domain_checks", "checked_at"),
        ("bot_events", "created_at"),
        ("host_metrics", "recorded_at"),
        ("container_metrics", "recorded_at"),
    ]
    with get_conn() as conn, conn.cursor() as cur:
        for table, col in tables:
            cur.execute(
                f"DELETE FROM {table} WHERE {col} < now() - make_interval(days => %s)",
                (days,),
            )
            deleted[table] = cur.rowcount
        conn.commit()
    total = sum(deleted.values())
    if total > 0:
        log_event("INFO", "system", f"Log rotation: deleted {total} old records ({days}d)")
    logger.info("Log rotation (%dd): %s", days, deleted)
    return deleted
