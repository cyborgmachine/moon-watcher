from dataclasses import dataclass, field

from moon_watcher.monitors.docker_monitor import DockerMonitor
from moon_watcher.monitors.domain import DomainMonitor
from moon_watcher.monitors.system import SystemMonitor
from moon_watcher.monitors.tls import CertMonitor
from moon_watcher.monitors.uptime import UptimeMonitor
from moon_watcher.notifier import TelegramNotifier
from moon_watcher.scheduler import Scheduler
from moon_watcher.settings import Settings


@dataclass
class Services:
    notifier: TelegramNotifier
    settings: Settings
    scheduler: Scheduler
    uptime: UptimeMonitor
    domains: DomainMonitor
    system: SystemMonitor
    docker: DockerMonitor
    certs: CertMonitor
    warmup_problems: set[str] = field(default_factory=set)
