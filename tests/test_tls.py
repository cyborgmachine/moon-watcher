"""SSL checks: target list, real TLS handshakes against a local server, alert rules."""

import shutil
import socket
import ssl
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from moon_watcher.monitors import tls
from moon_watcher.monitors.tls import CertMonitor, collect_targets, fetch_certificate


def test_targets_from_sites_and_extra_hosts():
    urls = [
        "https://Example.com/blog",
        "https://example.com",
        "http://plain.example",
        "https://api.example:8443/health",
        "https://example.com:443/x",
    ]
    assert collect_targets(urls, ["mail.example", "example.com", "db.example:5443"]) == [
        "example.com", "api.example:8443", "mail.example", "db.example:5443",
    ]


# --- real handshakes ---------------------------------------------------------

@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    d = tmp_path_factory.mktemp("certs")

    def run(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "30", "-subj", "/O=Test CA/CN=Test CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj", "/CN=localhost")
    (d / "ext").write_text(
        "subjectAltName=DNS:localhost\n"
        "basicConstraints=CA:FALSE\n"
        "authorityKeyIdentifier=keyid,issuer\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
    )
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "leaf.pem", "-days", "5", "-extfile", "ext")
    return d


@pytest.fixture(scope="module")
def server(certs):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "leaf.pem", certs / "leaf.key")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    yield port
    listener.close()


def trusting(certs):
    return ssl.create_default_context(cafile=str(certs / "ca.pem"))


def test_valid_certificate(server, certs):
    r = fetch_certificate(f"localhost:{server}", context=trusting(certs), timeout=5)
    assert r["status"] == "ok"
    assert r["issuer"] == "Test CA"
    assert tls.days_left(r) in (4, 5)


def test_untrusted_issuer(server):
    r = fetch_certificate(f"localhost:{server}", timeout=5)
    assert r["status"] == "invalid"
    assert "issuer" in r["error"]


def test_wrong_host_name(server, certs):
    r = fetch_certificate(f"127.0.0.1:{server}", context=trusting(certs), timeout=5)
    assert r["status"] == "invalid"
    assert "mismatch" in r["error"].lower()


def test_nothing_listening():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = fetch_certificate(f"127.0.0.1:{port}", timeout=2)
    assert r["status"] == "error"


# --- alert rules -------------------------------------------------------------

class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.deliver = True

    def send_alert(self, text, site_url="", event_type="generic", cooldown=True):
        self.sent.append(event_type)
        return self.deliver


def ok(days, issuer="Let's Encrypt"):
    not_after = datetime.now(timezone.utc) + timedelta(days=days, hours=1)
    return {"status": "ok", "not_after": not_after.replace(microsecond=0).isoformat(), "issuer": issuer}


INVALID = {"status": "invalid", "error": "certificate has expired"}
DOWN = {"status": "error", "error": "Timeout"}


@pytest.fixture
def memory(monkeypatch):
    data = {}
    monkeypatch.setattr(tls.Saved, "load", lambda self, default: data.get(self.key, default))
    monkeypatch.setattr(tls.Saved, "save", lambda self, value: data.__setitem__(self.key, value))
    return data


def run(mon, *results):
    with mock.patch.object(tls, "SSL_ALERT_DAYS", [14, 7, 3, 1]):
        for r in results:
            with mock.patch.object(tls, "fetch_certificate", return_value=dict(r)):
                mon.check("example.com")


def monitor():
    mon = CertMonitor(FakeNotifier())
    mon.load_state()
    return mon


def test_far_from_expiry_is_quiet(memory):
    mon = monitor()
    run(mon, ok(80), ok(79))
    assert mon.notifier.sent == []
    assert mon.status_icon("example.com") == "🟢"


def test_each_threshold_alerts_once(memory):
    mon = monitor()
    cert = ok(10)
    run(mon, cert, cert, cert)
    assert mon.notifier.sent == ["ssl_expiry_14"]

    with mock.patch.object(tls, "days_left", return_value=6):
        run(mon, cert)
    assert mon.notifier.sent == ["ssl_expiry_14", "ssl_expiry_7"]


def test_renewed_certificate_starts_over(memory):
    mon = monitor()
    run(mon, ok(5))
    run(mon, ok(89))
    run(mon, ok(5, issuer="Other CA"))
    assert mon.notifier.sent == ["ssl_expiry_7", "ssl_expiry_7"]


def test_invalid_then_valid_again(memory):
    mon = monitor()
    run(mon, INVALID, INVALID, ok(60))
    assert mon.notifier.sent == ["ssl_invalid", "ssl_valid"]
    assert mon.status_icon("example.com") == "🟢"


def test_unreachable_host_is_left_to_uptime(memory):
    mon = monitor()
    run(mon, DOWN, DOWN)
    assert mon.notifier.sent == []
    assert mon.status_icon("example.com") == "⚠️"


def test_failed_delivery_is_retried(memory):
    mon = monitor()
    mon.notifier.deliver = False
    run(mon, ok(5))
    mon.notifier.deliver = True
    run(mon, ok(5))
    assert mon.notifier.sent == ["ssl_expiry_7", "ssl_expiry_7"]


def test_no_repeat_after_restart(memory):
    cert = ok(5)
    run(monitor(), cert)
    restarted = monitor()
    run(restarted, cert)
    assert restarted.notifier.sent == []
    assert "Let&#x27;s Encrypt" in restarted.format_detail("example.com")


def test_report_and_detail_escape_html(memory):
    mon = monitor()
    run(mon, {"status": "invalid", "error": "Hostname mismatch, certificate is not valid for '<x>'."})
    assert "&lt;x&gt;" in mon.format_detail("example.com")
    assert "example.com — invalid" in mon.format_report(["example.com", "other.example"])
    assert "other.example — not checked" in mon.format_report(["example.com", "other.example"])
