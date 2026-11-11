from unittest import mock

import pytest

from moon_watcher import notifier as nt
from moon_watcher.notifier import Alert, TelegramNotifier


def make(delivered=1):
    n = TelegramNotifier("token", [1])
    n.batch_window = 0.05
    n.broadcast = mock.Mock(return_value=delivered)
    return n


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    monkeypatch.setattr(nt.Saved, "save", lambda self, value: None)


def test_cooldown_suppresses_repeats():
    n = make()
    assert n.send_alert("x", "s", "cpu_high") is True
    assert n.send_alert("x", "s", "cpu_high") is False
    assert n.send_alert("x", "s", "ram_high") is True
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


def test_cooldown_survives_restart():
    n = make()
    n.send_alert("x", "s", "cpu_high")
    restarted = make()
    restarted._cooldown_store.load = lambda default: dict(n._cooldowns)
    restarted.load_state()
    assert restarted.send_alert("x", "s", "cpu_high") is False


def test_burst_goes_out_as_one_message():
    n = make()
    results = n.send_alerts([Alert(f"site {i} down", f"s{i}", "downtime", cooldown=False) for i in range(5)])
    assert results == [True] * 5
    assert n.broadcast.call_count == 1
    text = n.broadcast.call_args[0][0]
    assert "5 events" in text and "site 0 down" in text and "site 4 down" in text


def test_pack_respects_message_size():
    texts = ["a" * 1500] * 5
    chunks = nt.pack(texts)
    assert [i for c in chunks for i in c] == list(range(5))
    assert all(sum(len(texts[i]) for i in c) <= nt.MAX_MESSAGE for c in chunks)
    assert len(chunks) == 3


def response(status, body=None):
    r = mock.Mock(status_code=status, ok=status < 400)
    r.json.return_value = body or {}
    return r


def test_bad_markup_falls_back_to_plain_text():
    n = TelegramNotifier("token", [1])
    n._post = mock.Mock(side_effect=[
        response(400, {"description": "Bad Request: can't parse entities: unclosed tag"}),
        response(200),
    ])
    assert n.send_message(1, "<b>broken &amp; <i>markup") is True
    payload = n._post.call_args_list[1][0][0]
    assert "parse_mode" not in payload
    assert payload["text"] == "broken & markup"


def test_rate_limit_waits_and_retries():
    n = TelegramNotifier("token", [1])
    n._post = mock.Mock(side_effect=[
        response(429, {"description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        response(200),
    ])
    with mock.patch.object(nt.time, "sleep") as sleep:
        assert n.send_message(1, "hi") is True
    sleep.assert_called_once_with(3.5)


def test_other_errors_are_not_retried():
    n = TelegramNotifier("token", [1])
    n._post = mock.Mock(return_value=response(403, {"description": "Forbidden: bot was blocked"}))
    assert n.send_message(1, "hi") is False
    assert n._post.call_count == 1
