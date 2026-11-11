"""Settings, all read from environment variables."""

import os
from dotenv import load_dotenv

from moon_watcher import __version__

load_dotenv()


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"Config error: {name} must be a number, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise SystemExit(f"Config error: {name} must be at least {minimum}, got {value}")
    return value


def _int_list(name: str, default: str = "") -> list[int]:
    raw = os.getenv(name) or default
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            raise SystemExit(
                f"Config error: {name} must be a comma-separated list of numbers, got {part!r}"
            ) from None
    return result


BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS: list[int] = _int_list("ADMIN_USER_IDS")

WARMUP_SITES: list[str] = [
    s.strip() for s in os.getenv("WARMUP_SITES", "").split(",") if s.strip()
]
UPTIME_SITES: list[str] = [
    s.strip() for s in os.getenv("UPTIME_SITES", "").split(",") if s.strip()
]
MONITORED_DOMAINS: list[str] = [
    d.strip() for d in os.getenv("MONITORED_DOMAINS", "").split(",") if d.strip()
]

WARMUP_INTERVAL: int = _int("WARMUP_INTERVAL", 30, minimum=1)
UPTIME_CHECK_INTERVAL: int = _int("UPTIME_CHECK_INTERVAL", 15, minimum=1)
DOMAIN_CHECK_INTERVAL: int = _int("DOMAIN_CHECK_INTERVAL", 1440, minimum=1)

DELAY_BETWEEN_REQUESTS: int = _int("DELAY_BETWEEN_REQUESTS", 10)
REQUEST_TIMEOUT: int = _int("REQUEST_TIMEOUT", 15)
MAX_RETRIES: int = _int("MAX_RETRIES", 3)
USER_AGENT: str = os.getenv(
    "USER_AGENT",
    f"Mozilla/5.0 (compatible; MoonWatcher/{__version__})",
)

# failed probes in a row before a site counts as down (1 = alert immediately)
UPTIME_FAIL_THRESHOLD: int = _int("UPTIME_FAIL_THRESHOLD", 2, minimum=1)
UPTIME_RETRY_DELAY: int = _int("UPTIME_RETRY_DELAY", 30)
UPTIME_CONCURRENCY: int = _int("UPTIME_CONCURRENCY", 5, minimum=1)

DOMAIN_ALERT_DAYS: list[int] = _int_list("DOMAIN_ALERT_DAYS", "30,14,7,3,1")
# warn once when this many domain lookups in a row have failed
DOMAIN_ERROR_ALERT_AFTER: int = _int("DOMAIN_ERROR_ALERT_AFTER", 3, minimum=1)

# extra hosts to check besides the https sites from UPTIME_SITES / WARMUP_SITES, "host" or "host:port"
SSL_HOSTS: list[str] = [h.strip() for h in os.getenv("SSL_HOSTS", "").split(",") if h.strip()]
SSL_CHECK_INTERVAL: int = _int("SSL_CHECK_INTERVAL", 720, minimum=1)
SSL_ALERT_DAYS: list[int] = _int_list("SSL_ALERT_DAYS", "14,7,3,1")

DB_HOST: str = os.getenv("DB_HOST", "postgres")
DB_PORT: int = _int("DB_PORT", 5432)
DB_NAME: str = os.getenv("DB_NAME", "moon_watcher")
DB_USER: str = os.getenv("DB_USER", "watcher")
DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")

# throttles repeating alerts (resource usage, restart loops); down/up and expiry alerts always go out
ALERT_COOLDOWN: int = _int("ALERT_COOLDOWN", 900)

LOG_RETENTION_DAYS: int = _int("LOG_RETENTION_DAYS", 30, minimum=1)
# host/container metrics are only shown for the last 24h, no reason to keep them long
METRICS_RETENTION_DAYS: int = _int("METRICS_RETENTION_DAYS", 3, minimum=1)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SSL_MONITORING: bool = _env_bool("SSL_MONITORING", True)
DOCKER_MONITORING: bool = _env_bool("DOCKER_MONITORING", True)
# wildcards allowed, e.g. "tmp-*"
IGNORED_DOCKER_CONTAINERS: list[str] = [
    s.strip() for s in os.getenv("IGNORED_DOCKER_CONTAINERS", "").split(",") if s.strip()
]
RESOURCE_CHECK_INTERVAL: int = _int("RESOURCE_CHECK_INTERVAL", 2, minimum=1)
# consecutive checks above a threshold before alerting
RESOURCE_ALERT_CHECKS: int = _int("RESOURCE_ALERT_CHECKS", 3)

THRESHOLD_CPU: int = _int("THRESHOLD_CPU", 90)
THRESHOLD_RAM: int = _int("THRESHOLD_RAM", 90)
THRESHOLD_DISK: int = _int("THRESHOLD_DISK", 90)
THRESHOLD_CONTAINER_CPU: int = _int("THRESHOLD_CONTAINER_CPU", 90)
THRESHOLD_CONTAINER_MEM: int = _int("THRESHOLD_CONTAINER_MEM", 90)

# mount the host root and point this at it for exact disk numbers
HOST_DISK_PATH: str = os.getenv("HOST_DISK_PATH", "/")
# Include the last N log lines in "container down" alerts (0 = off)
DOCKER_ALERT_LOG_LINES: int = _int("DOCKER_ALERT_LOG_LINES", 0)
