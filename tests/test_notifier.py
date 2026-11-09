from unittest import mock

from moon_watcher.notifier import TelegramNotifier


def make(delivered=1):
    n = TelegramNotifier("token", [1])
    n.broadcast = mock.Mock(return_value=delivered)
    return n


def test_cooldown_suppresses_repeats():
    n = make()
    assert n.send_alert("x", "s", "cpu_high") is True
    assert n.send_alert("x", "s", "cpu_high") is False
    assert n.send_alert("x", "s", "ram_high") is True      # different event type
    assert n.broadcast.call_count == 2


def test_state_changes_bypass_cooldown():
    n = make()
    n.send_alert("down", "s", "downtime", cooldown=False)
    n.send_alert("down", "s", "downtime", cooldown=False)
    assert n.broadcast.call_count == 2


def test_failed_delivery_does_not_start_cooldown():
    n = make(delivered=0)
    assert n.send_alert("x", "s", "e") is False
    n.broadcast.return_value = 1
    assert n.send_alert("x", "s", "e") is True
