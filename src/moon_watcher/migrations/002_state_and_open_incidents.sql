-- Small key/value store for state that has to survive a restart:
-- settings, docker container states, sent alerts, cooldowns, heartbeat
CREATE TABLE IF NOT EXISTS kv_state (
    key        TEXT PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one open incident per site. Older duplicates (possible before this
-- migration, when a manual and a scheduled check raced) are closed first.
UPDATE downtime_incidents d
SET ended_at = now()
WHERE ended_at IS NULL
  AND id <> (SELECT max(id) FROM downtime_incidents x
             WHERE x.site_url = d.site_url AND x.ended_at IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS uq_downtime_open
    ON downtime_incidents (site_url) WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_uptime_time ON uptime_checks (checked_at);
CREATE INDEX IF NOT EXISTS idx_container_metrics_recorded ON container_metrics (recorded_at);
