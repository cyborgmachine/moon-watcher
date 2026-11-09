import importlib

import pytest

from moon_watcher import config


def reload_config():
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def restore_config():
    yield
    importlib.reload(config)


def test_defaults(monkeypatch):
    monkeypatch.delenv("DOMAIN_ALERT_DAYS", raising=False)
    cfg = reload_config()
    assert cfg.DOMAIN_ALERT_DAYS == [30, 14, 7, 3, 1]


def test_empty_alert_days_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DOMAIN_ALERT_DAYS", "")
    assert reload_config().DOMAIN_ALERT_DAYS == [30, 14, 7, 3, 1]


def test_empty_number_uses_default(monkeypatch):
    monkeypatch.setenv("WARMUP_INTERVAL", "")
    assert reload_config().WARMUP_INTERVAL == 30


def test_not_a_number(monkeypatch):
    monkeypatch.setenv("UPTIME_CHECK_INTERVAL", "often")
    with pytest.raises(SystemExit, match="UPTIME_CHECK_INTERVAL"):
        reload_config()


def test_zero_interval_rejected(monkeypatch):
    monkeypatch.setenv("RESOURCE_CHECK_INTERVAL", "0")
    with pytest.raises(SystemExit, match="RESOURCE_CHECK_INTERVAL"):
        reload_config()


def test_bad_admin_id(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "123,@me")
    with pytest.raises(SystemExit, match="ADMIN_USER_IDS"):
        reload_config()
