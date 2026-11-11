# Changelog

## Unreleased

### SSL certificates
- Certificates of every https site (plus `SSL_HOSTS`) are checked every 12 hours
- Alerts at 14 / 7 / 3 / 1 days before expiry, once per threshold, starting over when the certificate is renewed
- Alert when the certificate stops being trusted (expired, wrong host name, broken chain) and when it is fixed
- New 🔒 SSL screen with manual checks; results and sent alerts survive a restart

### Reliability
- Down / recovery alerts no longer depend on the database: open incidents are kept in memory and synced when it is back
- Database calls have a connect timeout, a 15 s statement timeout and fail fast for 30 s after a lost connection
- All checks run right after start (domains used to wait a full day), the next run is counted from the end of the previous one
- A check that hangs is reported once; a watchdog restarts the bot if it stops responding
- Docker container states, sent resource alerts, cooldowns and settings survive a restart: no duplicate alerts, recoveries still arrive
- The startup message says how long the bot was offline
- A manual and a scheduled check of the same site can no longer open two incidents (one open incident per site is enforced in the database)
- Undelivered Docker alerts are retried on the next check, like uptime and domain alerts
- Alerts raised within a few seconds are grouped into one message; Telegram rate limits are respected; broken markup falls back to plain text
- Domains whose lookups keep failing or return no expiry date are reported once

### Changes
- Settings: monitors are paused for 1 / 6 / 24 h or until resumed instead of switched off; pauses are listed in the main menu
- Silent mode no longer repeats "issues detected" on every check during an outage; warmup reports only when the set of broken sites changes
- Scheduled reports go to all admins, not only the first one
- Uptime shows 24 h / 7 d uptime instead of all-time; up to `UPTIME_CONCURRENCY` sites are checked in parallel
- Site, uptime and domain lists are paginated; buttons use stable ids instead of list positions
- Container recreated by a deploy is no longer reported as a restart
- Metrics are kept for `METRICS_RETENTION_DAYS` (3), cleanup deletes in small batches
- `bot.py` split into `app`, `handlers`, `jobs`, `scheduler`, `settings`, `store` and `ui/`; the `schedule` dependency is gone

## 0.1.0 — first public release

### Features
- Uptime monitoring with Telegram alerts, incident tracking and downtime duration
- Cache warmup via sitemap.xml
- WHOIS domain expiry monitoring with multi-level alerts
- Docker container monitoring (state, health, restarts, CPU / RAM / network / IO)
- Host resource monitoring (CPU, RAM, swap, disk, load) with sustained-threshold alerts
- Inline-button control panel in Telegram, PostgreSQL storage, numbered SQL migrations

### Behaviour worth knowing
- A site is reported DOWN only after `UPTIME_FAIL_THRESHOLD` consecutive failed checks (default 2)
- State changes (down / recovered / expiring) are never suppressed by `ALERT_COOLDOWN`;
  the cooldown only throttles repeating alerts (resource usage, container restart loops)
- Domain alert history is reset automatically after a renewal
- A DOWN alert that Telegram did not accept is retried on the next check
- Sitemap index entries pointing to another host are ignored; nesting depth and sitemap size are capped
- Bad values in `.env` stop the bot with a readable message instead of a traceback
- Docker menu opens with a summary and splits containers into Problems / Running / Stopped / All, 8 per page; every Docker screen has a Menu button
- Domain expiry is read over RDAP first (works for .dev, .app and other TLDs without port 43 whois), whois is used when RDAP is not available
