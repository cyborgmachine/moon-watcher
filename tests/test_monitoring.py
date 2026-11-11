"""Unit tests for the Docker / host monitoring logic (no Docker daemon or DB needed)."""

import copy
import os
from unittest import mock

os.environ.setdefault("RESOURCE_ALERT_CHECKS", "3")

from moon_watcher.monitors import docker_monitor as dm  # noqa: E402
from moon_watcher.monitors.docker_monitor import (  # noqa: E402
    DockerMonitor,
    classify,
    filter_containers,
    parse_stats,
)
from moon_watcher.utils.paging import paginate  # noqa: E402
from moon_watcher.utils import fmt  # noqa: E402
from moon_watcher.utils.thresholds import SustainedThreshold  # noqa: E402


def raw_stats(cpu_total=2_000_000_000, pre_total=1_000_000_000,
              sys_total=20_000_000_000, pre_sys=10_000_000_000, online=4, v2=True):
    mem_stats = {"inactive_file": 50 * 1024**2} if v2 else {"total_inactive_file": 50 * 1024**2}
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": cpu_total}, "system_cpu_usage": sys_total, "online_cpus": online},
        "precpu_stats": {"cpu_usage": {"total_usage": pre_total}, "system_cpu_usage": pre_sys},
        "memory_stats": {"usage": 150 * 1024**2, "limit": 512 * 1024**2, "stats": mem_stats},
        "networks": {"eth0": {"rx_bytes": 100, "tx_bytes": 50}, "eth1": {"rx_bytes": 10, "tx_bytes": 5}},
        "blkio_stats": {"io_service_bytes_recursive": [
            {"op": "read", "value": 1000}, {"op": "Write", "value": 400}, {"op": "Total", "value": 1400}]},
        "pids_stats": {"current": 7},
    }


def test_cpu_matches_docker_formula():
    # container used 1e9 ns of 1e10 ns system time across 4 cpus -> 0.1 * 4 * 100 = 40%
    assert round(parse_stats(raw_stats())["cpu_percent"], 2) == 40.0


def test_memory_excludes_cache_v1_and_v2():
    for v2 in (True, False):
        s = parse_stats(raw_stats(v2=v2))
        assert s["mem_used"] == 100 * 1024**2
        assert round(s["mem_percent"], 1) == round(100 / 512 * 100, 1)


def test_network_blkio_pids():
    s = parse_stats(raw_stats())
    assert (s["net_rx"], s["net_tx"]) == (110, 55)
    assert (s["blk_read"], s["blk_write"]) == (1000, 400)
    assert s["pids"] == 7


def test_empty_payload_does_not_crash():
    s = parse_stats({})
    assert s["cpu_percent"] == 0.0 and s["mem_percent"] == 0.0


def info(state="running", health=None, exit_code=0, policy="always", name="web", started="t1", oom=False,
         cid="abc123"):
    return {"id": cid, "name": name, "image": "img", "state": state, "health": health,
            "exit_code": exit_code, "oom": oom, "restart_count": 0, "restart_policy": policy,
            "started_at": started, "cpu_limit": None, "stats": None}


def test_classify():
    assert classify(info()) == "ok"
    assert classify(info(health="healthy")) == "ok"
    assert classify(info(health="starting")) == "ok"
    assert classify(info(health="unhealthy")) == "unhealthy"
    assert classify(info("exited", exit_code=1)) == "down"
    assert classify(info("restarting", exit_code=137)) == "down"
    assert classify(info("exited", exit_code=0, policy="always")) == "down"   # service must not stop
    assert classify(info("exited", exit_code=0, policy="no")) == "idle"        # finished one-shot job
    assert classify(info("created")) == "idle"


def test_sustained_threshold_fires_once_then_recovers():
    t = SustainedThreshold(3)
    assert [t.update("k", True) for _ in range(2)] == [None, None]   # spike filtered
    assert t.update("k", True) == "fire"
    t.mark_alerted("k")
    assert t.update("k", True) is None                                # no repeat
    assert t.update("k", False) == "recover"
    assert t.update("k", False) is None


def test_short_spike_never_alerts():
    t = SustainedThreshold(3)
    for breached in (True, True, False):
        t.update("k", breached)
    assert t.update("k", True) is None


