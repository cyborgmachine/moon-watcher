-- Moon Watcher schema

-- Uptime checks: every individual ping
CREATE TABLE IF NOT EXISTS uptime_checks (
    id          BIGSERIAL PRIMARY KEY,
    site_url    TEXT        NOT NULL,
    status      TEXT        NOT NULL,          -- 'online' | 'offline'
    status_code INT         NOT NULL DEFAULT 0,
    response_time FLOAT     NOT NULL DEFAULT 0,
    error       TEXT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_uptime_site_time ON uptime_checks (site_url, checked_at DESC);

-- Warmup runs: one row per complete warmup of a site
CREATE TABLE IF NOT EXISTS warmup_runs (
    id              BIGSERIAL PRIMARY KEY,
    site_url        TEXT        NOT NULL,
    total_pages     INT         NOT NULL DEFAULT 0,
    successful      INT         NOT NULL DEFAULT 0,
    failed          INT         NOT NULL DEFAULT 0,
    duration_sec    FLOAT       NOT NULL DEFAULT 0,
    avg_load_time   FLOAT       NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_warmup_site_time ON warmup_runs (site_url, started_at DESC);

-- Domain checks: WHOIS results
CREATE TABLE IF NOT EXISTS domain_checks (
    id              BIGSERIAL PRIMARY KEY,
    domain_name     TEXT        NOT NULL,
    registrar       TEXT,
    creation_date   TIMESTAMPTZ,
    expiration_date TIMESTAMPTZ,
    updated_date    TIMESTAMPTZ,
    days_until_expiry INT,
    alert_status    TEXT,                     -- 'ok' | 'warning' | 'critical' | 'expired' | 'error'
    name_servers    TEXT[],
    whois_status    TEXT[],
    error           TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_domain_name_time ON domain_checks (domain_name, checked_at DESC);

-- Domain alert log: prevents duplicate alerts for same threshold
CREATE TABLE IF NOT EXISTS domain_alerts (
    id              BIGSERIAL PRIMARY KEY,
    domain_name     TEXT        NOT NULL,
    alert_level     INT         NOT NULL,     -- threshold days (30, 14, 7, 3, 1)
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain_name, alert_level)
);

-- Bot events: structured log replacing file logs
CREATE TABLE IF NOT EXISTS bot_events (
    id          BIGSERIAL PRIMARY KEY,
    level       TEXT        NOT NULL DEFAULT 'INFO', -- INFO | WARNING | ERROR
    category    TEXT        NOT NULL,                 -- 'warmup' | 'uptime' | 'domain' | 'system'
    message     TEXT        NOT NULL,
    site_url    TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_cat_time ON bot_events (category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_level     ON bot_events (level) WHERE level IN ('WARNING', 'ERROR');

-- Downtime incidents: tracks start/end of outages
CREATE TABLE IF NOT EXISTS downtime_incidents (
    id          BIGSERIAL PRIMARY KEY,
    site_url    TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    duration    INTERVAL GENERATED ALWAYS AS (ended_at - started_at) STORED,
    alert_sent  BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_downtime_site ON downtime_incidents (site_url, started_at DESC);

-- Host metrics: CPU / RAM / swap / disk snapshot on every resource check
CREATE TABLE IF NOT EXISTS host_metrics (
    id            BIGSERIAL PRIMARY KEY,
    cpu_percent   REAL        NOT NULL,
    ram_percent   REAL        NOT NULL,
    swap_percent  REAL        NOT NULL DEFAULT 0,
    disk_percent  REAL        NOT NULL DEFAULT 0,
    load1         REAL        NOT NULL DEFAULT 0,
    recorded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_host_metrics_time ON host_metrics (recorded_at DESC);

-- Docker container metrics: one row per container per resource check
CREATE TABLE IF NOT EXISTS container_metrics (
    id             BIGSERIAL PRIMARY KEY,
    container_name TEXT        NOT NULL,
    state          TEXT        NOT NULL,
    health         TEXT,
    cpu_percent    REAL,
    mem_used       BIGINT,
    mem_limit      BIGINT,
    mem_percent    REAL,
    net_rx         BIGINT,
    net_tx         BIGINT,
    pids           INT,
    restart_count  INT,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_container_metrics_time ON container_metrics (container_name, recorded_at DESC);

-- Aggregated uptime stats per site
CREATE OR REPLACE VIEW v_uptime_stats AS
SELECT
    site_url,
    count(*)                                        AS total_checks,
    count(*) FILTER (WHERE status = 'online')       AS successful_checks,
    count(*) FILTER (WHERE status = 'offline')      AS failed_checks,
    round((count(*) FILTER (WHERE status = 'online')::numeric
           / GREATEST(count(*), 1) * 100), 4)       AS uptime_percentage,
    (SELECT status FROM uptime_checks u2
     WHERE u2.site_url = uptime_checks.site_url
     ORDER BY checked_at DESC LIMIT 1)               AS last_status,
    max(checked_at)                                  AS last_check
FROM uptime_checks
GROUP BY site_url;

-- Aggregated warmup stats per site
CREATE OR REPLACE VIEW v_warmup_stats AS
SELECT
    site_url,
    count(*)                AS total_runs,
    sum(successful)         AS successful_pages,
    sum(failed)             AS failed_pages,
    sum(total_pages)        AS total_pages,
    max(finished_at)        AS last_warmup
FROM warmup_runs
GROUP BY site_url;

-- Latest domain info per domain
CREATE OR REPLACE VIEW v_domain_latest AS
SELECT DISTINCT ON (domain_name)
    domain_name,
    registrar,
    creation_date,
    expiration_date,
    updated_date,
    days_until_expiry,
    alert_status,
    name_servers,
    whois_status,
    error,
    checked_at
FROM domain_checks
ORDER BY domain_name, checked_at DESC;
