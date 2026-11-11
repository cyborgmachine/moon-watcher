"""Host CPU / RAM / swap / disk / load monitoring with threshold alerts."""

import logging
import os
import time

import psutil

from moon_watcher import database as db
from moon_watcher.config import (
    HOST_DISK_PATH,
    RESOURCE_ALERT_CHECKS,
    RESOURCE_CHECK_INTERVAL,
    THRESHOLD_CPU,
    THRESHOLD_DISK,
    THRESHOLD_RAM,
)
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.store import Saved
from moon_watcher.utils.fmt import bar, fmt_bytes, fmt_duration, level_icon
from moon_watcher.utils.thresholds import SustainedThreshold

logger = logging.getLogger(__name__)


def collect_host_metrics() -> dict:
    """Take one snapshot. Blocks ~1 s (CPU percent needs a sampling window).

    Inside a container /proc/stat, /proc/meminfo, loadavg and uptime still describe
    the host, so these numbers are the server's, not the container's.
    """
    cpu = psutil.cpu_percent(interval=1)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    try:
        disk = psutil.disk_usage(HOST_DISK_PATH)
        disk_percent, disk_used, disk_total = disk.percent, disk.used, disk.total
    except OSError as e:
        logger.warning("disk_usage(%s) failed: %s", HOST_DISK_PATH, e)
        disk_percent = disk_used = disk_total = 0

    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = 0.0

    return {
        "cpu_percent": cpu,
        "cpu_cores": psutil.cpu_count(logical=True) or 1,
        # "available" counts reclaimable page cache as free, like `free -m` does
        "ram_percent": vm.percent,
        "ram_used": vm.total - vm.available,
        "ram_total": vm.total,
        "swap_percent": sw.percent,
        "swap_used": sw.used,
        "swap_total": sw.total,
        "disk_percent": disk_percent,
        "disk_used": disk_used,
        "disk_total": disk_total,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "uptime_sec": time.time() - psutil.boot_time(),
    }


class SystemMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self._tracker = SustainedThreshold(RESOURCE_ALERT_CHECKS)
        self._store = Saved("host_thresholds")

    def load_state(self) -> None:
        self._tracker.restore(self._store.load([]))

    def check(self) -> dict:
        m = collect_host_metrics()

        checks = (
            ("cpu", "CPU", m["cpu_percent"], THRESHOLD_CPU, ""),
            ("ram", "RAM", m["ram_percent"], THRESHOLD_RAM,
             f"{fmt_bytes(m['ram_used'])} / {fmt_bytes(m['ram_total'])}"),
            ("disk", "Disk", m["disk_percent"], THRESHOLD_DISK,
             f"{fmt_bytes(m['disk_used'])} / {fmt_bytes(m['disk_total'])}"),
        )
        for key, label, value, threshold, extra in checks:
            action = self._tracker.update(key, value > threshold)
            if action == "fire":
                minutes = self._tracker.breaches(key) * RESOURCE_CHECK_INTERVAL
                msg = (
                    f"⚠️ <b>High {label} usage on server</b>\n\n"
                    f"{level_icon(value)} <b>{label}:</b> {value:.1f}% (threshold {threshold}%)\n"
                    + (f"📦 {extra}\n" if extra else "")
                    + f"⏳ Above threshold for ~{minutes} min\n"
                    f"📈 Load avg: {m['load1']:.2f} / {m['load5']:.2f} / {m['load15']:.2f}"
                )
                if self.notifier.send_alert(msg, site_url="host", event_type=f"{key}_high"):
                    self._tracker.mark_alerted(key)
                db.log_event("WARNING", "system", f"High {label}: {value:.1f}%")
            elif action == "recover":
                self.notifier.send_alert(
                    f"✅ <b>{label} back to normal</b>: {value:.1f}% (threshold {threshold}%)",
                    site_url="host", event_type=f"{key}_ok", cooldown=False,
                )

        self._store.save(self._tracker.alerted())
        try:
            db.save_host_metrics(m)
        except Exception as e:
            logger.error("Failed to save host metrics: %s", e)
        return m

    @staticmethod
    def format_report(m: dict) -> str:
        text = (
            f"🖥 <b>Server resources</b>\n\n"
            f"{level_icon(m['cpu_percent'])} <b>CPU:</b> {m['cpu_percent']:.1f}%  ({m['cpu_cores']} cores)\n"
            f"   {bar(m['cpu_percent'])}\n"
            f"   Load avg: {m['load1']:.2f} / {m['load5']:.2f} / {m['load15']:.2f}\n\n"
            f"{level_icon(m['ram_percent'])} <b>RAM:</b> {fmt_bytes(m['ram_used'])} / {fmt_bytes(m['ram_total'])}\n"
            f"   {bar(m['ram_percent'])}\n"
        )
        if m["swap_total"]:
            text += (
                f"{level_icon(m['swap_percent'])} <b>Swap:</b> {fmt_bytes(m['swap_used'])} / "
                f"{fmt_bytes(m['swap_total'])}\n   {bar(m['swap_percent'])}\n"
            )
        text += (
            f"\n{level_icon(m['disk_percent'])} <b>Disk:</b> {fmt_bytes(m['disk_used'])} / "
            f"{fmt_bytes(m['disk_total'])}\n   {bar(m['disk_percent'])}\n\n"
            f"⏱ <b>Uptime:</b> {fmt_duration(m['uptime_sec'])}"
        )

        try:
            h = db.get_host_metrics_summary(24)
        except Exception as e:
            logger.warning("host summary unavailable: %s", e)
            h = None
        if h:
            text += (
                f"\n\n📈 <b>Last 24h:</b>\n"
                f"   CPU avg {h['cpu_avg']:.1f}% / max {h['cpu_max']:.1f}%\n"
                f"   RAM avg {h['ram_avg']:.1f}% / max {h['ram_max']:.1f}%"
            )
        return text