def test_fire_repeats_until_marked_delivered():
    t = SustainedThreshold(1)
    assert t.update("k", True) == "fire"
    assert t.update("k", True) == "fire"   # alert was suppressed -> retry
    t.mark_alerted("k")
    assert t.update("k", True) is None


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.deliver = True

    def send_alert(self, text, site_url="", event_type="generic", cooldown=True):
        self.sent.append((event_type, site_url, text))
        return self.deliver

    def send_alerts(self, alerts):
        return [self.send_alert(a.text, a.key, a.event, a.cooldown) for a in alerts]


class MemoryStore:
    """Stands in for the kv_state table."""

    def __init__(self):
        self.data = {}

    def saved(self, key):
        store = self

        class Saved:
            def __init__(self, k):
                self.key = k

            def load(self, default):
                return store.data.get(self.key, default)

            def save(self, value):
                store.data[self.key] = copy.deepcopy(value)

        return Saved(key)


def run_checks(mon, sequence):
    it = iter(sequence)
    mon.snapshot = lambda with_stats=True: next(it)
    with mock.patch.object(dm.db, "save_container_metrics"), mock.patch.object(dm.db, "log_event"):
        for _ in sequence:
            mon.check()


def new_monitor(notifier=None, memory=None):
    memory = memory or MemoryStore()
    with mock.patch.object(dm, "Saved", memory.saved):
        mon = DockerMonitor(notifier or FakeNotifier())
    mon.load_state()
    return mon, memory


def make_monitor(sequence):
    """Monitor whose snapshot() yields the given lists of container infos, one per check."""
    mon, _ = new_monitor()
    run_checks(mon, sequence)
    return mon.notifier


def types(n):
    return [t for t, _, _ in n.sent]


def test_down_then_recovery_alerts():
    n = make_monitor([[info()], [info("exited", exit_code=1)], [info("exited", exit_code=1)], [info()]])
    assert types(n) == ["container_down", "container_recovery"]   # no repeat while still down


def test_restart_between_checks_is_detected():
    n = make_monitor([[info(started="t1")], [info(started="t2")]])
    assert types(n) == ["container_restart"]


def test_recreated_container_is_not_a_restart():
    n = make_monitor([[info(started="t1")], [info(started="t2", cid="def456")]])
    assert types(n) == []


def test_undelivered_down_alert_is_retried():
    mon, _ = new_monitor()
    mon.notifier.deliver = False
    run_checks(mon, [[info()], [info("exited", exit_code=1)]])
    mon.notifier.deliver = True
    run_checks(mon, [[info("exited", exit_code=1)], [info("exited", exit_code=1)]])
    assert types(mon.notifier) == ["container_down", "container_down"]


def test_no_repeat_alert_after_bot_restart():
    mon, memory = new_monitor()
    run_checks(mon, [[info()], [info("exited", exit_code=1)]])
    restarted, _ = new_monitor(memory=memory)
    run_checks(restarted, [[info("exited", exit_code=1)], [info()]])
    assert types(mon.notifier) == ["container_down"]
    assert types(restarted.notifier) == ["container_recovery"]


def test_restart_while_bot_was_offline_is_reported():
    mon, memory = new_monitor()
    run_checks(mon, [[info(started="t1")]])
    restarted, _ = new_monitor(memory=memory)
    run_checks(restarted, [[info(started="t2")]])
    assert types(restarted.notifier) == ["container_restart"]


def test_resource_recovery_after_bot_restart():
    def hot(cpu):
        i = info()
        i["stats"] = {"cpu_percent": cpu, "cpu_load": cpu, "mem_used": 1, "mem_limit": 100,
                      "mem_percent": 1.0, "net_rx": 0, "net_tx": 0, "pids": 1}
        return [i]
    mon, memory = new_monitor()
    run_checks(mon, [hot(95), hot(96), hot(97)])
    restarted, _ = new_monitor(memory=memory)
    run_checks(restarted, [hot(5)])
    assert types(mon.notifier) == ["cpu_high"]
    assert types(restarted.notifier) == ["cpu_ok"]


def test_unhealthy_alert():
    n = make_monitor([[info()], [info(health="unhealthy")], [info(health="healthy")]])
    assert types(n) == ["container_unhealthy", "container_recovery"]


def test_finished_one_shot_job_is_silent():
    n = make_monitor([[info(policy="no")], [info("exited", exit_code=0, policy="no")]])
    assert types(n) == []


def test_daemon_down_and_back():
    n = make_monitor([None, None, [info()]])
    assert types(n) == ["docker_daemon", "docker_daemon_ok"]


