import threading
import time
from unittest import mock

from moon_watcher import scheduler as sch
from moon_watcher.scheduler import Job, Scheduler


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wait_idle(job, timeout=2.0):
    deadline = time.time() + timeout
    while job.running and time.time() < deadline:
        time.sleep(0.01)
    assert not job.running


def test_first_run_after_delay_then_interval_from_finish(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sch.time, "monotonic", clock)
    calls = []
    s = Scheduler()
    job = Job("j", "Job", lambda: calls.append(clock.now), interval=600, first_delay=15)
    s.add(job)

    s.run_pending()
    assert calls == []
    clock.now += 15
    s.run_pending()
    wait_idle(job)
    assert calls == [1015.0]
    assert job.next_due == 1015.0 + 600

    clock.now += 599
    s.run_pending()
    assert len(calls) == 1


def test_running_job_is_not_started_twice():
    release = threading.Event()
    calls = []

    def work(*args):
        calls.append(args)
        release.wait(2)

    s = Scheduler()
    job = Job("j", "Job", work, interval=60)
    s.add(job)
    assert s.trigger("j", 42) is True
    assert s.trigger("j", 43) is False
    s.run_pending()
    release.set()
    wait_idle(job)
    assert calls == [(42,)]


def test_paused_job_is_skipped_and_rechecked_soon(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sch.time, "monotonic", clock)
    enabled = [False]
    calls = []
    s = Scheduler()
    job = Job("j", "Job", lambda: calls.append(1), interval=3600, enabled=lambda: enabled[0])
    s.add(job)
    s.run_pending()
    assert calls == [] and job.next_due == clock.now + 30

    enabled[0] = True
    clock.now += 30
    s.run_pending()
    wait_idle(job)
    assert calls == [1]


def test_stuck_job_is_reported_once_and_cleared(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(sch.time, "monotonic", clock)
    release = threading.Event()
    on_stall, on_unstall = mock.Mock(), mock.Mock()
    s = Scheduler(on_stall=on_stall, on_unstall=on_unstall)
    job = Job("j", "Job", lambda: release.wait(2), interval=60, stall_after=300)
    s.add(job)

    s.run_pending()
    clock.now += 301
    s.run_pending()
    s.run_pending()
    assert on_stall.call_count == 1

    release.set()
    wait_idle(job)
    on_unstall.assert_called_once_with(job)
    assert job.stalled is False


def test_failing_job_records_error_and_keeps_schedule():
    s = Scheduler()

    def boom():
        raise ValueError("nope")

    job = Job("j", "Job", boom, interval=60)
    s.add(job)
    s.run_pending()
    wait_idle(job)
    assert job.last_error == "ValueError: nope"
    assert job.last_finished is not None
