"""Background jobs.

Every job runs once shortly after start and then `interval` seconds after its
previous run *finished*, so a long run never overlaps the next one. A job that
runs longer than `stall_after` is reported once.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    title: str
    func: Callable[..., None]
    interval: float
    first_delay: float = 0
    stall_after: float | None = None
    enabled: Callable[[], bool] = lambda: True

    next_due: float = 0.0
    running_since: float | None = None
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_error: str | None = None
    stalled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def running(self) -> bool:
        return self.running_since is not None


class Scheduler:
    def __init__(
        self,
        on_stall: Callable[[Job, float], None] | None = None,
        on_unstall: Callable[[Job], None] | None = None,
        tick: float = 5,
    ):
        self.jobs: dict[str, Job] = {}
        self.on_stall = on_stall
        self.on_unstall = on_unstall
        self.tick = tick
        self.last_tick = time.monotonic()
        self._thread: threading.Thread | None = None

    def add(self, job: Job) -> None:
        job.next_due = time.monotonic() + job.first_delay
        self.jobs[job.name] = job

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def trigger(self, name: str, *args) -> bool:
        """Run a job right now (from a button). False if it is already running."""
        return self._start(self.jobs[name], args)

    def _loop(self) -> None:
        while True:
            try:
                self.run_pending()
            except Exception:
                logger.exception("Scheduler error")
            self.last_tick = time.monotonic()
            time.sleep(self.tick)

    def run_pending(self) -> None:
        now = time.monotonic()
        for job in self.jobs.values():
            if job.running:
                self._watch(job, now)
            elif now >= job.next_due:
                if job.enabled():
                    self._start(job, ())
                else:
                    # paused: look again soon so a resume takes effect quickly
                    job.next_due = now + min(job.interval, 30)

    def _watch(self, job: Job, now: float) -> None:
        if job.stall_after is None or job.stalled or job.running_since is None:
            return
        running_for = now - job.running_since
        if running_for > job.stall_after:
            job.stalled = True
            logger.error("Job %s is running for %.0f min", job.name, running_for / 60)
            if self.on_stall:
                self.on_stall(job, running_for)

    def _start(self, job: Job, args: tuple) -> bool:
        with job._lock:
            if job.running:
                return False
            job.running_since = time.monotonic()
        job.last_started = datetime.now(timezone.utc)
        threading.Thread(target=self._run, args=(job, args), name=f"job-{job.name}", daemon=True).start()
        return True

    def _run(self, job: Job, args: tuple) -> None:
        try:
            job.func(*args)
            job.last_error = None
        except Exception as e:
            logger.exception("Job %s failed", job.name)
            job.last_error = f"{type(e).__name__}: {e}"[:200]
        finally:
            job.last_finished = datetime.now(timezone.utc)
            job.next_due = time.monotonic() + job.interval
            was_stalled, job.stalled = job.stalled, False
            with job._lock:
                job.running_since = None
            if was_stalled and self.on_unstall:
                self.on_unstall(job)