def test_high_cpu_needs_three_checks_and_recovers():
    def hot(cpu):
        i = info()
        i["stats"] = {"cpu_percent": cpu, "cpu_load": cpu, "mem_used": 1, "mem_limit": 100,
                      "mem_percent": 1.0, "net_rx": 0, "net_tx": 0, "pids": 1}
        return [i]
    n = make_monitor([hot(95), hot(96), hot(97), hot(98), hot(5)])
    assert types(n) == ["cpu_high", "cpu_ok"]


def test_cpu_limit_scales_threshold():
    i = info()
    i["cpu_limit"] = 0.5
    i["_c"] = mock.Mock()
    i["_c"].stats.return_value = raw_stats(cpu_total=1_100_000_000, pre_total=1_000_000_000,
                                           sys_total=10_000_000_000, pre_sys=0, online=1)
    s = DockerMonitor._fetch_stats(i)
    assert round(s["cpu_percent"], 1) == 1.0          # 1% of one core...
    assert round(s["cpu_load"], 1) == 2.0             # ...is 2% of a 0.5-core limit


def test_ignore_patterns():
    with mock.patch.object(dm, "IGNORED_DOCKER_CONTAINERS", ["tmp-*", "db"]):
        assert dm.is_ignored("tmp-job") and dm.is_ignored("db")
        assert not dm.is_ignored("web")


def test_formatters():
    assert fmt.fmt_bytes(0) == "0 B"
    assert fmt.fmt_bytes(1536) == "1.5 KiB"
    assert fmt.fmt_bytes(3 * 1024**3) == "3.00 GiB"
    assert fmt.bar(50) == "█████░░░░░ 50%"
    assert fmt.fmt_duration(90061) == "1d 1h"
    assert fmt.parse_docker_ts("2026-11-09T04:46:12.123456789Z").year == 2026
    assert fmt.parse_docker_ts("0001-01-01T00:00:00Z") is None


def sample_fleet():
    return [
        info(name="web"),
        info(name="db", health="healthy"),
        info(name="api", health="unhealthy"),
        info(name="worker", state="exited", exit_code=1),
        info(name="migrate", state="exited", exit_code=0, policy="no"),
        info(name="loop", state="restarting", exit_code=137),
    ]


def names(items):
    return [i["name"] for i in items]


def test_filters():
    fleet = sample_fleet()
    assert names(filter_containers(fleet, "p")) == ["api", "worker", "loop"]
    assert names(filter_containers(fleet, "r")) == ["web", "db", "api"]
    # a restarting container is a problem, not "stopped"
    assert names(filter_containers(fleet, "s")) == ["worker", "migrate"]
    assert len(filter_containers(fleet, "a")) == 6


def test_summary_counts_and_problem_list():
    text = DockerMonitor.format_summary(sample_fleet())
    assert "Containers: 6" in text and "Problems: 3" in text
    assert "worker" in text and "exit code 1" in text
    assert "migrate" not in text  # finished one-shot job is not a problem


def test_summary_without_problems():
    text = DockerMonitor.format_summary([info(name="web")])
    assert "No problems" in text


def test_summary_caps_problem_list():
    broken = [info(name=f"c{n}", state="exited", exit_code=1) for n in range(9)]
    text = DockerMonitor.format_summary(broken)
    assert "and 4 more" in text and "c8" not in text


def test_list_escapes_html_and_fits_in_a_message():
    page = [info(name=f"c<{n}>&" + "x" * 50, state="exited", exit_code=1) for n in range(8)]
    text = DockerMonitor.format_list(page, "a", 0, 4, 30)
    assert "&lt;" in text and "page 1/4" in text and len(text) < 4096


def test_empty_list():
    assert "Nothing here" in DockerMonitor.format_list([], "p", 0, 1, 0)


def test_paginate():
    items = list(range(20))
    chunk, page, pages = paginate(items, 0, 8)
    assert chunk == list(range(8)) and (page, pages) == (0, 3)
    assert paginate(items, 2, 8)[0] == [16, 17, 18, 19]
    # page out of range (list shrank while the message was open)
    assert paginate(items, 99, 8)[1] == 2
    assert paginate(items, -5, 8)[1] == 0
    assert paginate([], 0, 8) == ([], 0, 1)


def test_host_metrics_real_machine():
    from moon_watcher.monitors import system as sm
    m = sm.collect_host_metrics()
    assert 0 <= m["ram_percent"] <= 100 and m["cpu_cores"] >= 1
    with mock.patch.object(sm.db, "get_host_metrics_summary", return_value=None):
        assert "Server resources" in sm.SystemMonitor.format_report(m)
