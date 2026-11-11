import pytest

from moon_watcher import settings as st
from moon_watcher.settings import Settings


@pytest.fixture
def stored(monkeypatch):
    data = {}
    monkeypatch.setattr(st.Saved, "load", lambda self, default: data.get(self.key, default))
    monkeypatch.setattr(st.Saved, "save", lambda self, value: data.__setitem__(self.key, value))
    return data


def test_everything_on_by_default(stored):
    s = Settings()
    s.load()
    assert all(s.is_enabled(m) for m in st.MONITORS)
    assert s.silent is True


def test_pause_survives_restart(stored):
    s = Settings()
    s.pause("uptime", 0)
    s.set_silent(False)

    again = Settings()
    again.load()
    assert not again.is_enabled("uptime")
    assert again.paused() == {"uptime": None}
    assert again.silent is False


def test_timed_pause_ends_by_itself(stored, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(st.time, "time", lambda: now[0])
    s = Settings()
    s.pause("warmup", 60)
    assert not s.is_enabled("warmup")

    now[0] += 3601
    assert s.is_enabled("warmup")
    assert s.expire_pauses() == ["warmup"]
    assert s.expire_pauses() == []
    assert stored["settings"]["paused"] == {}


def test_resume(stored):
    s = Settings()
    s.pause("resources", 0)
    s.resume("resources")
    assert s.is_enabled("resources")
