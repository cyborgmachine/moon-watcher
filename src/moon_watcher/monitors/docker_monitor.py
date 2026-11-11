"""Docker container monitoring: state, health, restarts, resource usage."""

import fnmatch
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape

import docker
from docker.errors import DockerException

from moon_watcher import database as db
from moon_watcher.config import (
    DOCKER_ALERT_LOG_LINES,
    IGNORED_DOCKER_CONTAINERS,
    RESOURCE_ALERT_CHECKS,
    RESOURCE_CHECK_INTERVAL,
    THRESHOLD_CONTAINER_CPU,
    THRESHOLD_CONTAINER_MEM,
)
from moon_watcher.utils.fmt import bar, fmt_bytes, fmt_duration, level_icon, parse_docker_ts
from moon_watcher.notifier import Alert, TelegramNotifier
from moon_watcher.store import Saved
from moon_watcher.utils.thresholds import SustainedThreshold

logger = logging.getLogger(__name__)

STATUS_ICON = {"ok": "🟢", "unhealthy": "🟠", "down": "🔴", "idle": "⚪"}


def parse_stats(raw: dict) -> dict:
    """Convert a raw Docker stats payload into plain numbers.

    CPU follows `docker stats`: 100% == one fully used core (so it can exceed 100).
    Memory excludes page cache, matching `docker stats` on both cgroup v1 and v2.
    """
    cpu_stats = raw.get("cpu_stats") or {}
    pre = raw.get("precpu_stats") or {}
    cpu_delta = (cpu_stats.get("cpu_usage") or {}).get("total_usage", 0) - (
        (pre.get("cpu_usage") or {}).get("total_usage", 0)
    )
    sys_delta = (cpu_stats.get("system_cpu_usage") or 0) - (pre.get("system_cpu_usage") or 0)
    online = (
        cpu_stats.get("online_cpus")
        or len((cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or [])
        or 1
    )
    cpu = (cpu_delta / sys_delta) * online * 100.0 if cpu_delta > 0 and sys_delta > 0 else 0.0

    mem = raw.get("memory_stats") or {}
    mem_stats = mem.get("stats") or {}
    cache = mem_stats.get("total_inactive_file", mem_stats.get("inactive_file", mem_stats.get("cache", 0)))
    mem_used = max((mem.get("usage") or 0) - cache, 0)
    mem_limit = mem.get("limit") or 0
    mem_percent = mem_used / mem_limit * 100.0 if mem_limit else 0.0

    nets = (raw.get("networks") or {}).values()
    net_rx = sum(n.get("rx_bytes", 0) for n in nets)
    net_tx = sum(n.get("tx_bytes", 0) for n in nets)

    blk_r = blk_w = 0
    for e in (raw.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
        op = (e.get("op") or "").lower()
        if op == "read":
            blk_r += e.get("value", 0)
        elif op == "write":
            blk_w += e.get("value", 0)

    return {
        "cpu_percent": cpu,
        "mem_used": mem_used,
        "mem_limit": mem_limit,
        "mem_percent": mem_percent,
        "net_rx": net_rx,
        "net_tx": net_tx,
        "blk_read": blk_r,
        "blk_write": blk_w,
        "pids": (raw.get("pids_stats") or {}).get("current", 0),
    }


def classify(info: dict) -> str:
    """ok | unhealthy | down | idle; only 'down' and 'unhealthy' raise alerts."""
    state = info["state"]
    if state == "running":
        return "unhealthy" if info["health"] == "unhealthy" else "ok"
    if state in ("created", "removing"):
        return "idle"
    # A one-shot job (restart policy "no") that finished cleanly is not an incident
    if state == "exited" and info["exit_code"] == 0 and info["restart_policy"] == "no":
        return "idle"
    return "down"  # restarting, paused, dead, crashed / stopped service


# groups for the docker menu in the bot: key -> (icon, label, test)
LIST_FILTERS = {
    "p": ("⚠️", "Problems", lambda i: classify(i) in ("down", "unhealthy")),
    "r": ("🟢", "Running", lambda i: i["state"] == "running"),
    "s": ("⏹", "Stopped", lambda i: i["state"] not in ("running", "restarting")),
    "a": ("📋", "All", lambda i: True),
}


def filter_containers(infos: list[dict], key: str) -> list[dict]:
    test = LIST_FILTERS[key][2]
    return [i for i in infos if test(i)]


def is_ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in IGNORED_DOCKER_CONTAINERS)


def _state_text(info: dict) -> str:
    text = info["state"]
    if info["health"] and info["state"] == "running":
        text += f" ({info['health']})"
    if info["state"] in ("exited", "dead", "restarting") and info["exit_code"] is not None:
        text += f", exit code {info['exit_code']}"
    if info["oom"]:
        text += ", OOMKilled"
    return text


def _nothing() -> None:
    pass


class DockerMonitor:
    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self.last_error: str | None = None
        self._client: docker.DockerClient | None = None
        self._check_lock = threading.Lock()
        # container name -> {"id", "status", "started_at", "alerted"}
        self._prev: dict[str, dict] = {}
        self._res = SustainedThreshold(RESOURCE_ALERT_CHECKS)
        self._daemon_down = False
        self._state_store = Saved("docker_containers")
        self._res_store = Saved("docker_thresholds")

    def load_state(self) -> None:
        self._prev = self._state_store.load({})
        self._res.restore(self._res_store.load([]))

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env(timeout=15)
        return self._client

    def _reset_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @staticmethod
    def _parse_container(c) -> dict:
        attrs = c.attrs
        st = attrs.get("State") or {}
        host = attrs.get("HostConfig") or {}

        nano = host.get("NanoCpus") or 0
        quota = host.get("CpuQuota") or 0
        period = host.get("CpuPeriod") or 100000
        cpu_limit = nano / 1e9 if nano else (quota / period if quota > 0 else None)

        return {
            "id": c.short_id,
            "name": c.name,
            "image": (attrs.get("Config") or {}).get("Image", "?"),
            "state": st.get("Status") or c.status,
            "health": (st.get("Health") or {}).get("Status"),
            "exit_code": st.get("ExitCode"),
            "oom": bool(st.get("OOMKilled")),
            "restart_count": attrs.get("RestartCount", 0),
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "started_at": st.get("StartedAt"),
            "cpu_limit": cpu_limit,
            "stats": None,
            "_c": c,
        }

    @staticmethod
    def _fetch_stats(info: dict) -> dict | None:
        try:
            stats = parse_stats(info["_c"].stats(stream=False))
        except Exception as e:
            logger.warning("stats failed for %s: %s", info["name"], e)
            return None
        # Percent of the CPU limit when one is set, otherwise of a single core
        limit = info["cpu_limit"]
        stats["cpu_load"] = stats["cpu_percent"] / limit if limit else stats["cpu_percent"]
        return stats

    def snapshot(self, with_stats: bool = True) -> list[dict] | None:
        """List all (non-ignored) containers; None if the Docker daemon is unreachable."""
        try:
            containers = self._get_client().containers.list(all=True)
        except DockerException as e:
            self.last_error = str(e)[:200]
            logger.error("Docker unreachable: %s", e)
            self._reset_client()
            return None
        self.last_error = None

        infos = []
        for c in containers:
            try:
                if not is_ignored(c.name):
                    infos.append(self._parse_container(c))
            except Exception as e:  # container removed between list() and inspect
                logger.debug("skip container: %s", e)
        infos.sort(key=lambda i: i["name"])

        if with_stats:
            running = [i for i in infos if i["state"] == "running"]
            if running:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for info, stats in zip(running, pool.map(self._fetch_stats, running), strict=True):
                        info["stats"] = stats
        return infos

    def get_container(self, short_id: str) -> dict | None:
        """Single container with fresh stats (for the detail view)."""
        try:
            c = self._get_client().containers.get(short_id)
        except DockerException as e:
            logger.warning("get_container(%s): %s", short_id, e)
            return None
        info = self._parse_container(c)
        if info["state"] == "running":
            info["stats"] = self._fetch_stats(info)
        return info

    def get_logs(self, short_id: str, tail: int = 30) -> str | None:
        try:
            c = self._get_client().containers.get(short_id)
            return c.logs(tail=tail).decode("utf-8", errors="replace")
        except DockerException as e:
            logger.warning("get_logs(%s): %s", short_id, e)
            return None

    def check(self) -> None:
        with self._check_lock:
            infos = self.snapshot(with_stats=True)

            if infos is None:
                if not self._daemon_down:
                    self._daemon_down = self.notifier.send_alert(
                        "🚨 <b>Docker is unreachable</b>\n\n"
                        f"<code>{escape(self.last_error or 'unknown error')}</code>\n"
                        "Check the socket mount and DOCKER_GID.",
                        site_url="docker", event_type="docker_daemon", cooldown=False,
                    )
                    db.log_event("ERROR", "docker", f"Docker unreachable: {self.last_error}")
                return
            if self._daemon_down:
                self._daemon_down = not self.notifier.send_alert(
                    "✅ <b>Docker is reachable again</b>",
                    site_url="docker", event_type="docker_daemon_ok", cooldown=False,
                )

            outbox: list[tuple[Alert, Callable[[], None]]] = []
            events: list[tuple[str, str, str]] = []
            self._process_states(infos, outbox, events)
            self._process_resources(infos, outbox, events)

            if outbox:
                delivered = self.notifier.send_alerts([alert for alert, _ in outbox])
                for (_, on_sent), ok in zip(outbox, delivered, strict=True):
                    if ok:
                        on_sent()

            self._state_store.save(self._prev)
            self._res_store.save(self._res.alerted())
            for level, message, name in events:
                db.log_event(level, "docker", message, name)
            try:
                db.save_container_metrics(infos)
            except Exception as e:
                logger.error("Failed to save container metrics: %s", e)

    def _process_states(self, infos: list[dict], outbox: list, events: list) -> None:
        seen = set()
        for i in infos:
            name = i["name"]
            seen.add(name)
            status = classify(i)
            prev = self._prev.get(name)
            entry = {"id": i["id"], "status": status, "started_at": i["started_at"], "alerted": False}
            self._prev[name] = entry

            if status in ("down", "unhealthy"):
                if prev and prev.get("status") == status:
                    entry["alerted"] = prev.get("alerted", False)
                if not entry["alerted"]:
                    outbox.append((self._problem_alert(i, status), lambda e=entry: e.update(alerted=True)))
                    if not prev or prev.get("status") != status:
                        events.append(("ERROR", f"Container {status}: {_state_text(i)}", name))

            elif status == "ok" and prev and prev.get("status") in ("down", "unhealthy"):
                if prev.get("alerted"):
                    outbox.append((Alert(
                        f"✅ <b>Docker: container recovered</b>\n\n🐳 <b>{escape(name)}</b>\n"
                        f"State: {escape(_state_text(i))}",
                        f"docker:{name}", "container_recovery", cooldown=False,
                    ), _nothing))
                events.append(("INFO", f"Container recovered: {name}", name))

            elif status == "ok" and prev and prev.get("status") == "ok" and prev.get("started_at") != i["started_at"]:
                if prev.get("id") != i["id"]:
                    # same name, new container: compose recreated it on deploy
                    events.append(("INFO", f"Container recreated: {name}", name))
                    continue
                outbox.append((Alert(
                    f"🔄 <b>Docker: container restarted</b>\n\n🐳 <b>{escape(name)}</b>\n"
                    f"Restart count: {i['restart_count']}"
                    + ("\n💥 Previous run was OOMKilled" if i["oom"] else ""),
                    f"docker:{name}", "container_restart",
                ), _nothing))
                events.append(("WARNING", f"Container restarted: {name}", name))

        for gone in set(self._prev) - seen:
            del self._prev[gone]

    def _problem_alert(self, i: dict, status: str) -> Alert:
        name = i["name"]
        head = "🚨 <b>Docker: container DOWN</b>" if status == "down" else \
               "🟠 <b>Docker: container UNHEALTHY</b>"
        msg = (
            f"{head}\n\n🐳 <b>{escape(name)}</b>\n"
            f"🖼 {escape(i['image'])}\n"
            f"📌 State: {escape(_state_text(i))}\n"
            f"🔁 Restarts: {i['restart_count']} (policy: {escape(i['restart_policy'])})"
        )
        if DOCKER_ALERT_LOG_LINES > 0:
            logs = self.get_logs(i["id"], DOCKER_ALERT_LOG_LINES)
            if logs and logs.strip():
                lines = [ln[:200] for ln in logs.strip().splitlines()[-DOCKER_ALERT_LOG_LINES:]]
                msg += f"\n\n📜 <b>Last logs:</b>\n<pre>{escape(chr(10).join(lines)[-1200:])}</pre>"
        return Alert(msg, f"docker:{name}", f"container_{status}", cooldown=False)

    def _process_resources(self, infos: list[dict], outbox: list, events: list) -> None:
        valid: set[str] = set()
        for i in infos:
            s = i["stats"]
            if i["state"] != "running" or not s:
                continue
            name = i["name"]
            metrics = (
                ("cpu", "CPU", s["cpu_load"], THRESHOLD_CONTAINER_CPU,
                 "of CPU limit" if i["cpu_limit"] else "of one core"),
                ("mem", "memory", s["mem_percent"], THRESHOLD_CONTAINER_MEM,
                 f"{fmt_bytes(s['mem_used'])} / {fmt_bytes(s['mem_limit'])}"),
            )
            for key, label, value, threshold, extra in metrics:
                k = f"{name}:{key}"
                valid.add(k)
                action = self._res.update(k, value > threshold)
                if action == "fire":
                    minutes = self._res.breaches(k) * RESOURCE_CHECK_INTERVAL
                    msg = (
                        f"⚠️ <b>Docker: high {label} usage</b>\n\n"
                        f"🐳 <b>{escape(name)}</b>\n"
                        f"{level_icon(value)} {label}: {value:.1f}% {extra} (threshold {threshold}%)\n"
                        f"⏳ Above threshold for ~{minutes} min"
                    )
                    outbox.append((Alert(msg, f"docker:{name}", f"{key}_high"),
                                   lambda k=k: self._res.mark_alerted(k)))
                    events.append(("WARNING", f"High {label}: {value:.1f}%", name))
                elif action == "recover":
                    outbox.append((Alert(
                        f"✅ <b>Docker: {label} back to normal</b>\n\n🐳 <b>{escape(name)}</b>: {value:.1f}%",
                        f"docker:{name}", f"{key}_ok", cooldown=False,
                    ), _nothing))
        self._res.prune(valid)

    @staticmethod
    def format_summary(infos: list[dict]) -> str:
        if not infos:
            return "🐳 <b>Docker</b>\n\nNo containers found (or all ignored)."

        counts = {key: len(filter_containers(infos, key)) for key in LIST_FILTERS}
        lines = [
            "🐳 <b>Docker</b>\n",
            f"Containers: {counts['a']}",
            f"🟢 Running: {counts['r']}",
            f"⚠️ Problems: {counts['p']}",
            f"⏹ Stopped: {counts['s']}",
        ]

        broken = filter_containers(infos, "p")
        if not broken:
            lines.append("\n✅ No problems")
        else:
            lines.append("\n<b>Needs attention:</b>")
            for i in broken[:5]:
                lines.append(f"{STATUS_ICON[classify(i)]} {escape(i['name'])} — {escape(_state_text(i))}")
            if len(broken) > 5:
                lines.append(f"…and {len(broken) - 5} more")
        return "\n".join(lines)

    @staticmethod
    def format_list(items: list[dict], key: str, page: int, pages: int, total: int) -> str:
        icon, label, _ = LIST_FILTERS[key]
        title = f"{icon} <b>{label}</b> ({total})"
        if pages > 1:
            title += f"   page {page + 1}/{pages}"
        if not items:
            return f"{title}\n\nNothing here."
        lines = [title, ""]
        for i in items:
            lines.append(f"{STATUS_ICON[classify(i)]} <b>{escape(i['name'])}</b> — {escape(_state_text(i))}")
        return "\n".join(lines)

    @staticmethod
    def format_detail(i: dict) -> str:
        status = classify(i)
        started = parse_docker_ts(i["started_at"])
        uptime = ((datetime.now(timezone.utc) - started).total_seconds()
                  if started and i["state"] == "running" else None)

        text = (
            f"{STATUS_ICON[status]} <b>{escape(i['name'])}</b>\n"
            f"🖼 {escape(i['image'])}\n\n"
            f"📌 <b>State:</b> {escape(_state_text(i))}\n"
            f"⏱ <b>Uptime:</b> {fmt_duration(uptime)}\n"
            f"🔁 <b>Restarts:</b> {i['restart_count']} (policy: {escape(i['restart_policy'])})\n"
        )

        s = i["stats"]
        if s:
            limit_txt = f"   (limit {i['cpu_limit']:g} cores)" if i["cpu_limit"] else ""
            text += (
                f"\n{level_icon(s['cpu_load'])} <b>CPU:</b> {s['cpu_percent']:.1f}%{limit_txt}\n"
                f"{level_icon(s['mem_percent'])} <b>Memory:</b> {fmt_bytes(s['mem_used'])} / "
                f"{fmt_bytes(s['mem_limit'])}\n   {bar(s['mem_percent'])}\n"
                f"🌐 <b>Network:</b> ↓ {fmt_bytes(s['net_rx'])}  ↑ {fmt_bytes(s['net_tx'])}\n"
                f"💽 <b>Block I/O:</b> R {fmt_bytes(s['blk_read'])}  W {fmt_bytes(s['blk_write'])}\n"
                f"⚙️ <b>PIDs:</b> {s['pids']}\n"
            )

        try:
            h = db.get_container_metrics_summary(i["name"], 24)
        except Exception as e:
            logger.warning("container summary unavailable: %s", e)
            h = None
        if h:
            text += (
                f"\n📈 <b>Last 24h:</b>\n"
                f"   CPU avg {h['cpu_avg']:.1f}% / max {h['cpu_max']:.1f}%\n"
                f"   Mem avg {fmt_bytes(h['mem_avg'])} / max {fmt_bytes(h['mem_max'])}\n"
                f"   Restarts: {h['restarts'] or 0}"
            )
        return text
