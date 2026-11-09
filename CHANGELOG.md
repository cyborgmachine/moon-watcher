# Changelog

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
